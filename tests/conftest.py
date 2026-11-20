import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

_TMP = tempfile.mkdtemp(prefix='dp-tests-')
# 默认 SQLite；设置 TEST_DATABASE_URL 可在真实 PostgreSQL 上跑（例如并发末笔测试）。
os.environ['DATABASE_URL'] = os.environ.get(
    'TEST_DATABASE_URL', 'sqlite:///' + os.path.join(_TMP, 'test.db'))
os.environ.setdefault('JWT_SECRET', 'test-secret-not-for-production')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/15')

import pytest


@pytest.fixture()
def engine():
    from src.config.database import Base, engine as app_engine
    Base.metadata.drop_all(app_engine)
    Base.metadata.create_all(app_engine)
    return app_engine


@pytest.fixture()
def session(engine):
    from src.config.database import Session
    with Session() as s:
        yield s
