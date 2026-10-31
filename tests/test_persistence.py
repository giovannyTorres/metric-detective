"""These tests use a real PostgreSQL server and real operating-system process death."""

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from metric_detective.store import LostLeaseError
from metric_detective.worker import process_one

pytestmark = pytest.mark.postgres


def test_concurrent_claims_and_advisory_lock(pg_store, demo_job):
    with ThreadPoolExecutor(max_workers=6) as pool:
        leases = list(pool.map(lambda _: pg_store.claim(), range(6)))
    won = [lease for lease in leases if lease is not None]
    assert len(won) == 1
    lease = won[0]
    try:
        # Even an expired lease cannot steal a still-live session's advisory lock.
        with pg_store.connect() as connection:
            connection.execute(
                "UPDATE md_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                (demo_job["id"],),
            )
        assert pg_store.claim() is None
    finally:
        lease.close()
    recovered = pg_store.claim()
    assert recovered is not None
    assert recovered.owner != lease.owner
    recovered.close()


def test_fenced_checkpoint_and_evidence_upsert(pg_store, demo_job):
    lease = pg_store.claim()
    state = {"phase": "compare", "evidence": [{"id": "ev-1", "value": "1"}]}
    try:
        pg_store.checkpoint(demo_job["id"], lease.owner, state)
        pg_store.checkpoint(demo_job["id"], lease.owner, state)
        with pytest.raises(LostLeaseError):
            pg_store.checkpoint(demo_job["id"], "stale-owner", state)
        with pg_store.connect() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) AS n FROM md_evidence WHERE job_id=%s", (demo_job["id"],)
                ).fetchone()["n"]
                == 1
            )
    finally:
        lease.close()


def test_worker_completes_persisted_report(pg_store, demo_job):
    assert process_one(pg_store)
    job = pg_store.get_job(demo_job["id"])
    assert job["status"] == "completed", job
    assert job["report"]["evidence"]
    assert job["attempts"] == 1
    assert not process_one(pg_store)


def test_real_process_kill_and_recovery(pg_store, demo_job):
    code = """
import time
from metric_detective.store import Store
from metric_detective.settings import Settings
from metric_detective.worker import process_one
def pause(state):
    if len(state.get('evidence', [])) >= 2:
        time.sleep(120)
process_one(Store(Settings.from_env()), checkpoint_observer=pause)
"""
    environment = os.environ.copy()
    environment.update(
        {
            "MD_DATABASE_URL": pg_store.settings.database_url,
            "MD_DATA_DIR": str(pg_store.settings.data_dir),
            "MD_LEASE_SECONDS": "3",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    old_ids = set()
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline:
            with pg_store.connect() as connection:
                checkpoint = connection.execute(
                    "SELECT checkpoint FROM md_jobs WHERE id=%s", (demo_job["id"],)
                ).fetchone()["checkpoint"]
            if checkpoint and len(checkpoint.get("evidence", [])) >= 2:
                evidence = checkpoint["evidence"]
                old_ids = (
                    set(evidence)
                    if isinstance(evidence, dict)
                    else {item["id"] for item in evidence}
                )
                break
            assert process.poll() is None, "Worker exited before the persisted interruption point"
            time.sleep(0.05)
        assert old_ids, "No committed checkpoint before timeout"
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    # Recovery occurs after a real lease expiry, without editing the saved checkpoint.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process_one(pg_store):
            break
        time.sleep(0.1)
    job = pg_store.get_job(demo_job["id"])
    assert job["status"] == "completed", job
    assert job["attempts"] == 2
    ids = [item["id"] for item in job["report"]["evidence"]]
    assert old_ids <= set(ids)
    assert len(ids) == len(set(ids))
    with pg_store.connect() as connection:
        rows = connection.execute(
            "SELECT evidence_id FROM md_evidence WHERE job_id=%s", (demo_job["id"],)
        ).fetchall()
    assert {row["evidence_id"] for row in rows} == set(ids)
