"""Runtime diagnostics are observations; no model/database health is fabricated."""

import httpx
import psycopg
import pytest

from metric_detective.api import create_app
from metric_detective.settings import Settings


def test_diagnostics_distinguishes_missing_worker_and_model(tmp_path, monkeypatch):
    class StoreDouble:
        def worker_health(self):
            return {"status": "missing", "active_workers": 0, "last_seen_at": None}

    def tags(*_args, **_kwargs):
        return httpx.Response(
            200, json={"models": []}, request=httpx.Request("GET", "http://ollama.test/api/tags")
        )

    monkeypatch.setattr(httpx, "get", tags)
    from fastapi.testclient import TestClient

    client = TestClient(create_app(Settings("unused", tmp_path), StoreDouble()))
    details = client.get("/health/details").json()
    assert details["status"] == "degraded"
    assert details["worker"]["status"] == "missing"
    assert details["ollama"]["status"] == "model_missing"
    assert not details["ollama"]["model_available"]


def test_diagnostics_sanitizes_database_and_provider_failures(tmp_path, monkeypatch):
    class BrokenStore:
        def worker_health(self):
            raise psycopg.OperationalError("PRIVATE credential fixture")

    def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("PRIVATE bearer fixture")

    monkeypatch.setattr(httpx, "get", unavailable)
    from fastapi.testclient import TestClient

    client = TestClient(create_app(Settings("unused", tmp_path), BrokenStore()))
    response = client.get("/health/details")
    assert "PRIVATE" not in response.text
    details = response.json()
    assert details["database"]["status"] == "error"
    assert details["ollama"]["status"] == "unavailable"


@pytest.mark.postgres
def test_live_worker_presence_expires_and_can_be_removed(pg_store):
    assert pg_store.worker_health()["status"] == "missing"
    pg_store.touch_worker("explicit-test-worker")
    assert pg_store.worker_health()["active_workers"] == 1
    with pg_store.connect() as connection:
        connection.execute(
            "UPDATE md_workers SET last_seen_at=clock_timestamp()-interval '21 seconds'"
        )
    assert pg_store.worker_health()["status"] == "missing"
    pg_store.touch_worker("explicit-test-worker")
    pg_store.forget_worker("explicit-test-worker")
    assert pg_store.worker_health()["active_workers"] == 0
