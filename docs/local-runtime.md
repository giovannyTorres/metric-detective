# Local launcher and connection recovery

`127.0.0.1` is the computer running Metric Detective. Its API, worker, database
and interface need to be running before opening the local workspace.

```sh
uv sync --locked --extra dev
uv run metric-detective local start --open
uv run metric-detective local status
uv run metric-detective local stop
# Stop owned processes, then start a fresh supervisor.
uv run metric-detective local restart --open
```

Windows source checkouts also contain **Iniciar Metric Detective.cmd**,
**Estado Metric Detective.cmd**, and **Detener Metric Detective.cmd**. Double
click the start file after configuring `.env`. After a computer restart, run it
again. The launcher does not install a Windows service, login task or registry
startup entry.

The supervisor is detached from the launching terminal, writes actual service
state to `runtime/local/state.json`, and restarts an exited owned service at
most three times. API and interface HTTP health must pass before it reports
running. It captures process identities and stops only the trees it created.
Reused process IDs are not treated as ownership. Concurrent launches use
immutable per-launch configuration files and an exclusive supervisor lock.

Defaults: UI `http://127.0.0.1:18501`, API `http://127.0.0.1:18000`.
Compose separately uses ports 8501 and 8000. All host bindings are loopback.

## Existing PostgreSQL

Set `MD_DATABASE_URL` in `.env` to an available PostgreSQL database. An existing
healthy database server is reused and will not be stopped by the launcher.
The launcher initializes the application's own tables and synthetic examples.

## Optional portable PostgreSQL

Point the launcher at an existing PostgreSQL binary distribution and a data
directory. It does not download or install PostgreSQL. For example:

```dotenv
MD_DATABASE_URL=postgresql://detective:choose-a-local-password@127.0.0.1:55432/detective
MD_POSTGRES_BIN=C:/PostgreSQL/pgsql/bin
MD_POSTGRES_DATA=runtime/postgres
MD_POSTGRES_PORT=55432
```

Replace the example password in your untracked `.env`. The URI port must match
`MD_POSTGRES_PORT`. A new empty data directory is initialized with SCRAM password
authentication using a temporary password file, which is removed afterward.
An existing cluster is preserved. A nonempty unrecognized directory is rejected.
The launcher creates the configured application database if it is absent.

Official binary distribution information: [PostgreSQL Windows downloads](https://www.postgresql.org/download/windows/).

## Optional installed Ollama

With an installed Ollama executable and downloaded model, set:

```dotenv
MD_START_OLLAMA=true
MD_OLLAMA_BASE_URL=http://127.0.0.1:11434
MD_LLM_MODEL=qwen3.5:4b
```

An already running Ollama instance is reused. Otherwise an explicitly enabled
instance is started on loopback. Without a model, baseline investigations and
the labelled packaged replay remain available. No model is silently downloaded.

## Diagnose a failure

The workspace's **Estado del sistema** shows observed database health, worker
heartbeats and installed-model availability. Worker presence expires after
twenty seconds without a heartbeat. The UI blocks a new job when its worker is
missing and explains how to restart it. If the API is unavailable, the UI can
still display the packaged replay, visibly labelled as a saved investigation.

| Safe error code | Next action |
| --- | --- |
| `postgres_unavailable_configure_database_or_postgres_bin` | Check the database URI or optional binary/data configuration. |
| `managed_postgres_dsn_port_mismatch` | Use the same loopback host and port in the URI and managed-cluster settings. |
| `port_in_use:<port>` | Choose another configured port or stop the application owning it. The launcher does not terminate unrelated applications. |
| `runtime_already_running_different_ports` | Inspect `local status`; stop the existing owned runtime before changing ports. |
| `cannot_detach_run_start_from_your_terminal` | Run the start command from your own terminal. A restricted parent process may prohibit detached children. |
| `startup_timeout_check_local_status_and_supervisor_log` | Inspect `local status` and the local service logs before retrying. |

Service logs are in `runtime/local/`, which is excluded from Git and Docker
builds. No provider key or database URI is written to the public status object.

Implementation references: [Windows process creation flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags),
[psutil process identity and children](https://psutil.io/), and
[Python package resources](https://docs.python.org/3.13/library/importlib.resources.html).
