"""Local FastAPI boundary: bounded uploads and durable asynchronous jobs."""

import json
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

import httpx
import psycopg
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from metric_detective import __version__
from metric_detective.contracts import InvestigationRequest, MetricConfig
from metric_detective.engine import AnalysisEngine, DataValidationError
from metric_detective.resources import load_recorded_report
from metric_detective.settings import Settings
from metric_detective.store import Store


class UploadLimitMiddleware:
    def __init__(self, app, maximum: int):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        maximum = self.maximum if scope["path"] == "/datasets" else 65536
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "invalid_content_length"}, 400)(
                scope, receive, send
            )
        if declared > maximum:
            return await JSONResponse({"detail": "request_too_large"}, 413)(scope, receive, send)
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > maximum:
                raise HTTPException(413, "request_too_large")
            return message

        await self.app(scope, bounded_receive, send)


def initialize_demo(store: Store, settings: Settings) -> list[dict]:
    from metric_detective.synthetic import (
        COMPARISON_START,
        REFERENCE_START,
        SCENARIOS,
        generate_all,
    )

    demo_root = settings.data_dir / "demo"
    # Never rewrite a snapshot while another worker may be reading it. Serialize
    # first-start seeding and expose the complete directory with one rename.
    with store.connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(124798238)")
        if not demo_root.exists():
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            staging = settings.data_dir / (".seed-" + str(uuid.uuid4()))
            generate_all(staging, seed=17)
            staging.rename(demo_root)
        manifest = []
        for scenario in SCENARIOS:
            folder = demo_root / scenario["id"]
            if not all(
                (folder / name).is_file() for name in ("orders.csv", "loads.csv", "config.json")
            ):
                raise RuntimeError("demo_dataset_incomplete")
            manifest.append(
                {
                    **scenario,
                    "config": json.loads((folder / "config.json").read_text(encoding="utf-8")),
                    "orders_path": str(folder / "orders.csv"),
                    "loads_path": str(folder / "loads.csv"),
                    "reference_start": REFERENCE_START.isoformat(),
                    "comparison_start": COMPARISON_START.isoformat(),
                }
            )
    scenarios = []
    for item in manifest:
        dataset_id = "demo-" + item["id"]
        store.register_dataset(
            dataset_id, item["orders_path"], item.get("loads_path"), item["config"], item["name"]
        )
        scenarios.append(
            {
                key: value
                for key, value in item.items()
                if key not in {"orders_path", "loads_path", "labels", "path"}
            }
            | {"dataset_id": dataset_id}
        )
    return scenarios


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or Store(settings)

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store.initialize()
        app.state.scenarios = initialize_demo(store, settings)
        yield

    app = FastAPI(title="Metric Detective", version=__version__, lifespan=lifespan)
    app.add_middleware(UploadLimitMiddleware, maximum=settings.max_upload_bytes + 65536)
    app.state.store = store

    @app.exception_handler(psycopg.Error)
    async def database_error(_request, _error):
        return JSONResponse({"detail": "database_unavailable"}, status_code=503)

    @app.get("/health")
    def health():
        store.healthy()
        return {"status": "ok", "database": "postgresql"}

    @app.get("/health/details")
    def health_details():
        database = {"status": "ok"}
        try:
            worker = store.worker_health()
        except psycopg.Error:
            database = {"status": "error"}
            worker = {"status": "missing", "active_workers": 0}
        model = os.getenv("MD_LLM_MODEL", "qwen3.5:4b")
        ollama = {"status": "unavailable", "model": model, "model_available": False}
        headers = (
            {"Authorization": "Bearer " + os.environ["MD_LLM_API_KEY"]}
            if os.getenv("MD_LLM_API_KEY")
            else {}
        )
        try:
            response = httpx.get(
                os.getenv("MD_OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/tags",
                headers=headers,
                timeout=2,
                trust_env=False,
            )
            response.raise_for_status()
            available = any(item.get("name") == model for item in response.json().get("models", []))
            ollama.update(status="ok" if available else "model_missing", model_available=available)
        except (httpx.HTTPError, ValueError):
            pass
        return {
            "status": "ok"
            if database["status"] == worker["status"] == ollama["status"] == "ok"
            else "degraded",
            "version": __version__,
            "database": database,
            "worker": worker,
            "ollama": ollama,
        }

    @app.get("/scenarios")
    def scenarios():
        return app.state.scenarios

    @app.post("/datasets", status_code=201)
    async def upload_dataset(
        orders: Annotated[UploadFile, File()],
        config: Annotated[str, Form()],
        loads: Annotated[UploadFile | None, File()] = None,
    ):
        try:
            metric_config = MetricConfig.model_validate_json(config)
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, "invalid_metric_configuration") from error
        dataset_id = str(uuid.uuid4())
        folder = settings.data_dir / dataset_id
        folder.mkdir()
        total = 0
        registered = False
        try:
            for upload, name in [(orders, "orders.csv"), (loads, "loads.csv")]:
                if upload is None:
                    continue
                with (folder / name).open("wb") as destination:
                    while chunk := await upload.read(65536):
                        total += len(chunk)
                        if total > settings.max_upload_bytes:
                            raise HTTPException(413, "files_too_large")
                        destination.write(chunk)
                await upload.close()
            request = InvestigationRequest(
                dataset_id=dataset_id,
                reference_start=date(2026, 1, 5),
                comparison_start=date(2026, 1, 12),
                config=metric_config,
            )

            # Constructing the engine validates the exact file contract. Quality
            # failures (e.g. duplicates) remain inspectable in a subsequent job.
            def validate_files():
                with AnalysisEngine(
                    folder / "orders.csv", folder / "loads.csv" if loads else None, request
                ) as engine:
                    engine.run("check_data_quality")

            await run_in_threadpool(validate_files)
            (folder / "config.json").write_text(
                metric_config.model_dump_json(indent=2), encoding="utf-8"
            )
            await run_in_threadpool(
                store.register_dataset,
                dataset_id,
                folder / "orders.csv",
                folder / "loads.csv" if loads else None,
                metric_config.model_dump(mode="json"),
                "Archivos cargados",
            )
            registered = True
        except HTTPException:
            raise
        except DataValidationError as error:
            raise HTTPException(
                422, {"code": "invalid_dataset_contract", "message": str(error)}
            ) from error
        except (ValueError, UnicodeError, OSError) as error:
            raise HTTPException(422, "invalid_dataset_contract") from error
        finally:
            for upload in (orders, loads):
                if upload is not None:
                    await upload.close()
            if not registered and folder.exists():
                if folder.resolve().parent != settings.data_dir.resolve():
                    raise RuntimeError("upload_cleanup_path_mismatch")
                shutil.rmtree(folder)
        return {"id": dataset_id, "config": metric_config.model_dump(mode="json")}

    @app.post("/investigations", status_code=202)
    def investigate(request: InvestigationRequest):
        dataset = store.dataset(request.dataset_id)
        if not dataset:
            raise HTTPException(404, "dataset_not_found")
        if request.config.model_dump(mode="json") != dataset["config"]:
            raise HTTPException(422, "configuration_must_match_validated_dataset")
        job = store.create_job(request.model_dump(mode="json"))
        return {"id": job["id"], "status": job["status"]}

    @app.get("/investigations")
    def history(limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return store.list_jobs(limit)

    @app.get("/investigations/{job_id}")
    def get_job(job_id: str):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, "investigation_not_found")
        return job

    def get_report(job_id):
        job = get_job(job_id)
        if not job["report"]:
            raise HTTPException(409, "report_not_ready")
        from metric_detective.reporting import validate_report

        validate_report(job["report"])
        return job["report"]

    @app.get("/investigations/{job_id}/report.json")
    def report_json(job_id: str):
        return JSONResponse(
            get_report(job_id), headers={"Content-Disposition": "attachment; filename=report.json"}
        )

    @app.get("/investigations/{job_id}/report.html", response_class=HTMLResponse)
    def report_html(job_id: str):
        from metric_detective.reporting import render_html

        return HTMLResponse(
            render_html(get_report(job_id)),
            headers={"Content-Disposition": "attachment; filename=report.html"},
        )

    @app.get("/replay")
    def replay():
        try:
            return load_recorded_report()
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "recorded_investigation_not_available") from None

    return app
