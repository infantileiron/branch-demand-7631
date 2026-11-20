"""真实 PostgreSQL 上的并发预算测试：多线程争抢同一批训练日的最后预算。

仅在设置了 TEST_DATABASE_URL 指向 PostgreSQL 时运行；SQLite 的写锁会掩盖
并发扣账的竞态，因此这里单独标记。
"""
import os
import threading
from datetime import date, timedelta

import pytest

PG = os.environ.get('TEST_DATABASE_URL', '').startswith('postgresql')
pytestmark = pytest.mark.skipif(not PG, reason='需要真实 PostgreSQL')


@pytest.fixture(autouse=True)
def clean(engine):
    from src.config.database import Session
    from src.models.entities import (BudgetDay, BudgetEntry, DailyVolume, ForecastRun,
                                     Publication, Staffing)
    with Session() as s:
        for model in (BudgetEntry, BudgetDay, Publication, Staffing, ForecastRun, DailyVolume):
            s.query(model).delete()
        s.commit()
    for i in range(70):
        with Session() as s:
            s.add(DailyVolume(branch='川北', day=(date(2026, 9, 6) + timedelta(days=i)).isoformat(),
                              volume=40 + i % 7, note=''))
            s.commit()


def _make_run():
    from src.config.database import Session
    from src.models.entities import ForecastRun
    with Session() as s:
        run = ForecastRun(name='并发预测', branch='川北', backtest_days=14, alpha=0.6,
                          beta=0.2, state='done')
        run.result = {'snapshot': [{'id': i, 'branch': '川北',
                                    'day': (date(2026, 9, 6) + timedelta(days=i)).isoformat(),
                                    'volume': 40 + i % 7, 'note': ''} for i in range(70)]}
        s.add(run)
        s.commit()
        return run.id


def test_parallel_last_charge_has_exactly_one_winner(monkeypatch):
    from src.config.database import Session
    from src.models.entities import BudgetDay, BudgetEntry, Publication
    from src.services import publications as pubs
    from src.schemas.publications import PublicationInput
    from src.constants.privacy import EPSILON_UNIT

    monkeypatch.setattr(pubs, 'enqueue', lambda pid: True)
    run_id = _make_run()
    # 每秒日上限只够 2 份 1.0；先占 1 份，剩 1 份给 8 个并发请求抢
    with Session() as s:
        pubs.create(s, PublicationInput(number='pg-seed-001', branch='川北', run_id=run_id,
                                        epsilon=1.0, daily_cap=2.0))

    barrier = threading.Barrier(8)
    results = []

    def worker(i):
        with Session() as s:
            try:
                barrier.wait(timeout=10)
                pubs.create(s, PublicationInput(number=f'pg-race-{i:03d}', branch='川北',
                                                run_id=run_id, epsilon=1.0, daily_cap=2.0))
                results.append('ok')
            except Exception as e:  # HTTPException(409) 是预期失败
                results.append(getattr(e, 'status_code', repr(e)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count('ok') == 1, results
    assert results.count(409) == 7, results
    with Session() as s:
        assert s.get(BudgetDay, ('川北', '2026-09-06')).used_micro == 2 * EPSILON_UNIT
        assert s.query(Publication).count() == 2
        # 每条明细都真实对应一份成功发布，且没有超上限
        per_day = {}
        for e in s.query(BudgetEntry).all():
            per_day[e.day] = per_day.get(e.day, 0) + e.epsilon_micro
        assert set(per_day.values()) == {2 * EPSILON_UNIT}
