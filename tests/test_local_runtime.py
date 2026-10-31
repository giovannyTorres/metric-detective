"""Local supervisor boundaries without PostgreSQL, model calls or real app services."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from metric_detective import local_runtime as runtime


def options(tmp_path):
    return runtime.LocalOptions(root=str(tmp_path), runtime_dir=str(tmp_path))


def test_port_contract_and_real_listening_port_collision(tmp_path):
    with pytest.raises(runtime.RuntimeFailure, match="ports_must_be_distinct"):
        runtime.LocalOptions(root=str(tmp_path), runtime_dir=str(tmp_path), api_port=18501)
    with pytest.raises(runtime.RuntimeFailure, match="ports_must_be_between"):
        runtime.LocalOptions(root=str(tmp_path), runtime_dir=str(tmp_path), api_port=65536)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(runtime.RuntimeFailure, match=f"port_in_use:{port}"):
            runtime._free_port(port)


def test_pid_reuse_stale_state_is_stopped_and_never_receives_stop_token(tmp_path):
    process = psutil.Process()
    runtime.atomic_json(
        tmp_path / "state.json",
        {
            "pid": process.pid,
            "process_started": process.create_time() - 100,
            "token": "old-supervisor-token",
            "status": "running",
        },
    )
    state = runtime.stop_local(tmp_path, timeout=0.1)
    assert state["status"] == "stopped"
    assert state["supervisor_alive"] is False
    assert not (tmp_path / "stop").exists()
    assert process.is_running()


def test_stop_request_uses_supervisor_token_without_signalling_pid(tmp_path, monkeypatch):
    replies = iter(
        [
            {"supervisor_alive": True, "status": "running", "token": "specific-instance"},
            {"supervisor_alive": False, "status": "stopped"},
        ]
    )
    monkeypatch.setattr(runtime, "local_status", lambda _folder: next(replies))
    assert runtime.stop_local(tmp_path)["status"] == "stopped"
    assert (tmp_path / "stop").read_text(encoding="ascii") == "specific-instance"


def test_exited_service_pid_is_not_looked_up_or_added_to_owned_tree(monkeypatch):
    service = runtime.Service("exited", [], process=SimpleNamespace(pid=12345, poll=lambda: 0))

    def forbidden_lookup(_pid):
        raise AssertionError("An exited Popen PID could now belong to an unrelated process")

    monkeypatch.setattr(runtime.psutil, "Process", forbidden_lookup)
    service.remember_tree()
    assert service.owned == {}


def test_unhealthy_services_never_become_running_after_first_cycle(tmp_path, monkeypatch):
    supervisor = runtime.Supervisor(options(tmp_path), "test-instance")
    monkeypatch.setattr(supervisor, "prepare_postgres", lambda: None)
    monkeypatch.setattr(runtime, "_http_ok", lambda _url: False)

    def fake_add(name, _command):
        service = SimpleNamespace(
            name=name,
            process=SimpleNamespace(pid=111, poll=lambda: None),
            restarts=0,
            remember_tree=lambda: None,
            stop=lambda: None,
        )
        supervisor.services.append(service)
        return service

    monkeypatch.setattr(supervisor, "add", fake_add)
    observed = []

    def end_after_two_cycles(_seconds):
        observed.append(supervisor.state["status"])
        if len(observed) >= 2:
            (tmp_path / "stop").write_text("test-instance", encoding="ascii")

    monkeypatch.setattr(runtime.time, "sleep", end_after_two_cycles)
    supervisor.run()
    assert len(observed) >= 2
    assert "running" not in observed
    assert supervisor.state["status"] == "stopped"


def test_fast_startup_failure_retains_actionable_safe_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_free_port", lambda _port: None)

    def failed_supervisor(command, **_kwargs):
        runtime.atomic_json(
            tmp_path / "state.json",
            {
                "pid": 2**30,
                "process_started": 0,
                "token": command[-1],
                "status": "failed",
                "error": "postgres_unavailable_configure_database_or_postgres_bin",
            },
        )
        return SimpleNamespace()

    monkeypatch.setattr(runtime.subprocess, "Popen", failed_supervisor)
    with pytest.raises(runtime.RuntimeFailure, match="postgres_unavailable"):
        runtime.start_local(options(tmp_path), timeout=0.05)


def test_delayed_supervisor_launch_configuration_cannot_be_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_free_port", lambda _port: None)
    launched_configs = []

    def delayed_supervisor(command, **_kwargs):
        # A detached process may not have read its config before another user
        # invokes start. Keep the actual config path to model that schedule.
        launched_configs.append(Path(command[-2]))
        return SimpleNamespace()

    monkeypatch.setattr(runtime.subprocess, "Popen", delayed_supervisor)
    first = options(tmp_path)
    second = runtime.LocalOptions(
        root=str(tmp_path), runtime_dir=str(tmp_path), api_port=18001, ui_port=18502
    )
    for attempt in (first, second):
        with pytest.raises(runtime.RuntimeFailure, match="startup_timeout"):
            runtime.start_local(attempt, timeout=0)
    assert json.loads(launched_configs[0].read_text(encoding="utf-8"))["api_port"] == 18000
    assert json.loads(launched_configs[1].read_text(encoding="utf-8"))["api_port"] == 18001


def test_real_owned_process_tree_stops_without_touching_other_processes(tmp_path):
    marker = tmp_path / "owned-child.json"
    script = """
import json, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(json.dumps({'child': child.pid}), encoding='utf-8')
time.sleep(60)
"""
    creation = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(marker)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **creation,
    )
    service = runtime.Service("test-owned-only", [], process=process)
    original_test_process = psutil.Process()
    child = None
    try:
        deadline = time.monotonic() + 5
        while not marker.is_file() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.02)
        assert marker.is_file(), "Owned test process did not create its child"
        child = psutil.Process(json.loads(marker.read_text(encoding="utf-8"))["child"])
        service.remember_tree()
        assert child.pid in service.owned
        service.stop()
        assert process.poll() is not None
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        assert original_test_process.is_running()
    finally:
        service.stop()
        if child is not None and child.is_running():
            try:
                child.kill()
                child.wait(timeout=3)
            except psutil.Error:
                pass


def test_real_supervisor_file_lock_blocks_second_supervisor(tmp_path):
    marker = tmp_path / "unexpected-second-owner"
    script = """
import sys
from pathlib import Path
from metric_detective.local_runtime import LocalOptions, Supervisor
folder = Path(sys.argv[1])
supervisor = Supervisor(LocalOptions(root=str(folder), runtime_dir=str(folder)), 'second')
def forbidden_prepare():
    Path(sys.argv[2]).write_text('lock failed', encoding='utf-8')
    raise RuntimeError('second supervisor entered preparation')
supervisor.prepare_postgres = forbidden_prepare
supervisor.run()
"""
    creation = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with (tmp_path / "supervisor.lock").open("a+b") as lock:
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), str(marker)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
            **creation,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert not marker.exists()
