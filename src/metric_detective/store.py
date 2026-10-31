"""PostgreSQL job queue, fenced writes, and idempotent evidence persistence.

A session advisory lock stays held for the lifetime of processing, in addition
to the expiring lease. Two live database sessions cannot execute the same job.
After process death PostgreSQL releases the lock; the lease permits recovery.
"""

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from metric_detective.settings import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS md_datasets (
    id TEXT PRIMARY KEY, orders_path TEXT NOT NULL, loads_path TEXT,
    config JSONB NOT NULL, name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS md_jobs (
    id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES md_datasets(id),
    request JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'queued'
      CHECK(status IN ('queued','running','completed','partial','blocked_data_quality','failed')),
    checkpoint JSONB, report JSONB, progress JSONB NOT NULL DEFAULT '{}',
    lease_owner TEXT, lease_until TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS md_jobs_claim ON md_jobs(status, lease_until, created_at);
CREATE TABLE IF NOT EXISTS md_evidence (
    job_id TEXT NOT NULL REFERENCES md_jobs(id), evidence_id TEXT NOT NULL,
    result JSONB NOT NULL, PRIMARY KEY(job_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS md_workers (
    id TEXT PRIMARY KEY, last_seen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
"""


class LostLeaseError(RuntimeError):
    """A superseded worker must stop without writing any more state."""


def public_job(row: dict) -> dict:
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else value
        for key, value in row.items()
        if key not in {"lease_owner", "lease_until", "checkpoint"}
    }


@dataclass
class Lease:
    job: dict
    owner: str
    connection: Any

    def close(self):
        # Closing a session releases every session advisory lock it holds.
        self.connection.close()


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def connect(self):
        with psycopg.connect(
            self.settings.database_url, connect_timeout=5, row_factory=dict_row
        ) as connection:
            yield connection

    def initialize(self):
        with self.connect() as connection:
            # Serialize first-start DDL across API and workers.
            connection.execute("SELECT pg_advisory_xact_lock(124798237)")
            connection.execute(SCHEMA)

    def healthy(self) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 AS ok").fetchone()["ok"] == 1

    def touch_worker(self, worker_id: str):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO md_workers(id,last_seen_at) VALUES(%s,clock_timestamp()) ON CONFLICT(id) DO UPDATE SET last_seen_at=EXCLUDED.last_seen_at",
                (worker_id,),
            )

    def forget_worker(self, worker_id: str):
        with self.connect() as connection:
            connection.execute("DELETE FROM md_workers WHERE id=%s", (worker_id,))

    def worker_health(self) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS active_workers,MAX(last_seen_at) AS last_seen_at FROM md_workers WHERE last_seen_at > clock_timestamp()-interval '20 seconds'"
            ).fetchone()
        return {
            "status": "ok" if row["active_workers"] else "missing",
            "active_workers": row["active_workers"],
            "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        }

    def register_dataset(self, dataset_id, orders_path, loads_path, config, name):
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO md_datasets(id, orders_path, loads_path, config, name)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                (
                    dataset_id,
                    str(orders_path),
                    str(loads_path) if loads_path else None,
                    Jsonb(config),
                    name,
                ),
            )

    def dataset(self, dataset_id: str) -> dict | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM md_datasets WHERE id=%s", (dataset_id,)
            ).fetchone()

    def create_job(self, request: dict) -> dict:
        job_id = str(uuid.uuid4())
        with self.connect() as connection:
            row = connection.execute(
                """INSERT INTO md_jobs(id,dataset_id,request) VALUES(%s,%s,%s)
                RETURNING *""",
                (job_id, request["dataset_id"], Jsonb(request)),
            ).fetchone()
            return public_job(row)

    def get_job(self, job_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM md_jobs WHERE id=%s", (job_id,)).fetchone()
            return public_job(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id,dataset_id,request,status,progress,attempts,error,created_at,updated_at
                FROM md_jobs ORDER BY created_at DESC LIMIT %s""",
                (limit,),
            ).fetchall()
            return [public_job(row) for row in rows]

    def claim(self) -> Lease | None:
        connection = psycopg.connect(
            self.settings.database_url, connect_timeout=5, row_factory=dict_row
        )
        owner = str(uuid.uuid4())
        try:
            rows = connection.execute(
                """SELECT * FROM md_jobs WHERE
                (status='queued' OR (status='running' AND lease_until < clock_timestamp()))
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 16"""
            ).fetchall()
            for row in rows:
                locked = connection.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                    (row["id"],),
                ).fetchone()["acquired"]
                if not locked:
                    continue
                if row["attempts"] >= self.settings.max_job_attempts:
                    connection.execute(
                        """UPDATE md_jobs SET status='failed',error='worker_attempts_exhausted',
                        updated_at=clock_timestamp() WHERE id=%s""",
                        (row["id"],),
                    )
                    connection.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (row["id"],)
                    )
                    continue
                row = connection.execute(
                    """UPDATE md_jobs SET status='running',lease_owner=%s,
                    lease_until=clock_timestamp() + %s * interval '1 second',
                    attempts=attempts+1,updated_at=clock_timestamp()
                    WHERE id=%s RETURNING *""",
                    (owner, self.settings.lease_seconds, row["id"]),
                ).fetchone()
                connection.commit()
                return Lease(row, owner, connection)
            connection.commit()
            connection.close()
            return None
        except BaseException:
            connection.close()
            raise

    def heartbeat(self, job_id: str, owner: str):
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE md_jobs SET lease_until=clock_timestamp()+%s*interval '1 second'
                WHERE id=%s AND lease_owner=%s AND status='running'""",
                (self.settings.lease_seconds, job_id, owner),
            )
            if result.rowcount != 1:
                raise LostLeaseError("job_lease_lost")

    def checkpoint(self, job_id: str, owner: str, state: dict):
        evidence = state.get("evidence", [])
        if isinstance(evidence, dict):
            evidence = list(evidence.values())
        progress = {"phase": state.get("phase", "investigating"), "evidence_count": len(evidence)}
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE md_jobs SET checkpoint=%s,progress=%s,updated_at=clock_timestamp()
                WHERE id=%s AND lease_owner=%s AND status='running'
                AND lease_until>clock_timestamp()""",
                (Jsonb(state), Jsonb(progress), job_id, owner),
            )
            if result.rowcount != 1:
                raise LostLeaseError("job_lease_lost")
            for item in evidence:
                connection.execute(
                    """INSERT INTO md_evidence(job_id,evidence_id,result) VALUES(%s,%s,%s)
                    ON CONFLICT(job_id,evidence_id) DO NOTHING""",
                    (job_id, item["id"], Jsonb(item)),
                )

    def finish(self, job_id: str, owner: str, report: dict):
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE md_jobs SET report=%s,status=%s,progress=%s,
                lease_until=NULL,lease_owner=NULL,updated_at=clock_timestamp()
                WHERE id=%s AND lease_owner=%s AND status='running'
                AND lease_until>clock_timestamp()""",
                (Jsonb(report), report["status"], Jsonb({"phase": "finished"}), job_id, owner),
            )
            if result.rowcount != 1:
                raise LostLeaseError("job_lease_lost")

    def fail(self, job_id: str, owner: str, safe_error: str):
        with self.connect() as connection:
            connection.execute(
                """UPDATE md_jobs SET status='failed',error=%s,lease_until=NULL,
                lease_owner=NULL,updated_at=clock_timestamp()
                WHERE id=%s AND lease_owner=%s AND status='running'""",
                (safe_error, job_id, owner),
            )
