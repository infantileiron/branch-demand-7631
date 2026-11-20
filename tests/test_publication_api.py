"""HTTP 层测试：角色、状态流转、领取与导出不含敏感字段。"""
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from src.models.entities import DailyVolume, User
from src.utils.security import hash_password


class _FakeRedis:
    def __init__(self):
        self.items = []

    def rpush(self, queue, value):
        self.items.append((queue, value))
        return len(self.items)

    def blpop(self, *a, **k):
        return None

    def ping(self):
        return True


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    import redis
    import src.routes.api as api
    import src.services.publications as pubsvc
    stub = _FakeRedis()
    monkeypatch.setattr(redis.Redis, 'from_url', classmethod(lambda cls, *a, **k: stub))
    monkeypatch.setattr(api.redis.Redis, 'from_url', lambda *a, **k: stub)
    monkeypatch.setattr(pubsvc.os, 'environ', {**pubsvc.os.environ})
    return stub


@pytest.fixture()
def client(engine):
    from src.main import app
    from src.config.database import Session
    with Session() as s:
        for role in ('admin', 'analyst', 'viewer'):
            s.add(User(email=role + '@t.local', role=role, password=hash_password('pw')))
        for i in range(70):
            s.add(DailyVolume(branch='川北', day=(date(2026, 9, 6) + timedelta(days=i)).isoformat(),
                              volume=40 + i % 9, note='test'))
        s.commit()
    with TestClient(app) as c:
        yield c


def token(client, role):
    r = client.post('/auth/login', json={'email': role + '@t.local', 'password': 'pw'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


def make_done_run(client, headers):
    r = client.post('/runs', json={'name': '对外发布用预测', 'branch': '川北',
                                   'backtest_days': 14, 'alpha': 0.6, 'beta': 0.2},
                    headers=headers)
    assert r.status_code == 200, r.text
    run_id = r.json()['id']
    from src.config.database import Session
    from src.models.entities import ForecastRun
    from src.services import publications as pubs
    from src.services.analysis import forecast
    with Session() as s:
        run = s.get(ForecastRun, run_id)
        run.result = {**forecast(run.result['snapshot'], 14, 0.6, 0.2), 'snapshot': run.result['snapshot']}
        run.state = 'done'
        s.commit()
    return run_id


def test_viewer_cannot_publish_and_analyst_can(client):
    viewer = token(client, 'viewer')
    run_id = make_done_run(client, token(client, 'analyst'))
    body = {'number': 'pub-api-001', 'branch': '川北', 'run_id': run_id, 'epsilon': 1, 'daily_cap': 3}
    r = client.post('/publications', json=body, headers=viewer)
    assert r.status_code == 403
    r = client.post('/publications', json=body, headers=token(client, 'analyst'))
    assert r.status_code == 200, r.text
    assert r.json()['state'] == 'pending'


def test_pending_cannot_be_claimed_then_claimable_can(client):
    analyst = token(client, 'analyst')
    run_id = make_done_run(client, analyst)
    body = {'number': 'pub-api-002', 'branch': '川北', 'run_id': run_id, 'epsilon': 1, 'daily_cap': 3}
    client.post('/publications', json=body, headers=analyst)
    r = client.get('/publications/pub-api-002/claim', headers=token(client, 'viewer'))
    assert r.status_code == 409

    from src.config.database import Session
    from src.services import publications as pubs
    with Session() as s:
        pub = s.query(pubs.Publication).filter_by(number='pub-api-002').one()
        pubs.process(s, pub.id)

    viewer = token(client, 'viewer')
    r = client.get('/publications/pub-api-002/claim', headers=viewer)
    assert r.status_code == 200
    data = r.json()
    text = json.dumps(data, ensure_ascii=False)
    for banned in ('"seed"', '"snapshot"', '"actual"', '"backtest"', '"volume"'):
        assert banned not in text
    assert set(data['forecasts']) == {'seasonal_naive', 'holt'}
    assert all(isinstance(v, int) and v >= 0 for m in data['forecasts'].values() for v in m)

    export = client.get('/publications/pub-api-002/export', headers=viewer)
    assert export.status_code == 200
    assert 'attachment; filename="publication-pub-api-002.json"' in export.headers['content-disposition']
    assert '"seed"' not in export.text


def test_viewer_list_hides_internal_fields(client):
    analyst = token(client, 'analyst')
    run_id = make_done_run(client, analyst)
    client.post('/publications', json={'number': 'pub-api-003', 'branch': '川北', 'run_id': run_id,
                                        'epsilon': 1, 'daily_cap': 3}, headers=analyst)
    viewer_rows = client.get('/publications', headers=token(client, 'viewer')).json()
    analyst_rows = client.get('/publications', headers=token(client, 'analyst')).json()
    assert 'id' not in viewer_rows[0] and 'epsilon' not in viewer_rows[0]
    assert 'id' in analyst_rows[0] and 'epsilon' in analyst_rows[0]
    assert 'seed' not in analyst_rows[0]


def test_budget_endpoint_requires_admin_and_reports_charge(client):
    analyst = token(client, 'analyst')
    run_id = make_done_run(client, analyst)
    client.post('/publications', json={'number': 'pub-api-004', 'branch': '川北', 'run_id': run_id,
                                        'epsilon': 1, 'daily_cap': 3}, headers=analyst)
    assert client.get('/budget?branch=川北', headers=analyst).status_code == 403
    rows = client.get('/budget?branch=川北', headers=token(client, 'admin')).json()
    assert rows and all(r['used'] == 1.0 for r in rows)


def test_duplicate_number_returns_same_record_and_single_charge(client):
    analyst = token(client, 'analyst')
    run_id = make_done_run(client, analyst)
    body = {'number': 'pub-api-005', 'branch': '川北', 'run_id': run_id, 'epsilon': 1, 'daily_cap': 3}
    first = client.post('/publications', json=body, headers=analyst)
    second = client.post('/publications', json=body, headers=analyst)
    assert first.status_code == second.status_code == 200
    assert second.json()['number'] == 'pub-api-005'
    rows = client.get('/budget?branch=川北', headers=token(client, 'admin')).json()
    assert all(r['used'] == 1.0 for r in rows)

    changed = dict(body, epsilon=2)
    conflict = client.post('/publications', json=changed, headers=analyst)
    assert conflict.status_code == 409


def test_insufficient_budget_rejects_release(client):
    analyst = token(client, 'analyst')
    run_id = make_done_run(client, analyst)
    for i in range(3):
        r = client.post('/publications', json={'number': f'pub-api-10{i}', 'branch': '川北',
                                               'run_id': run_id, 'epsilon': 1, 'daily_cap': 3},
                        headers=analyst)
        assert r.status_code == 200, r.text
    overflow = client.post('/publications', json={'number': 'pub-api-104', 'branch': '川北',
                                                  'run_id': run_id, 'epsilon': 1, 'daily_cap': 3},
                           headers=analyst)
    assert overflow.status_code == 409
    assert '预算不足' in overflow.json()['detail']
    assert client.get('/publications/pub-api-104', headers=analyst).status_code == 404


def test_withdraw_endpoint_keeps_budget_and_blocks_claim(client):
    analyst = token(client, 'analyst')
    admin = token(client, 'admin')
    run_id = make_done_run(client, analyst)
    client.post('/publications', json={'number': 'pub-api-006', 'branch': '川北', 'run_id': run_id,
                                        'epsilon': 1, 'daily_cap': 3}, headers=analyst)
    from src.config.database import Session
    from src.services import publications as pubs
    with Session() as s:
        pubs.process(s, s.query(pubs.Publication).filter_by(number='pub-api-006').one().id)
    r = client.post('/publications/pub-api-006/withdraw', headers=analyst)
    assert r.status_code == 200 and r.json()['state'] == 'withdrawn'
    assert client.get('/publications/pub-api-006/claim', headers=admin).status_code == 409
    assert client.get('/publications/pub-api-006/export', headers=admin).status_code == 409
    rows = client.get('/budget?branch=川北', headers=admin).json()
    assert rows and all(x['used'] == 1.0 for x in rows)
