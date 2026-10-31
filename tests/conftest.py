import os
from dataclasses import replace

import pytest
from psycopg.conninfo import conninfo_to_dict

from metric_detective.settings import Settings
from metric_detective.store import Store


@pytest.fixture
def pg_store(tmp_path):
    url = os.getenv("MD_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set MD_TEST_DATABASE_URL to run real PostgreSQL integration tests")
    if not conninfo_to_dict(url).get("dbname", "").endswith("_test"):
        pytest.fail("MD_TEST_DATABASE_URL database name must end in _test (tables are cleared)")
    settings = replace(
        Settings.from_env(), database_url=url, data_dir=tmp_path / "datasets", lease_seconds=3
    )
    store = Store(settings)
    store.initialize()
    with store.connect() as connection:
        connection.execute("TRUNCATE md_evidence, md_jobs, md_datasets, md_workers CASCADE")
    yield store


@pytest.fixture
def demo_job(pg_store):
    from metric_detective.api import initialize_demo
    from metric_detective.contracts import InvestigationRequest

    scenario = initialize_demo(pg_store, pg_store.settings)[0]
    request = InvestigationRequest(
        dataset_id=scenario["dataset_id"],
        reference_start=scenario["reference_start"],
        comparison_start=scenario["comparison_start"],
        config=scenario["config"],
        mode="baseline",
    )
    return pg_store.create_job(request.model_dump(mode="json"))
