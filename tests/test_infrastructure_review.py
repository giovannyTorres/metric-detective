"""API responsiveness during bounded but CPU-heavy dataset validation."""

import asyncio
import time

import httpx

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
