"""对外发布的编排：幂等建单、逐日预算扣账、待处理→可领取、撤回。

预算模型
--------
* 每次发布声明一个 ε 和逐日累计上限 cap（均以 micro-ε 整数记账）。
* 发布覆盖冻结快照里的每一个训练日期；**重叠训练日共同扣账**，每遇到一个这样
  的日期就累加整份 ε。因此同一网点同一天被多份发布复用时，累计消耗受 cap 约束。
* 扣账是条件自增（``used + ε <= cap`` 才成功），在一个事务里按日期排序执行，
  并发请求不会超支，也不会互相死锁。
* 撤回只改状态，预算明细保留，**不退还**已领取的预算。
"""
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.constants.privacy import EPSILON_UNIT, MAX_ATTEMPTS
from src.constants.privacy import PUBLICATION_STATE_LABELS
from src.models.entities import BudgetDay, BudgetEntry, ForecastRun, Publication
from src.services.analysis import holt, seasonal
from src.services.privacy import (
    HORIZON, MODELS, clip, coordinate, mechanism_description, sensitivities, split_epsilon,
)

QUEUE = 'demand:publications'


def _now():
    return datetime.now(timezone.utc).isoformat()


def to_micro(value):
    """浮点 ε 转整数 micro-ε，四舍五入到最近单位，避免累加漂移。"""
    return int(round(float(value) * EPSILON_UNIT))


def _dialect_insert(s, table):
    name = s.bind.dialect.name
    if name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
        return insert(table)
    if name == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
        return insert(table)
    return None


def _ensure_budget_rows(s, branch, days):
    """为每个训练日补齐台账行（并发安全：冲突即忽略）。"""
    rows = [{'branch': branch, 'day': d, 'used_micro': 0} for d in days]
    ins = _dialect_insert(s, BudgetDay)
    if ins is not None:
        s.execute(ins.values(rows).on_conflict_do_nothing(index_elements=['branch', 'day']))
        return
    for row in rows:
        try:
            with s.begin_nested():
                s.execute(BudgetDay.__table__.insert().values(**row))
        except IntegrityError:
            pass


def charge(s, branch, days, epsilon_micro, cap_micro, number):
    """把整份 ε 扣到每个训练日；任一日超上限则整体回滚（由调用方事务负责）。"""
    if epsilon_micro <= 0:
        raise HTTPException(422, '每次发布预算必须为正')
    if epsilon_micro > cap_micro:
        raise HTTPException(422, '每次发布预算不能高于逐日累计上限')
    ordered = sorted(set(days))
    if not ordered:
        raise HTTPException(409, '冻结数据为空，无法确定扣账日期')
    _ensure_budget_rows(s, branch, ordered)
    for day in ordered:
        result = s.execute(
            update(BudgetDay)
            .where(BudgetDay.branch == branch, BudgetDay.day == day,
                   BudgetDay.used_micro + epsilon_micro <= cap_micro)
            .values(used_micro=BudgetDay.used_micro + epsilon_micro)
        )
        if result.rowcount != 1:
            raise HTTPException(
                409, f'预算不足：{branch} {day} 的逐日累计将超过上限 '
                     f'{cap_micro / EPSILON_UNIT:g} ε，发布已拒绝且未扣账')
        s.add(BudgetEntry(number=number, branch=branch, day=day,
                          epsilon_micro=epsilon_micro, created=_now()))


def request_fingerprint(data):
    payload = {
        'number': data.number, 'name': data.name, 'branch': data.branch,
        'run_id': data.run_id, 'epsilon_micro': to_micro(data.epsilon),
        'daily_cap_micro': to_micro(data.daily_cap), 'clip_upper': data.clip_upper,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _snapshot_days(run):
    rows = (run.result or {}).get('snapshot') or []
    days = sorted({r['day'] for r in rows})
    return rows, days


def enqueue(pub_id):
    """把发布入口放进队列；失败不抛错，记录保持待处理等 worker 恢复。"""
    try:
        import redis
        redis.Redis.from_url(os.environ['REDIS_URL']).rpush(QUEUE, pub_id)
        return True
    except Exception:
        return False


def _replay(s, pub, fingerprint):
    """同号重试：内容一致则复用原记录，绝不再次扣账。"""
    if pub.request_hash != fingerprint:
        raise HTTPException(409, '同一发布编号已提交过不同内容，请换编号或原样重试')
    if pub.state == 'withdrawn':
        raise HTTPException(409, '该发布已撤回，已领取预算不退还；如需重发请用新编号')
    if pub.state == 'failed':
        if (pub.attempts or 0) >= MAX_ATTEMPTS:
            raise HTTPException(409, f'该发布已失败 {pub.attempts} 次，请换编号重新提交')
        pub.state = 'pending'
        pub.error = ''
        pub.updated = _now()
        s.commit()
    queued = enqueue(pub.id)
    return pub, queued


def create(s, data, actor_email=None):
    eps = to_micro(data.epsilon)
    cap = to_micro(data.daily_cap)
    if eps > cap:
        raise HTTPException(422, '每次发布预算不能高于逐日累计上限')
    fingerprint = request_fingerprint(data)

    existing = s.execute(select(Publication).where(Publication.number == data.number)).scalar_one_or_none()
    if existing is not None:
        return _replay(s, existing, fingerprint)

    run = s.get(ForecastRun, data.run_id)
    if not run:
        raise HTTPException(404, '关联的预测版本不存在')
    if run.state != 'done':
        raise HTTPException(409, '仅成功完成的预测版本可以对外发布')
    if run.branch != data.branch:
        raise HTTPException(422, '发布网点与预测版本不一致')
    rows, days = _snapshot_days(run)
    if len(days) < 2 or len(days) != len(rows):
        raise HTTPException(409, '冻结快照不完整，不能发布')

    pub = Publication(
        number=data.number, name=data.name or run.name, branch=data.branch,
        run_id=run.id, epsilon_micro=eps, daily_cap_micro=cap,
        request_hash=fingerprint, state='pending', attempts=0, clip_upper=data.clip_upper,
        seed=secrets.token_hex(32), created=_now(), updated=_now(),
    )
    s.add(pub)
    try:
        s.flush()
    except IntegrityError:
        s.rollback()
        existing = s.execute(select(Publication).where(Publication.number == data.number)).scalar_one_or_none()
        if existing is not None:
            return _replay(s, existing, fingerprint)
        raise HTTPException(409, '发布编号冲突，请重试')

    try:
        charge(s, data.branch, days, eps, cap, data.number)
        s.commit()
    except Exception:
        s.rollback()
        raise

    if actor_email:
        from src.services.business import audit
        audit(s, type('U', (), {'email': actor_email})(), f'创建对外发布 {data.number}')
    queued = enqueue(pub.id)
    return pub, queued


def process(s, pub_id):
    """worker 入口：从冻结快照重算两法原始预测，加噪后置为可领取。

    中断可重入：噪声由服务端种子确定，重跑得到逐位一致的结果；预算早已扣账，
    这里不再触碰台账。
    """
    # 原子认领：只有仍待处理且未超尝试上限的一方拿到本次处理权，重复入队不会重算
    claimed = s.execute(
        update(Publication)
        .where(Publication.id == pub_id, Publication.state == 'pending',
               Publication.attempts < MAX_ATTEMPTS)
        .values(attempts=Publication.attempts + 1, updated=_now())
    ).rowcount
    s.commit()
    pub = s.get(Publication, pub_id)
    if claimed != 1:
        return pub
    try:
        run = s.get(ForecastRun, pub.run_id)
        if not run or run.state != 'done':
            raise ValueError('关联预测版本未成功')
        rows, days = _snapshot_days(run)
        if len(days) < 2 or len(days) != len(rows):
            raise ValueError('冻结快照不完整')
        ordered = sorted(rows, key=lambda r: r['day'])
        values = [clip(r['volume'], pub.clip_upper) for r in ordered]
        epsilon_total = pub.epsilon_micro / EPSILON_UNIT
        parts = split_epsilon(epsilon_total)
        sens = sensitivities(len(values), run.alpha, run.beta, pub.clip_upper)
        raw = {'holt': holt(values, HORIZON, run.alpha, run.beta),
               'seasonal_naive': seasonal(values, HORIZON)}
        seed = bytes.fromhex(pub.seed)
        forecasts = {}
        for model in MODELS:
            scale = sens[model][1] / parts[model]
            forecasts[model] = [coordinate(seed, model, i, raw[model][i], scale)
                                for i in range(HORIZON)]
        last = ordered[-1]['day']
        last_date = datetime.strptime(last, '%Y-%m-%d').date()
        forecast_days = [(last_date + timedelta(days=h)).isoformat() for h in range(1, HORIZON + 1)]
        pub.result = {'days': forecast_days, 'forecasts': forecasts}
        pub.mechanism = mechanism_description(
            len(values), run.alpha, run.beta, epsilon_total, run.branch,
            ordered[0]['day'], last,
            {'seasonal_naive': HORIZON, 'holt': HORIZON}, pub.clip_upper,
        )
        pub.mechanism['daily_cap'] = pub.daily_cap_micro / EPSILON_UNIT
        pub.mechanism['charged_days'] = len(days)
        pub.state = 'claimable'
        pub.error = ''
        pub.updated = _now()
        s.commit()
    except Exception as e:  # noqa: BLE001 - 失败原因写入记录供内部查看
        s.rollback()
        pub = s.get(Publication, pub_id)
        if pub:
            pub.state = 'failed'
            pub.error = str(e)[:300]
            pub.updated = _now()
            s.commit()
    return pub


def withdraw(s, number, actor_email=None):
    pub = s.execute(select(Publication).where(Publication.number == number)).scalar_one_or_none()
    if not pub:
        raise HTTPException(404, '发布记录不存在')
    if pub.state == 'withdrawn':
        return pub
    if pub.state not in ('pending', 'claimable', 'failed'):
        raise HTTPException(409, '当前状态不可撤回')
    pub.state = 'withdrawn'
    pub.updated = _now()
    s.commit()
    if actor_email:
        from src.services.business import audit
        audit(s, type('U', (), {'email': actor_email})(), f'撤回对外发布 {number}')
    return pub


def public_view(pub):
    """对外可见字段：编号、状态、七日整数预测、机制说明。不含原值/回测/种子。"""
    body = {'number': pub.number, 'branch': pub.branch, 'state': pub.state,
            'state_label': PUBLICATION_STATE_LABELS.get(pub.state, pub.state)}
    if pub.state == 'claimable' and pub.result:
        body['days'] = pub.result['days']
        body['forecasts'] = pub.result['forecasts']
        body['mechanism'] = pub.mechanism
    return body


def internal_view(pub):
    """内部列表视图：便于核对预算与状态，仍不含随机种子。"""
    body = public_view(pub)
    body.update({'id': pub.id, 'run_id': pub.run_id,
                 'epsilon': pub.epsilon_micro / EPSILON_UNIT,
                 'daily_cap': pub.daily_cap_micro / EPSILON_UNIT,
                 'attempts': pub.attempts, 'clip_upper': pub.clip_upper,
                 'created': pub.created, 'updated': pub.updated, 'error': pub.error})
    if pub.state == 'claimable' and pub.mechanism:
        body['charged_days'] = pub.mechanism.get('charged_days')
    return body
