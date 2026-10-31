"""Environment-only runtime settings. Values are never logged."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    max_upload_bytes: int = 10 * 1024 * 1024
    lease_seconds: int = 30
    max_job_attempts: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(override=False)
        return cls(
            database_url=os.getenv(
                "MD_DATABASE_URL",
                "postgresql://detective:local-development-only@localhost:5432/detective",
            ),
            data_dir=Path(os.getenv("MD_DATA_DIR", "runtime/datasets")).resolve(),
            max_upload_bytes=max(1024, int(os.getenv("MD_MAX_UPLOAD_BYTES", "10485760"))),
            lease_seconds=max(3, int(os.getenv("MD_LEASE_SECONDS", "30"))),
            max_job_attempts=max(1, int(os.getenv("MD_MAX_JOB_ATTEMPTS", "3"))),
        )
