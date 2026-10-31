# Executed verification

Date: **2026-10-31**, America/Mexico_City. This record separates observed results
from configuration and unexecuted checks. All input records are synthetic.

## Environment and clean installation

- Windows 11, CPython **3.13.5**, uv **0.13.5**.
- Real isolated PostgreSQL **17.11**, bound to loopback on a temporary test port.
  Portable binaries came from EDB, linked by the official PostgreSQL downloads
  page. No system PostgreSQL service was installed.
- A fresh virtual environment outside the source tree was created with
  `uv sync --locked --extra dev`. All **82 installed packages** passed
  `uv pip check`; no dependency incompatibilities were reported.
- Exact direct versions and transitive versions/hashes are in `pyproject.toml`
  and `uv.lock`. The same lock is used by Docker and CI.

## Automated verification

The final frozen-source, clean-environment full suite passed **118 tests in 65.62 seconds** with real
PostgreSQL and no skipped database checks. Coverage was **90%** across the Python
package, excluding the separately tested Streamlit UI module. Ruff lint and
format checks passed. This final run includes HTML traceability/escaping,
unsupported-provider rejection and selection-depth regression tests. These are
local results, not a remote CI claim.
The test suite includes independently recomputed CSV totals using Decimal and
Fraction; exact decomposition/rounding; each segment partition; reproducible SQL;
missing/zero/new/disappeared values; timezones; hostile tool parameters;
data-quality gates; graph routes and budgets; provider errors with an explicit
CI double; semantic tampering; API endpoints; exports; and real PostgreSQL.

The UI tests exercise actual baseline engine reports in Streamlit AppTest,
including blocked data and empty periods. A concurrent upload/health test uses an
explicit slow-validation double to verify that validation runs off the event
loop. These doubles are not reported as model evaluations.

## Real process interruption and concurrent workers

`tests/test_persistence.py::test_real_process_kill_and_recovery` launches a new
Python worker process against real PostgreSQL. The parent waits for at least two
committed pieces of evidence, kills the worker process, waits for the actual
three-second test lease to expire, and starts another worker. Assertions verify:

- The recovered job completes on attempt **2**.
- Previously committed evidence IDs survive.
- Final report IDs are unique and match the persisted evidence ledger.

The concurrency test runs six simultaneous claims for one job: exactly one owns
it. It also deliberately expires that lease while its database session remains
alive; another worker cannot acquire the session advisory lock. Stale fencing
tokens cannot write checkpoints. Repeated checkpoint commits do not duplicate
evidence.

## Actual end-to-end UI

Native API, worker and Streamlit processes ran against the isolated PostgreSQL
instance. In a real browser, a synthetic `mobile_drop` baseline investigation was
started from the form and completed with persisted evidence. Its observed data:

| Measure | MXN |
| --- | ---: |
| Reference revenue | 25,325.97 |
| Comparison revenue | 18,322.62 |
| Change | -7,003.35 |
| Volume arithmetic contribution | -7,041.08 |
| Ticket arithmetic contribution | +37.73 |

The [walkthrough GIF](assets/walkthrough.gif) contains sampled screenshots from
that actual baseline workflow. It is not a simulated model interaction or a
continuous screen recording. Browser checks cover the daily chart, waterfall,
partition contributions, evidence/decisions, and exports. The replay is a
separate saved **real Ollama** investigation; its source JSON, model, token usage
and budget are retained in `examples/recorded-investigation.json` and evaluation
artifacts.

The six published SQL queries in the recorded real-model report were also
re-executed from their exact bound parameters. Its data fingerprint matched the
included development-seed input files.

CSV snapshots are explicitly excluded from Git newline conversion. The staged
CSV bytes were compared to the analyzed files before committing, preserving the
recorded dataset fingerprint across Windows and Linux checkouts.
A separate fresh Git clone then loaded its own source module and reproduced
the recorded fingerprint and all **six SQL queries** from the included files.

## Model evaluation

See [evaluation methodology and measured results](evaluation.md). Live Ollama
calls are separate from CI doubles and the fixed baseline. Earlier partial runs
are retained. Completion status, coverage and missing analyses matter alongside
numeric correctness. Cost is unavailable; local hardware/energy cost was not
measured. No claim of superiority is made.

## Explicitly unverified or limited

- Docker is absent on the authoring computer, so **Docker Compose has not been
  executed locally**. A separate GitHub Actions job builds the image, starts all
  Compose services and runs a credential-free baseline investigation.
- A local test pass is not a GitHub Actions pass. Remote CI status must be checked
  after the repository is published.
- The n8n workflow was parsed as JSON and reviewed. It was **not imported or
  executed in n8n** and prepares, but never sends, a notification.
- `max_seconds` is a cooperative investigation deadline checked between bounded
  ingestion/analytical operations. It cannot preempt an active CSV parse or
  DuckDB query. Provider requests also have transport timeouts. Interrupted
  provider responses have unknown usage and end partial without automatic
  reissue. Session locks/fencing protect ownership and writes, not a hard CPU
  execution deadline.
- This is a small synthetic, local MVP, without internet-facing authentication,
  causal identification or a real-company deployment claim.

## Reproduce the checks

```sh
uv sync --locked --extra dev
uv run ruff check .
uv run ruff format --check .
# Set MD_TEST_DATABASE_URL to an expendable database ending in _test.
uv run pytest --cov=metric_detective --cov-report=term-missing
uv pip check
```

Without `MD_TEST_DATABASE_URL`, PostgreSQL tests explicitly skip. In CI a real
disposable PostgreSQL service is provided; no model credentials are required.
