"""对外发布编排的集成测试：幂等、扣账、并发末笔、中断恢复、撤回、不可泄露字段。"""
import copy
import threading
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.constants.privacy import EPSILON_UNIT
from src.models.entities import BudgetDay, BudgetEntry, DailyVolume, ForecastRun, Publication
from src.schemas.publications import PublicationInput
from src.services import publications as pubs
from src.services.privacy import clip


def make_run(s, branch='川北', days=70, start=date(2026, 9, 6), alpha=0.6, beta=0.2):
    run = ForecastRun(name='测试预测', branch=branch, backtest_days=14, alpha=alpha,
                      beta=beta, state='done')
    snapshot = [{'id': i, 'branch': branch, 'day': (start + timedelta(days=i)).isoformat(),
                 'volume': 40 + (i % 7), 'note': ''} for i in range(days)]
    run.result = {'snapshot': snapshot}
    s.add(run)
    s.commit()
    return run


def payload(run, number, epsilon=1.0, daily_cap=3.0, **kw):
    return PublicationInput(number=number, branch=run.branch, run_id=run.id,
                            epsilon=epsilon, daily_cap=daily_cap, **kw)


def drain(s, pid):
    pubs.process(s, pid)


def test_happy_path_release_is_pending_then_claimable(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pub, queued = pubs.create(session, payload(run, 'pub-000001'))
    assert queued is True
    assert pub.state == 'pending'
    assert pubs.public_view(pub) == {'number': 'pub-000001', 'branch': '川北', 'state': 'pending', 'state_label': '待处理'}

    drain(session, pub.id)
    session.refresh(pub)
    assert pub.state == 'claimable'
    view = pubs.public_view(pub)
    assert set(view) == {'number', 'branch', 'state', 'state_label', 'days', 'forecasts', 'mechanism'}
    assert set(view['forecasts']) == {'seasonal_naive', 'holt'}
    for model in view['forecasts']:
        assert len(view['forecasts'][model]) == 7
        assert all(isinstance(v, int) and v >= 0 for v in view['forecasts'][model])
    assert len(view['days']) == 7
    assert view['mechanism']['epsilon_total'] == 1.0
    assert view['mechanism']['delta'] == 0.0


def test_release_never_exposes_raw_values_backtest_truth_or_seed(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pub, _ = pubs.create(session, payload(run, 'pub-000002'))
    drain(session, pub.id)
    session.refresh(pub)
    blob = repr(pubs.public_view(pub))
    for banned in ('snapshot', 'seed', pub.seed, 'actual', 'backtest', 'history', 'volume'):
        assert banned not in blob

    # 数字层面：七日预测不得等于原值序列或原始（未加噪）预测
    from src.services.analysis import holt
    values = [clip(r['volume']) for r in run.result['snapshot']]
    raw_holt = [round(v) for v in holt(values, 7, 0.6, 0.2)]
    released = pubs.public_view(pub)
    assert released['forecasts']['holt'] != raw_holt
    actual_tail = values[-7:]
    assert released['forecasts']['seasonal_naive'] != actual_tail

    # 失败原因只对内部可见
    pub.state = 'failed'
    pub.error = 'backend detail'
    session.commit()
    assert 'error' not in pubs.public_view(pub)
    assert pubs.internal_view(pub)['error'] == 'backend detail'


def test_same_number_same_content_does_not_recharge_and_reuses_record(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    first, _ = pubs.create(session, payload(run, 'pub-000003'))
    used_before = session.get(BudgetDay, ('川北', '2026-09-06')).used_micro
    entries_before = session.query(BudgetEntry).count()

    again, queued = pubs.create(session, payload(run, 'pub-000003'))
    assert queued is True
    assert again.id == first.id
    assert session.query(Publication).count() == 1
    assert session.get(BudgetDay, ('川北', '2026-09-06')).used_micro == used_before
    assert session.query(BudgetEntry).count() == entries_before


def test_same_number_different_content_is_rejected(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pubs.create(session, payload(run, 'pub-000004'))
    with pytest.raises(HTTPException) as err:
        pubs.create(session, payload(run, 'pub-000004', epsilon=2.0))
    assert err.value.status_code == 409


def test_overlapping_training_days_share_the_same_ledger(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pub, _ = pubs.create(session, payload(run, 'pub-000005', epsilon=1.0, daily_cap=3.0))
    assert session.get(BudgetDay, ('川北', '2026-09-07')).used_micro == EPSILON_UNIT
    # 同一网点、同样的日期集合 → 每一天都共同扣账
    pub2, _ = pubs.create(session, payload(run, 'pub-000006', epsilon=1.0, daily_cap=3.0))
    assert session.get(BudgetDay, ('川北', '2026-09-07')).used_micro == 2 * EPSILON_UNIT
    pub3, _ = pubs.create(session, payload(run, 'pub-000007', epsilon=1.0, daily_cap=3.0))
    assert session.get(BudgetDay, ('川北', '2026-09-07')).used_micro == 3 * EPSILON_UNIT
    # 第三次后已达上限，下一次在扣账阶段拒绝，且不产生新发布或新明细
    entries = session.query(BudgetEntry).count()
    with pytest.raises(HTTPException) as err:
        pubs.create(session, payload(run, 'pub-000008', epsilon=1.0, daily_cap=3.0))
    assert err.value.status_code == 409
    assert session.query(Publication).filter_by(number='pub-000008').count() == 0
    assert session.query(BudgetEntry).count() == entries
    assert session.get(BudgetDay, ('川北', '2026-09-07')).used_micro == 3 * EPSILON_UNIT


def test_partial_overlap_days_are_each_charged(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run_a = make_run(session, days=70, start=date(2026, 9, 6))
    run_b = make_run(session, days=70, start=date(2026, 9, 6))  # 同一日期集合的另一版本
    pubs.create(session, payload(run_a, 'pub-000009', epsilon=0.5, daily_cap=1.0))
    pubs.create(session, payload(run_b, 'pub-000010', epsilon=0.5, daily_cap=1.0))
    day = session.get(BudgetDay, ('川北', '2026-10-01'))
    assert day.used_micro == EPSILON_UNIT  # 0.5+0.5，两个版本重叠在这一天
    with pytest.raises(HTTPException):
        pubs.create(session, payload(run_a, 'pub-000011', epsilon=0.5, daily_cap=1.0))


def test_partial_overlap_only_charges_shared_days(session, monkeypatch):
    """两个窗口只部分重叠：共同日期双扣，非共同日期各扣一次。"""
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run_a = make_run(session, days=70, start=date(2026, 9, 6))
    run_b = make_run(session, days=70, start=date(2026, 9, 16))
    pubs.create(session, payload(run_a, 'pub-000027', epsilon=0.5, daily_cap=2.0))
    pubs.create(session, payload(run_b, 'pub-000028', epsilon=0.5, daily_cap=2.0))
    shared = session.get(BudgetDay, ('川北', '2026-09-20'))
    only_a = session.get(BudgetDay, ('川北', '2026-09-08'))
    only_b = session.get(BudgetDay, ('川北', '2026-11-20'))
    assert shared.used_micro == EPSILON_UNIT            # 0.5 + 0.5
    assert only_a.used_micro == EPSILON_UNIT // 2       # 只被 A 覆盖
    assert only_b.used_micro == EPSILON_UNIT // 2       # 只被 B 覆盖


def test_budget_shortfall_is_rejected_and_leaves_no_partial_charge(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    # 上限只够两份 1.0，第三份必须在整笔层面失败而不是扣了一半
    pubs.create(session, payload(run, 'pub-000012', epsilon=1.0, daily_cap=2.0))
    pubs.create(session, payload(run, 'pub-000013', epsilon=1.0, daily_cap=2.0))
    snapshot = {d.day: d.used_micro for d in session.query(BudgetDay).all()}
    with pytest.raises(HTTPException) as err:
        pubs.create(session, payload(run, 'pub-000014', epsilon=1.0, daily_cap=2.0))
    assert err.value.status_code == 409
    after = {d.day: d.used_micro for d in session.query(BudgetDay).all()}
    assert after == snapshot
    assert session.query(Publication).filter_by(number='pub-000014').count() == 0


def test_concurrent_last_charge_never_overspends(session, monkeypatch):
    """两个线程在只剩一份预算时同时提交，只能有一个成功。"""
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pubs.create(session, payload(run, 'pub-000015', epsilon=1.0, daily_cap=2.0))
    session.commit()

    from src.config.database import Session
    barrier = threading.Barrier(2)
    results = []

    def attempt(number):
        with Session() as s:
            try:
                barrier.wait(timeout=5)
                pubs.create(s, payload(run, number, epsilon=1.0, daily_cap=2.0))
                results.append(('ok', number))
            except HTTPException as e:
                results.append((e.status_code, number))

    threads = [threading.Thread(target=attempt, args=('pub-0000%02d' % i,)) for i in (16, 17)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(str(r[0]) for r in results) == ['409', 'ok'], results
    assert session.get(BudgetDay, ('川北', '2026-09-06')).used_micro == 2 * EPSILON_UNIT
    assert session.query(Publication).count() == 2


def test_interruption_after_charge_recovers_without_new_charge(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: False)  # 模拟入队前中断
    run = make_run(session)
    pub, queued = pubs.create(session, payload(run, 'pub-000018'))
    assert queued is False and pub.state == 'pending'
    used = session.get(BudgetDay, ('川北', '2026-09-06')).used_micro
    entries = session.query(BudgetEntry).count()

    # 重启恢复：worker 把待处理重新取回并计算，预算不变
    drain(session, pub.id)
    session.refresh(pub)
    assert pub.state == 'claimable'
    assert session.get(BudgetDay, ('川北', '2026-09-06')).used_micro == used
    assert session.query(BudgetEntry).count() == entries

    # 同号重投，结果逐位一致
    before = copy.deepcopy(pub.result)
    matched, _ = pubs.create(session, payload(run, 'pub-000018'))
    drain(session, matched.id)
    session.refresh(pub)
    assert pub.result == before
    assert session.get(BudgetDay, ('川北', '2026-09-06')).used_micro == used


def test_custom_clip_upper_flows_into_mechanism_and_forecast(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pub, _ = pubs.create(session, payload(run, 'pub-000029', clip_upper=50))
    assert pub.clip_upper == 50
    drain(session, pub.id)
    session.refresh(pub)
    assert pub.state == 'claimable'
    assert pub.mechanism['clip_upper'] == 50
    from src.services.privacy import sensitivities
    tight = sensitivities(70, 0.6, 0.2, 50)
    assert pub.mechanism['output_sensitivity']['holt'] == pytest.approx(tight['holt'][1])
    # 更紧的裁剪 → 更小的灵敏度与噪声
    assert pub.mechanism['output_sensitivity']['holt'] < sensitivities(70, 0.6, 0.2)[ 'holt'][1]


def test_duplicate_queue_entries_do_not_double_process(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pub, _ = pubs.create(session, payload(run, 'pub-000030'))
    drain(session, pub.id)
    session.refresh(pub)
    first = copy.deepcopy(pub.result)
    attempts = pub.attempts
    # 队列里残留同一条目：再次取出时应被原子认领挡住
    drain(session, pub.id)
    session.refresh(pub)
    assert pub.result == first
    assert pub.attempts == attempts


def test_reprocessing_is_byte_identical(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pub, _ = pubs.create(session, payload(run, 'pub-000019'))
    drain(session, pub.id)
    session.refresh(pub)
    first = copy.deepcopy(pub.result)
    pub.state = 'pending'  # 模拟状态被回退后重算
    session.commit()
    drain(session, pub.id)
    session.refresh(pub)
    assert pub.state == 'claimable'
    assert pub.result == first


def test_withdraw_keeps_charge_and_blocks_replay(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pub, _ = pubs.create(session, payload(run, 'pub-000020'))
    drain(session, pub.id)
    used = session.get(BudgetDay, ('川北', '2026-09-06')).used_micro
    entries = session.query(BudgetEntry).count()

    withdrawn = pubs.withdraw(session, 'pub-000020')
    assert withdrawn.state == 'withdrawn'
    assert session.get(BudgetDay, ('川北', '2026-09-06')).used_micro == used
    assert session.query(BudgetEntry).count() == entries

    with pytest.raises(HTTPException) as err:
        pubs.create(session, payload(run, 'pub-000020'))
    assert err.value.status_code == 409
    # 撤回后不再带出预测
    assert 'days' not in pubs.public_view(withdrawn)


def test_withdrawn_release_cannot_be_claimed_by_route(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pub, _ = pubs.create(session, payload(run, 'pub-000021'))
    drain(session, pub.id)
    pubs.withdraw(session, 'pub-000021')
    session.refresh(pub)
    assert pub.state == 'withdrawn'
    assert 'days' not in pubs.public_view(pub)


def test_retry_after_failed_attempts_uses_same_number_once(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pub, _ = pubs.create(session, payload(run, 'pub-000022'))
    # 让第一次处理失败
    original = pubs.sensitivities
    monkeypatch.setattr(pubs, 'sensitivities', lambda *a: (_ for _ in ()).throw(RuntimeError('boom')))
    drain(session, pub.id)
    session.refresh(pub)
    assert pub.state == 'failed'
    assert pub.attempts == 1
    used = session.get(BudgetDay, ('川北', '2026-09-06')).used_micro

    monkeypatch.setattr(pubs, 'sensitivities', original)
    matched, _ = pubs.create(session, payload(run, 'pub-000022'))
    assert matched.id == pub.id
    assert session.get(BudgetDay, ('川北', '2026-09-06')).used_micro == used
    drain(session, matched.id)
    session.refresh(pub)
    assert pub.state == 'claimable'


def test_publication_requires_done_run_and_matching_branch(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    run.state = 'running'
    session.commit()
    with pytest.raises(HTTPException) as err:
        pubs.create(session, payload(run, 'pub-000023'))
    assert err.value.status_code == 409
    run.state = 'done'
    session.commit()
    bad = PublicationInput(number='pub-000024', branch='川南', run_id=run.id)
    with pytest.raises(HTTPException) as err:
        pubs.create(session, bad)
    assert err.value.status_code == 422


def test_epsilon_above_daily_cap_rejected(session, monkeypatch):
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    with pytest.raises(HTTPException) as err:
        pubs.create(session, payload(run, 'pub-000025', epsilon=2.0, daily_cap=1.0))
    assert err.value.status_code == 422
    assert session.query(Publication).count() == 0


def test_internal_predictions_and_staffing_remain(session, monkeypatch):
    """对外发布不影响内部预测与排班：原有 /runs 数据保持完整。"""
    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run = make_run(session)
    pub, _ = pubs.create(session, payload(run, 'pub-000026'))
    drain(session, pub.id)
    session.refresh(run)
    assert run.result['snapshot']
    assert run.state == 'done'
    assert all('volume' in r for r in run.result['snapshot'])
