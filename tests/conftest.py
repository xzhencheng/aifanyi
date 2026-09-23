import json
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from aifanyi.workflow import checkpointer

from aifanyi.api import create_app
from aifanyi.cli import initialize
from aifanyi.config import Settings
from aifanyi.db import Base, Database, Record
from aifanyi.errors import DomainError
from aifanyi.schemas import Plan, Repair, Review
from aifanyi.store import patch
from aifanyi.worker import Runner


class FakeGateway:
    """Test-only controlled provider; never installed as an application model route."""

    def __init__(self):
        self.calls = []
        self.translation = "The device is in standby mode."
        self.reviews = []
        self.plan = None
        self.failure_role = None

    def generate(self, config, role, messages, execution, budgets, schema=None, validate=None, limit=12):
        self.calls.append(role)
        if role == self.failure_role:
            raise DomainError("MODEL_UNAVAILABLE", "Injected provider outage", 503)
        if schema is None:
            return self.translation
        payload = json.loads(messages[-1]["content"])
        if schema == Review:
            value = (
                self.reviews.pop(0)
                if self.reviews
                else {
                    "reviewed_segment_ids": [x["segment_id"] for x in payload["segments"]],
                    "issues": [],
                    "style_suggestions": [],
                }
            )
            if callable(value):
                value = value(payload)
            value = Review.model_validate(value)
        elif schema == Repair:
            value = Repair(
                segment_id=payload["segment_id"],
                text="The device is in standby mode.",
                issue_refs=[x["id"] for x in payload["issues"]],
            )
        elif schema == Plan:
            value = Plan.model_validate(self.plan or {"intent": "clarify", "question": "请提供目标语言。"})
        elif schema.__name__ == "Detection":
            value = schema(language="zh-CN")
        else:
            value = schema(text="基于原文的译法说明。", evidence_refs=[])
        if validate:
            validate(value)
        return value


@pytest.fixture
def env(tmp_path):
    database_url = f"sqlite:///{tmp_path}/test.db"
    checkpoint_url = f"sqlite:///{tmp_path}/checkpoints.db"
    admin_engine, schema = None, None
    if os.environ.get("AIFANYI_TEST_DATABASE_URL"):
        base_url = make_url(os.environ["AIFANYI_TEST_DATABASE_URL"])
        assert base_url.drivername == "postgresql+psycopg"
        schema = "test_" + uuid4().hex
        admin_engine = create_engine(base_url)
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = base_url.update_query_dict({"options": f"-csearch_path={schema}"})
        database_url = scoped.render_as_string(hide_password=False)
        checkpoint_url = database_url
    settings = Settings(
        environment="test",
        database_url=database_url,
        checkpoint_url=checkpoint_url,
        dev_auth=True,
        dev_token="test-token-only-abcdefghijklmnopqrstuvwxyz",
        sync_wait_seconds=0.01,
        hymt_base_url="http://models.test/v1",
        qwen_base_url="http://qwen.test/v1",
        qwen_model="qwen-test",
    )
    db = Database(settings.database_url)
    Base.metadata.create_all(db.engine)
    initialize(db, settings)
    with checkpointer(settings) as cp:
        if hasattr(cp, "setup"):
            cp.setup()
    with db.session() as s:
        patch(s.get(Record, "profile_default"), status="validated")
    gateway = FakeGateway()
    client = TestClient(create_app(settings, db, gateway))
    client.headers["Authorization"] = "Bearer " + settings.dev_token
    runner = Runner(db, settings, gateway)
    yield client, runner, gateway, db, settings
    client.close()
    db.engine.dispose()
    if admin_engine is not None:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def post(client, path, data, key=None):
    return client.post(path, json=data, headers={"Idempotency-Key": key or uuid4().hex})


def job_request(text="设备处于待机状态。", **kwargs):
    return {
        "project_id": "project_default",
        "source_language": "zh-CN",
        "target_language": "en",
        "input": {"kind": "text", "text": text},
        **kwargs,
    }
