"""One bounded investigation at a time; PostgreSQL coordinates worker replicas."""

import logging
import threading
import time
from pathlib import Path

from metric_detective.agent import initial_state, run_investigation
from metric_detective.contracts import InvestigationRequest
from metric_detective.engine import AnalysisEngine
from metric_detective.settings import Settings
from metric_detective.store import LostLeaseError, Store

logger = logging.getLogger("metric_detective.worker")


def process_one(store: Store, provider=None, checkpoint_observer=None) -> bool:
    lease = store.claim()
    if lease is None:
        return False
    job_id = lease.job["id"]
    stop = threading.Event()
    heartbeat_failed = threading.Event()

    def heartbeat():
        while not stop.wait(store.settings.lease_seconds / 3):
            try:
                store.heartbeat(job_id, lease.owner)
            except Exception:
                heartbeat_failed.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    engine = None
    try:
        dataset = store.dataset(lease.job["dataset_id"])
        request = InvestigationRequest.model_validate(lease.job["request"])
        resume_state = lease.job.get("checkpoint") or initial_state(request)
        engine = AnalysisEngine(
            Path(dataset["orders_path"]),
            Path(dataset["loads_path"]) if dataset["loads_path"] else None,
            request,
        )

        def checkpoint(state):
            if heartbeat_failed.is_set() or lease.connection.closed:
                raise LostLeaseError("job_lease_lost")
            store.checkpoint(job_id, lease.owner, state)
            if checkpoint_observer:
                checkpoint_observer(state)

        report = run_investigation(
            engine,
            request,
            provider=provider,
            on_checkpoint=checkpoint,
            resume_state=resume_state,
        )
        if heartbeat_failed.is_set():
            raise LostLeaseError("job_lease_lost")
        store.finish(job_id, lease.owner, report)
        logger.info("investigation_finished job=%s status=%s", job_id, report["status"])
    except LostLeaseError:
        logger.warning("investigation_lease_lost job=%s", job_id)
    except Exception as error:
        # Never log exception messages, requests, DSNs, provider bodies or credentials.
        safe_error = "investigation_failed_" + type(error).__name__
        store.fail(job_id, lease.owner, safe_error)
        logger.error("investigation_failed job=%s code=%s", job_id, safe_error)
    finally:
        if engine is not None:
            engine.close()
        stop.set()
        thread.join(timeout=2)
        lease.close()
    return True


def run_worker(settings: Settings | None = None, once: bool = False):
    store = Store(settings or Settings.from_env())
    store.initialize()
    while True:
        try:
            processed = process_one(store)
        except Exception as error:
            logger.error("worker_database_error code=%s", type(error).__name__)
            processed = False
        if once:
            return
        if not processed:
            time.sleep(1)
