"""User-controlled local supervisor, independent of the launching terminal.

No installation, login task or system service is registered. After a reboot the
user runs `local start` again. Only process trees created here are terminated.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psutil
import psycopg
from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .settings import Settings

log = logging.getLogger("metric_detective.local")


class RuntimeFailure(RuntimeError):
    """Safe user-facing error code, never a connection string or provider body."""


@dataclass
class LocalOptions:
    root: str
    runtime_dir: str
    api_port: int = 18000
    ui_port: int = 18501
    postgres_bin: str | None = None
    postgres_data: str | None = None
    postgres_port: int = 55432
    start_ollama: bool = False

    def __post_init__(self):
        if any(not 1024 <= p <= 65535 for p in (self.api_port, self.ui_port, self.postgres_port)):
            raise RuntimeFailure("ports_must_be_between_1024_and_65535")
        if len({self.api_port, self.ui_port, self.postgres_port}) != 3:
            raise RuntimeFailure("ports_must_be_distinct")
        self.root = str(Path(self.root).resolve())
        self.runtime_dir = str(Path(self.runtime_dir).resolve())
        for name in ("postgres_bin", "postgres_data"):
            if value := getattr(self, name):
                setattr(self, name, str(Path(value).resolve()))


def atomic_json(path: Path, value: dict):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_state(folder: Path) -> dict:
    try:
        return json.loads((folder / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "stopped", "services": {}}


def supervisor_alive(state: dict) -> bool:
    try:
        process = psutil.Process(state["pid"])
        return process.is_running() and abs(process.create_time() - state["process_started"]) < 0.01
    except (KeyError, psutil.Error):
        return False


def local_status(folder: Path) -> dict:
    state = read_state(folder)
    alive = supervisor_alive(state)
    return {
        **state,
        "supervisor_alive": alive,
        "status": state.get("status") if alive or state.get("status") == "failed" else "stopped",
    }


def _http_ok(url: str) -> bool:
    try:
        return httpx.get(url, timeout=1, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


def _free_port(port: int):
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    except OSError:
        raise RuntimeFailure(f"port_in_use:{port}") from None


def start_local(options: LocalOptions, *, open_browser: bool = False, timeout: float = 60) -> dict:
    folder = Path(options.runtime_dir)
    folder.mkdir(parents=True, exist_ok=True)
    existing = local_status(folder)
    if existing["supervisor_alive"]:
        if (
            existing.get("api_url") != f"http://127.0.0.1:{options.api_port}"
            or existing.get("ui_url") != f"http://127.0.0.1:{options.ui_port}"
        ):
            raise RuntimeFailure("runtime_already_running_different_ports")
        if open_browser:
            webbrowser.open(existing["ui_url"])
        return existing
    _free_port(options.api_port)
    _free_port(options.ui_port)
    token = secrets.token_hex(16)
    config = folder / f"config-{token}.json"
    atomic_json(config, asdict(options))
    environment = os.environ.copy()
    environment.update(
        {"PYTHONUNBUFFERED": "1", "MD_API_URL": f"http://127.0.0.1:{options.api_port}"}
    )
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_BREAKAWAY_FROM_JOB
        )
    else:
        flags["start_new_session"] = True
    with (folder / "supervisor.log").open("ab") as output:
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "metric_detective.local_runtime",
                    "supervise",
                    str(config),
                    token,
                ],
                cwd=options.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                **flags,
            )
        except OSError:
            raise RuntimeFailure("cannot_detach_run_start_from_your_terminal") from None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = local_status(folder)
        if (
            state.get("token") != token
            and state["supervisor_alive"]
            and state["status"] == "running"
        ):
            if (
                state.get("api_url") != f"http://127.0.0.1:{options.api_port}"
                or state.get("ui_url") != f"http://127.0.0.1:{options.ui_port}"
            ):
                raise RuntimeFailure("runtime_already_running_different_ports")
            if open_browser:
                webbrowser.open(state["ui_url"])
            return state
        if state.get("token") == token:
            if state["status"] == "failed":
                raise RuntimeFailure(state.get("error", "startup_failed"))
            if state["status"] == "running":
                if open_browser:
                    webbrowser.open(state["ui_url"])
                return state
        time.sleep(0.25)
    raise RuntimeFailure("startup_timeout_check_local_status_and_supervisor_log")


def stop_local(folder: Path, *, timeout: float = 30) -> dict:
    state = local_status(folder)
    if not state["supervisor_alive"]:
        return state
    # A tokenized stop request cannot stop an unrelated reused PID.
    (folder / "stop").write_text(state["token"], encoding="ascii")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = local_status(folder)
        if not state["supervisor_alive"]:
            return state
        time.sleep(0.15)
    raise RuntimeFailure("shutdown_timeout_check_local_status")


@dataclass
class Service:
    name: str
    command: list[str]
    process: subprocess.Popen | None = None
    restarts: int = 0
    started: float = 0
    owned: dict[int, psutil.Process] = field(default_factory=dict)

    def remember_tree(self):
        if self.process is None or self.process.poll() is not None:
            return
        try:
            root = self.owned.get(self.process.pid)
            if root is None:
                root = psutil.Process(self.process.pid)
                self.owned[root.pid] = root
            if not root.is_running():
                return
            for process in [root, *root.children(recursive=True)]:
                previous = self.owned.get(process.pid)
                if previous is None or not previous.is_running():
                    self.owned[process.pid] = process
        except psutil.Error:
            pass

    def stop(self):
        self.remember_tree()
        if self.name == "postgres" and self.process and self.process.poll() is None:
            data = Path(self.command[self.command.index("-D") + 1])
            try:
                owns_postmaster = (
                    int((data / "postmaster.pid").read_text().splitlines()[0]) == self.process.pid
                )
                if owns_postmaster:
                    control = Path(self.command[0]).with_name(
                        "pg_ctl.exe" if os.name == "nt" else "pg_ctl"
                    )
                    subprocess.run(
                        [str(control), "-D", str(data), "-m", "fast", "-w", "stop"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        processes = list(self.owned.values())
        for process in reversed(processes):
            try:
                process.terminate()  # psutil verifies process identity, including PID reuse.
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(processes, timeout=3)
        for process in alive:
            try:
                process.kill()
            except psutil.Error:
                pass
        if self.process:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self.owned.clear()


class Supervisor:
    def __init__(self, options: LocalOptions, token: str):
        self.options, self.token = options, token
        self.folder = Path(options.runtime_dir)
        self.services: list[Service] = []
        self.environment = os.environ.copy()
        self.environment["MD_API_URL"] = f"http://127.0.0.1:{options.api_port}"
        self.state = {
            "pid": os.getpid(),
            "process_started": psutil.Process().create_time(),
            "token": token,
            "started_at": datetime.now(UTC).isoformat(),
            "status": "starting",
            "services": {},
            "api_url": f"http://127.0.0.1:{options.api_port}",
            "ui_url": f"http://127.0.0.1:{options.ui_port}",
        }

    def save(self):
        self.state["updated_at"] = datetime.now(UTC).isoformat()
        self.state["services"] = {
            s.name: {
                "pid": s.process.pid if s.process else None,
                "status": "running" if s.process and s.process.poll() is None else "stopped",
                "restarts": s.restarts,
            }
            for s in self.services
        }
        atomic_json(self.folder / "state.json", self.state)

    def spawn(self, service: Service):
        with (self.folder / f"{service.name}.log").open("ab") as output:
            service.process = subprocess.Popen(
                service.command,
                cwd=self.options.root,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        service.started = time.monotonic()
        service.remember_tree()

    def add(self, name, command):
        service = Service(name, command)
        self.services.append(service)
        self.spawn(service)
        self.save()
        return service

    def prepare_postgres(self):
        settings = Settings.from_env()
        self.environment["MD_DATABASE_URL"] = settings.database_url
        self.environment["MD_DATA_DIR"] = str(settings.data_dir)
        try:
            with psycopg.connect(settings.database_url, connect_timeout=2):
                return  # Existing external PostgreSQL is reused and never stopped.
        except psycopg.Error:
            if not self.options.postgres_bin:
                raise RuntimeFailure(
                    "postgres_unavailable_configure_database_or_postgres_bin"
                ) from None
        binaries = Path(self.options.postgres_bin)
        suffix = ".exe" if os.name == "nt" else ""
        postgres, initdb = binaries / ("postgres" + suffix), binaries / ("initdb" + suffix)
        if not postgres.is_file() or not initdb.is_file():
            raise RuntimeFailure("postgres_binaries_not_found")
        data = Path(self.options.postgres_data or self.folder / "postgres")
        connection = conninfo_to_dict(settings.database_url)
        if (
            connection.get("host", "localhost") not in {"127.0.0.1", "localhost"}
            or int(connection.get("port", 5432)) != self.options.postgres_port
        ):
            raise RuntimeFailure("managed_postgres_dsn_port_mismatch")
        if not (data / "PG_VERSION").is_file():
            data.mkdir(parents=True, exist_ok=True)
            if any(data.iterdir()):
                raise RuntimeFailure("postgres_directory_is_not_empty")
            password = connection.get("password")
            if not password:
                raise RuntimeFailure("new_postgres_requires_password_in_local_environment")
            password_file = self.folder / ".init-password"
            password_file.write_text(password, encoding="utf-8")
            try:
                result = subprocess.run(
                    [
                        str(initdb),
                        "-D",
                        str(data),
                        "-U",
                        connection.get("user", "detective"),
                        "--auth=scram-sha-256",
                        "--pwfile",
                        str(password_file),
                        "--encoding=UTF8",
                        "--locale=C",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=45,
                )
                if result.returncode:
                    raise RuntimeFailure("postgres_initialization_failed")
            finally:
                password_file.unlink(missing_ok=True)
        self.add(
            "postgres",
            [
                str(postgres),
                "-D",
                str(data),
                "-h",
                "127.0.0.1",
                "-p",
                str(self.options.postgres_port),
            ],
        )
        deadline = time.monotonic() + 30
        admin_url = make_conninfo(settings.database_url, dbname="postgres")
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(admin_url, connect_timeout=1, autocommit=True) as admin:
                    name = connection.get("dbname", "detective")
                    if not admin.execute(
                        "SELECT 1 FROM pg_database WHERE datname=%s", (name,)
                    ).fetchone():
                        admin.execute(
                            psycopg.sql.SQL("CREATE DATABASE {}").format(
                                psycopg.sql.Identifier(name)
                            )
                        )
                return
            except psycopg.Error:
                time.sleep(0.25)
        raise RuntimeFailure("postgres_start_timeout")

    def run(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / "supervisor.lock").open("a+b") as lock:
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return  # A second start cannot create a second supervisor.
            self.save()
            try:
                self.prepare_postgres()
                if self.options.start_ollama and not _http_ok("http://127.0.0.1:11434/api/tags"):
                    executable = shutil.which("ollama")
                    if not executable:
                        raise RuntimeFailure("ollama_executable_not_found")
                    self.environment["OLLAMA_HOST"] = "127.0.0.1:11434"
                    self.add("ollama", [executable, "serve"])
                self.add(
                    "api",
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "metric_detective.api:create_app",
                        "--factory",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(self.options.api_port),
                        "--no-access-log",
                    ],
                )
                self.add("worker", [sys.executable, "-m", "metric_detective.cli", "worker"])
                self.add(
                    "ui",
                    [
                        sys.executable,
                        "-m",
                        "streamlit",
                        "run",
                        str(Path(__file__).with_name("ui.py")),
                        "--server.address",
                        "127.0.0.1",
                        "--server.port",
                        str(self.options.ui_port),
                        "--server.headless",
                        "true",
                        "--browser.gatherUsageStats",
                        "false",
                        "--server.maxUploadSize",
                        "10",
                        "--server.fileWatcherType",
                        "none",
                        "--theme.base",
                        "dark",
                        "--theme.primaryColor",
                        "#5EEAD4",
                        "--theme.backgroundColor",
                        "#0B1322",
                        "--theme.secondaryBackgroundColor",
                        "#142138",
                        "--theme.textColor",
                        "#E9EEF6",
                    ],
                )
                while True:
                    stop = self.folder / "stop"
                    if stop.exists() and stop.read_text(encoding="ascii") == self.token:
                        stop.unlink()
                        break
                    for service in self.services:
                        service.remember_tree()
                        if service.process.poll() is not None and service.restarts < 3:
                            service.stop()
                            service.restarts += 1
                            time.sleep(min(0.5 * service.restarts, 1.5))
                            self.spawn(service)
                    ready = all(s.process.poll() is None for s in self.services)
                    ready = (
                        ready
                        and _http_ok(self.state["api_url"] + "/health")
                        and _http_ok(self.state["ui_url"] + "/_stcore/health")
                    )
                    self.state["status"] = "running" if ready else "degraded"
                    self.save()
                    time.sleep(1)
                self.state["status"] = "stopped"
            except Exception as error:
                self.state["status"] = "failed"
                self.state["error"] = (
                    str(error)
                    if isinstance(error, RuntimeFailure)
                    else "startup_failed_" + type(error).__name__
                )
            finally:
                for service in reversed(self.services):
                    service.stop()
                self.save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["supervise"])
    parser.add_argument("config", type=Path)
    parser.add_argument("token")
    args = parser.parse_args()
    options = LocalOptions(**json.loads(args.config.read_text(encoding="utf-8")))
    load_dotenv(Path(options.root) / ".env", override=False)
    Supervisor(options, args.token).run()


if __name__ == "__main__":
    main()
