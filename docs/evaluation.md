# Reproducible evaluation

The real local model did **not** outperform the fixed baseline. Under the recorded 12,000-token admission budget it returned more partial investigations and was much slower. Its returned numerical evidence was correct within the checks below. These are separate results: correct numbers do not imply a complete investigation or good planning.

## Recorded real run

The suite ran on 2026-10-31 against a real local Ollama server with `qwen3.5:4b`, through LangChain `ChatOllama` structured output. Full model digest, package versions, prompt/schema hash, UTC time, settings and every investigation are in [real results](../evaluation/real-ollama/results.json). No hosted inference, paid deployment, or verified monetary pricing was involved. Cost is **unavailable**, rather than an invented zero.

Development uses seed **17**. Holdout seeds **101 and 202** each cover the eight ecommerce scenarios and one services example. Both methods receive identical files, metric configuration, periods and controlled analytical tools. The model receives aggregate evidence only; scenario names, generating labels and order identifiers are absent from its selection packet. Holdout seeds differ, but they share generator families with development, so this is a small synthetic evaluation rather than evidence of real-world generalization.

| Measure, all 18 runs per method | Fixed baseline | Real Ollama investigator |
|---|---:|---:|
| Completed | 14 | 2 |
| Partial | 0 | 12 |
| Blocked by data quality | 4 | 4 |
| Failed | 0 | 0 |
| Correct returned numeric checks / checks made | 1,812 / 1,812 | 1,152 / 1,152 |
| Leading-device targets identified / eligible cases | 4 / 4 | 4 / 4 |
| Unsupported canonical claims | 0 / 258 claims | 0 / 210 claims |
| Correct abstention guardrail checks | 8 / 8 | 8 / 8 |
| Mean analytical calls, mandatory checks included | 6.44 | 5.50 |
| Mean graph execution time | 0.0624 s | 11.0963 s |
| Median graph execution time | 0.0542 s | 13.4696 s |
| LLM request attempts | 0 | 73 |
| Measured input / output tokens | 0 / 0 | 77,714 / 5,119 |
| Monetary cost | Unavailable | Unavailable |

All four blocked cases are the incomplete-load and duplicated-reference scenarios across both seeds. The twelve model partials stop at conservative token-budget admission; their valid evidence remains available. The numeric-check denominators differ because omitted analyses do **not** count as incorrect. Therefore 100% on returned checks is not 100% investigation accuracy or completeness.

The local model commonly followed a broad decomposition, daily-pattern and marginal-segmentation route rather than showing a clear planning advantage. Some selections required the bounded invalid-selection retry. These results support neither superior reasoning nor greater efficiency than the baseline.

## Budget and production configuration

The recorded holdout command explicitly used `max_tokens=12000`, `max_calls=8`, `max_depth=2`, `max_seconds=180`, `max_retries=1`, temperature zero, reasoning disabled, a 384-token output cap and an 8,192-token model context. Token admission reserves the UTF-8 byte size of the prompt/schema plus output headroom, a deliberately conservative bound. A report can stop before the *measured* token total reaches the nominal budget. The limit is not misreported as tokens actually spent.

The current application default is **20,000 tokens**. This choice comes from the separate development-seed experiment, not a rerun or replacement of the constrained holdout results. The [recorded real development investigation](../examples/recorded-investigation.json), paired with `data/mobile_drop` seed 17, completed in 19.021081 seconds with seven analytical calls, seven LLM attempts, 8,897 input tokens and 611 output tokens. It repaired one rejected selection. This is one demonstration, not a holdout success rate.

The [earlier development attempt](../evaluation/live-first.json) is also retained: it was partial because a tool selection failed validation. No failure was silently turned into a success.

## What each measurement verifies

- **Numerical correctness:** `numerical_checks` independently reads the CSV with Python `csv`, `Decimal` and `Fraction`, without calling engine arithmetic helpers or DuckDB queries. It checks period amounts, counts, AOVs, revenue changes, exact rational decomposition, cent rounding, returned segment amounts/counts and daily amounts/counts. Blocked datasets have zero business-numeric checks rather than an invented score.
- **Relevant segments:** the largest absolute device contribution must be `mobile` in the observable `mobile_drop` and `mixed_segments` cases, across the two seeds. This is four targeted coverage checks, not arbitrary segment precision/recall.
- **Unsupported claims:** every finding must match a closed observation template, evidence path, exact stored value, unit and period. Validators also check reconciliations across unfiltered tools. A valid citation alone cannot make a causal statement pass. Zero unsupported claims demonstrates these guardrails, not general natural-language entailment or an unconstrained model's factuality.
- **Abstention:** checks cover business-diagnosis blocking for bad data, avoiding causal attribution in the ambiguous example, and recognizing an unobserved ticket in an empty period. These mainly measure deterministic quality/report guardrails, not calibrated LLM refusal or confidence.
- **Calls and tokens:** successful analytical calls and provider attempts are counted separately. Token totals come from actual Ollama response metadata. Missing or interrupted usage is marked `usage_unknown`; an unknown interrupted provider call is not reissued during recovery. No hidden reasoning text is requested or published.
- **Latency:** these suite measurements begin at `run_investigation`, after engine construction. They include model loading/inference when incurred and exclude CSV validation/DuckDB ingestion, HTTP upload, queue delay and UI rendering. Production job timing starts earlier. Means include the four rapid quality-blocked cases.

Time limits are cooperative between ingestion/analytical nodes with per-attempt transport timeouts. There is no preemptive hard deadline capable of terminating every stuck native query or provider request. The worker's lease protects job ownership; it is not a hard query watchdog.

## Reproduce

Run without credentials using the clearly labelled CI test double:

```bash
uv sync --frozen --extra dev
uv run python -m metric_detective.evaluation --provider scripted --output evaluation/smoke-scripted
uv run pytest
```

The [smoke results](../evaluation/smoke-scripted/results.json) contain 36 actual executions: 18 baseline and 18 **deterministic test-double** investigations. Both have 14 completed and four blocked runs. They make zero LLM calls. They are not a real-model evaluation and must not be used as a model comparison.

Reproduce the constrained real suite with a running Ollama server and the same model:

```bash
ollama pull qwen3.5:4b
uv run python -m metric_detective.evaluation --provider ollama --output evaluation/real-ollama --seeds 101 202 --max-seconds 180 --max-tokens 12000
```

Set `MD_LLM_MODEL`, `MD_OLLAMA_BASE_URL`, and optional `MD_LLM_API_KEY` through the environment. No other provider integration is advertised. Exact inference decisions and timings can change with model digest, Ollama version, hardware and caching despite temperature zero. To evaluate the current application budget, pass `--max-tokens 20000` and use a different output directory; that does not replace the recorded 12,000-token experiment.

Each `results.json` is saved incrementally and lists its completed records. Every record links a full report and data fingerprint. Regenerate its scenario with the recorded seed to reproduce numbers. The evaluator rejects development seed 17 as holdout. Report presentation was normalized after execution to two-decimal Spanish wording and translated warnings; evidence, selections, statuses and measurements were preserved.

## Reference documentation consulted

- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api): state, nodes, conditional entry, compilation and bounded loops.
- [LangChain ChatOllama](https://docs.langchain.com/oss/python/integrations/chat/ollama): the implemented integration and usage metadata.
- [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs): JSON-schema constrained selection. The model output still passes Pydantic and execution-policy validation.

Real PostgreSQL kill/restart recovery and API checks are documented separately in [verification](verification.md); they are not simulated by this evaluation harness.
