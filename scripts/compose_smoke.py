"""Credential-free smoke: real API, PostgreSQL worker and deterministic baseline."""

import json
import time
import urllib.request


def get(path):
    with urllib.request.urlopen("http://localhost:8000" + path, timeout=5) as response:
        return json.load(response)


scenario = get("/scenarios")[0]
body = {
    key: scenario[key] for key in ("dataset_id", "reference_start", "comparison_start", "config")
}
body["mode"] = "baseline"
request = urllib.request.Request(
    "http://localhost:8000/investigations",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=5) as response:
    job_id = json.load(response)["id"]
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    job = get(f"/investigations/{job_id}")
    if job["status"] not in {"queued", "running"}:
        assert job["status"] == "completed", job
        assert job["report"]["evidence"]
        print("Compose baseline investigation completed with persisted evidence.")
        break
    time.sleep(1)
else:
    raise TimeoutError("Compose job did not finish")
with urllib.request.urlopen("http://localhost:8501/_stcore/health", timeout=5) as response:
    assert response.status == 200
