import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from deep_agents_app.config import Settings
from deep_agents_app.db.engine import make_engine
from deep_agents_app.db.migrate import migrate
from deep_agents_app.main import create_app
from deep_agents_app.runs.store import RunStore


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set (needs a throwaway Postgres)")
    return url


def make_settings(database_url: str, schema: str, data_dir, **overrides) -> Settings:
    values = dict(
        database_url=database_url,
        database_schema=schema,
        data_dir=data_dir,
        default_model="gw-model",
        llm_gateway_url="http://litellm.test/v1",
        llm_api_key="test-key",
        app_language="en",
        run_mode="thread",  # fast in-process runs; process mode has its own tests
    )
    return Settings(_env_file=None, **{**values, **overrides})  # type: ignore[arg-type]


@pytest.fixture
def settings(database_url, tmp_path):
    schema = f"t_{uuid.uuid4().hex[:12]}"
    yield make_settings(database_url, schema, tmp_path)
    engine = create_engine(Settings(_env_file=None, database_url=database_url).sqlalchemy_url)  # type: ignore[call-arg]
    with engine.begin() as db:
        db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


@pytest.fixture
def store(settings):
    engine = make_engine(settings)
    migrate(settings, engine)
    yield RunStore(engine, settings)
    engine.dispose()


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def wait_for_status(client: TestClient, run_id: str, wanted=("completed", "failed", "cancelled"), seconds: float = 30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in wanted:
            return run
        time.sleep(0.2)
    raise AssertionError(f"run did not finish: {run}")
