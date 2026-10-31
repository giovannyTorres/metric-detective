# Executed verification: version 0.2.0

Date: **2026-10-31**, America/Mexico_City. All demonstration records are synthetic.
The [0.1 verification record](verification.md) remains available as history.

## Automated and installed-package checks

- The full local suite passed **156 tests in 65.09 seconds**, with real
  PostgreSQL and no skipped database checks. Coverage was **84%**, excluding
  the separately tested Streamlit UI module. The expanded native supervisor is
  included in that coverage denominator.
- After the final progress-indicator correction, all **16 UI tests** passed
  in 7.88 seconds. Two directly inspect Streamlit's running status state for a
  queued/running job. The subsequent chart-scope caption change also passed
  those same sixteen tests.
- Ruff lint and format checks passed. `uv pip check` verified **83 installed
  development/runtime packages** without incompatibilities.
- A **0.2.0 wheel** was installed into a fresh isolated environment with
  **75 runtime packages**. Python isolated mode imported only the installed
  wheel, loaded and validated its packaged replay without a source checkout or
  database, exercised `/replay`, and completed a newly generated seed-303
  baseline investigation with eight pieces of evidence and zero LLM calls.

Environment: Windows x64, CPython 3.13.5, uv 0.13.5 and real PostgreSQL 17.11.
The wheel contains its replay JSON as a package resource. Local startup passes
theme, upload-size and telemetry settings explicitly, including outside a source
checkout. No system PostgreSQL service or OS startup task was installed.

## Runtime and real failures

The native launcher was run against the existing portable PostgreSQL data and
installed Ollama. It started database, model server, API, worker and Streamlit.
Both API/UI HTTP health passed; `/health/details` independently reported a live
worker heartbeat and installed configured model. The launching command exited
while the detached supervisor and services continued running.

The UI's actual Python process was killed. The supervisor created a new process,
the UI HTTP health recovered, and the supervisor, database and model-server
processes were not restarted. This was a real operating-system interruption.
The complete runtime was also stopped and restarted through `local restart`,
then successfully processed a new real-model investigation from the browser.

The PostgreSQL test suite separately kills an investigation worker after committed
checkpoints, waits for real lease expiry and resumes it. Attempt two completes
with prior evidence IDs preserved and no duplicate evidence. Concurrent claims,
session advisory locks and stale fencing-token rejection are covered.

Additional native tests use real short-lived process trees, an occupied socket
and a cross-process supervisor lock. Explicit doubles cover startup-failure and
configuration-race edges. None of those doubles is described as a live model.

## Actual interface and reports

Browser verification covered a new Ollama investigation, observed progress,
completion, quality declarations, full monetary values, daily/segment/waterfall
charts, saved history and inspectable evidence. Filtered daily charts identify
their segment; overall charts explicitly state that they have no segment filter.
API-unavailable replay and missing-worker/model submission gates are also
exercised through Streamlit AppTest with explicit service doubles.

The [current replay JSON](../examples/recorded-investigation-v2.json) and
[offline HTML](../examples/recorded-investigation-v2.html) are a saved real-model
execution paired with `data/mobile_drop`, not a simulation. The README GIF uses
sampled screenshots from the actual UI workflow, rather than a continuous video.
The historical v1 replay and benchmark artifacts are preserved.

Report tests reject altered figures, derived percentages, wrong units/periods,
invented citations, unsupported causal text, inconsistent data-quality status and
unsupported schema versions. Each saved current/historical report was validated
against the current verifier. Input validation rejects case-insensitive logical
column collisions, handles date overflow, and binds parsing and fingerprints to
one immutable byte snapshot. Upload cancellation and failed database registration
close files and remove unregistered partial snapshots.

## Evaluation and remote checks

The [fresh v2 real evaluation](evaluation-v2.md) ran 18 cases per method on seeds
303/404. Both the model and baseline completed fourteen and blocked four; neither
returned a partial or failed case. The baseline was faster and returned broader
evidence. Returned numeric checks were 882/882 for the model and 1,812/1,812 for
the baseline; these denominators measure different returned coverage.

The public repository's [initial remote checks](https://github.com/giovannyTorres/metric-detective/actions/runs/41253018889)
passed both test/lint and Docker Compose smoke jobs. Compose built an image,
started all services, executed a persisted baseline investigation and checked the
UI health endpoint on a clean GitHub runner. That run used the earlier source
commit. The [current workflow status](https://github.com/giovannyTorres/metric-detective/actions/workflows/ci.yml)
identifies the checked commit for every subsequent push; an earlier green run is
not evidence for a later revision.

## Remaining practical limits

- Windows reboot/logoff stops native processes. Run the start launcher again;
  there is no automatic OS startup registration.
- Investigation deadlines remain cooperative between bounded ingestion/analysis
  nodes, with provider transport timeouts; they cannot preempt every native call.
- n8n was parsed/reviewed as JSON, not imported or executed in n8n. It prepares a
  notification payload and never sends one.
- No paid infrastructure or publicly hosted multi-tenant application was deployed.
- Model cost remains unavailable, and arithmetic concentration does not prove a
  cause. See [scope and limitations](limitations.md).
