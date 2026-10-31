import json

import pytest
from fastapi.testclient import TestClient

from metric_detective.api import create_app
from metric_detective.worker import process_one

pytestmark = pytest.mark.postgres


def test_async_routes_exports_and_validation(pg_store):
    with TestClient(create_app(pg_store.settings, pg_store)) as client:
        assert client.get("/health").json()["database"] == "postgresql"
        scenario = client.get("/scenarios").json()[0]
        assert "path" not in scenario
        body = {
            key: scenario[key]
            for key in ("dataset_id", "reference_start", "comparison_start", "config")
        }
        body["mode"] = "baseline"
        response = client.post("/investigations", json=body)
        assert response.status_code == 202
        job_id = response.json()["id"]
        assert client.get(f"/investigations/{job_id}").json()["status"] == "queued"
        assert client.get(f"/investigations/{job_id}/report.json").status_code == 409
        assert process_one(pg_store)
        assert client.get(f"/investigations/{job_id}").json()["status"] == "completed"
        assert client.get(f"/investigations/{job_id}/report.json").json()["evidence"]
        assert "<html" in client.get(f"/investigations/{job_id}/report.html").text.lower()
        assert client.get("/investigations").json()[0]["id"] == job_id
        assert client.get("/investigations/missing").status_code == 404
        body["comparison_start"] = body["reference_start"]
        assert client.post("/investigations", json=body).status_code == 422


def test_upload_safe_names_schema_and_missing_loads(pg_store):
    with TestClient(create_app(pg_store.settings, pg_store)) as client:
        scenario = client.get("/scenarios").json()[0]
        dataset = pg_store.dataset(scenario["dataset_id"])
        data = open(dataset["orders_path"], "rb").read()
        response = client.post(
            "/datasets",
            data={"config": json.dumps(scenario["config"])},
            files={"orders": ("../../escape.csv", data, "text/csv")},
        )
        assert response.status_code == 201, response.text
        stored = pg_store.dataset(response.json()["id"])
        assert stored["orders_path"].endswith("orders.csv")
        assert stored["loads_path"] is None
        body = {key: scenario[key] for key in ("reference_start", "comparison_start", "config")}
        body.update({"dataset_id": response.json()["id"], "mode": "baseline"})
        job_id = client.post("/investigations", json=body).json()["id"]
        process_one(pg_store)
        assert client.get(f"/investigations/{job_id}").json()["status"] == "partial"
        assert (
            client.post(
                "/datasets", data={"config": "{}"}, files={"orders": ("x.csv", b"wrong\n1\n")}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/datasets", data={"config": "{"}, files={"orders": ("x.csv", b"wrong\n1\n")}
            ).status_code
            == 422
        )


def test_total_upload_limit(pg_store):
    from dataclasses import replace

    settings = replace(pg_store.settings, max_upload_bytes=1024)
    with TestClient(create_app(settings, pg_store)) as client:
        response = client.post(
            "/datasets", data={"config": "{}"}, files={"orders": ("large.csv", b"x" * 2048)}
        )
        assert response.status_code == 413
        response = client.post(
            "/datasets",
            content=b"x" * 70000,
            headers={"Content-Type": "multipart/form-data; boundary=test"},
        )
        assert response.status_code == 413
