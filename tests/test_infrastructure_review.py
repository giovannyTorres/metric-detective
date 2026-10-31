"""API responsiveness during bounded but CPU-heavy dataset validation."""

import asyncio
import time

import httpx
import psycopg
import pytest

from metric_detective import api
from metric_detective.contracts import MetricConfig
from metric_detective.settings import Settings


def test_upload_validation_does_not_block_health_or_job_polling(tmp_path, monkeypatch):
    class LocalStore:
        def healthy(self):
            return True

        def register_dataset(self, *_args):
            pass

    class SlowEngine:
        def __init__(self, *_args):
            # Stand in for parsing and inserting a valid, bounded large CSV.
            # This test uses no database and no provider; delay is explicit.
            time.sleep(0.4)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def run(self, *_args):
            return None

    monkeypatch.setattr(api, "AnalysisEngine", SlowEngine)
    app = api.create_app(Settings(database_url="unused", data_dir=tmp_path), LocalStore())

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            started = time.monotonic()

            async def probe():
                await asyncio.sleep(0.02)
                response = await client.get("/health")
                return response.status_code, time.monotonic() - started

            upload = client.post(
                "/datasets",
                data={"config": MetricConfig().model_dump_json()},
                files={"orders": ("orders.csv", b"fixture", "text/csv")},
            )
            response, (health_status, health_elapsed) = await asyncio.gather(upload, probe())
            assert response.status_code == 201
            assert health_status == 200
            assert health_elapsed < 0.3, (
                "Synchronous upload validation blocked status/health routes"
            )

    asyncio.run(exercise())


def test_failed_dataset_registration_cleans_files_and_sanitizes_error(tmp_path, monkeypatch):
    class FailingStore:
        def register_dataset(self, *_args):
            raise psycopg.OperationalError("PRIVATE-credential in a simulated database error")

    class ValidEngine:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def run(self, *_args):
            return None

    monkeypatch.setattr(api, "AnalysisEngine", ValidEngine)
    app = api.create_app(Settings(database_url="unused", data_dir=tmp_path), FailingStore())

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/datasets",
                data={"config": MetricConfig().model_dump_json()},
                files={"orders": ("orders.csv", b"synthetic fixture", "text/csv")},
            )
        assert response.status_code == 503
        assert response.json() == {"detail": "database_unavailable"}
        assert "PRIVATE" not in response.text
        assert list(tmp_path.iterdir()) == [], (
            "Failed registration retained untracked uploaded data"
        )

    asyncio.run(exercise())


def test_cancelled_dataset_upload_closes_file_and_cleans_partial_snapshot(tmp_path):
    class CancelledUpload:
        closed = False

        async def read(self, _size):
            raise asyncio.CancelledError("Explicit cancellation test double")

        async def close(self):
            self.closed = True

    app = api.create_app(Settings(database_url="unused", data_dir=tmp_path), object())
    endpoint = next(
        route.endpoint for route in app.routes if getattr(route, "path", None) == "/datasets"
    )
    upload = CancelledUpload()

    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            await endpoint(orders=upload, config=MetricConfig().model_dump_json(), loads=None)
        assert upload.closed
        assert list(tmp_path.iterdir()) == [], "Cancellation retained a partial uploaded snapshot"

    asyncio.run(exercise())
