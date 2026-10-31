# Investigator v2: current evaluation and planning changes

The current investigator completed all fourteen nonblocked holdout cases and correctly blocked four bad-data cases. The deterministic baseline did the same, returned broader evidence, and was much faster. This evaluation supports a useful bounded planning implementation, not a claim that an LLM is required or superior for these tasks.

The [historical v1 experiment](evaluation.md) and its original files remain unchanged. Its seeds and budget differ, so the results below are not a controlled before/after comparison.

## Real held-out run

On **2026-10-31**, a real local Ollama server ran **`qwen3.5:4b`** through LangChain `ChatOllama`, using the `bounded-candidates-v2` protocol. Fresh evaluation seeds **303 and 404** cover eight ecommerce scenarios and one services example each. Development used seed **17**. The agent and fixed baseline receive identical files, metric definitions, periods, limits and analytical tools within this suite.

The [machine-readable results](../evaluation/real-ollama-v2/results.json) include the model digest, prompt and schema-template hashes, versions, date, settings and links to all **36 reports**. Configuration: 20,000-token budget, eight analytical calls including mandatory checks, two segmentation levels, 180 seconds of cooperative runtime, one bounded retry, temperature zero, reasoning disabled, 64 output tokens per selection and an 8,192-token model context. No provider prices or local energy costs were verified; monetary cost remains **unavailable**.

| Measure, 18 runs per method | Fixed baseline | Real v2 investigator |
|---|---:|---:|
| Completed | 14 | 14 |
| Partial | 0 | 0 |
| Blocked by data quality | 4 | 4 |
| Failed | 0 | 0 |
| Correct returned numeric checks / checks made | 1,812 / 1,812 | 882 / 882 |
| Leading-device targets found / eligible cases | 4 / 4 | 4 / 4 |
| Unsupported canonical findings / finding count | 0 / 258 | 0 / 136 |
| Correct abstention guardrail checks | 8 / 8 | 8 / 8 |
| Mean analytical calls, mandatory checks included | 6.44 | 4.11 |
| Mean graph execution time | 0.0438 s | 3.6117 s |
| Median graph execution time | 0.0514 s | 4.4657 s |
| Real LLM request attempts | 0 | 56 |
| Measured input / output tokens | 0 / 0 | 34,778 / 448 |
| Unknown-usage runs | 0 | 0 |
| Monetary cost | Unavailable | Unavailable |

All four blocks are incomplete-load and duplicated-reference cases across both seeds. A no-relevant-change investigation can complete after the required comparison without producing unnecessary segment charts. A completed status means the explicitly defined investigation scope is covered; it does not mean a causal explanation was identified.

Numerical accuracy is conditional on returned evidence. The baseline returns all three marginal rankings and a fixed drill-down; the model chooses a narrower partition and, where useful, a focused follow-up. Missing optional analyses are not counted as incorrect numerical checks. Consequently the different denominators must not be presented as equal analytical coverage. The baseline remains faster and broader in this experiment.

## What changed

The first two graph steps still deterministically validate quality and compare full weeks. Explicit bad-data declarations stop business diagnosis before any model call.

The model now chooses an **action ID** from a bounded frontier. Each ID resolves to a specific controlled tool, exact parameters, action-purpose label and evidence citations. Choices include different tools, dimensions and observed segment filters. Python executes only the chosen admitted action. Invalid IDs, additional SQL fields or malformed responses are rejected; there is no scripted fallback in a real investigation. This is constrained planning, not unrestricted tool generation.

The frontier depends on observations. Changed order counts prioritize segment questions; stable counts prioritize ticket decomposition. These priorities do not force a fixed sequence: the model may choose another relevant available action. After one marginal partition, unrelated additional marginal rankings are suppressed. When a leading segment accounts for at least 60% of the sum of absolute changes within that partition, a focused daily check and admissible two-dimension checks become available. This descriptive share is neither confidence nor a causal effect. Only one focused follow-up is offered after one has run. The fixed baseline bypasses this frontier and preserves its fixed sequence.

The prompt contains whitelisted summaries rather than repeated full tool results: short period totals, arithmetic contributions, up to three leading segment rows and two largest daily differences. Ratios used for selection are rounded; full exact evidence remains in the report. Long category labels are shortened with a hash **only in the prompt**; the selected ID still resolves the exact original parameter value. Raw orders, order IDs, scenario names and generator labels are absent from the selection packet. Configured dimensions must represent approved nonidentifying categories.

The JSON schema now contains only the admitted action IDs. The 64-token generation cap replaces the earlier 384-token cap. Preflight admission remains a conservative byte-based estimate with schema and framing headroom, rather than an exact model tokenizer. Actual token usage comes from Ollama response metadata. A conservative context-window guard rejects oversized packets rather than silently relying on truncation.

Finishing still has semantic prerequisites. A sufficient-evidence finish for a relevant revenue change requires the comparison, a decomposition, an unfiltered daily pattern and a marginal ranking. A no-relevant-change finish requires the actual metric threshold result. Hitting the analytical call cap after all required evidence is present now closes the investigation with an explicitly labelled **deterministic budget guard**. It does not fabricate another LLM decision or mislabel an already covered scope as partial. Exhausting the cap before coverage is complete remains partial.

Versioned hypotheses and next checks distinguish stable-order ticket questions from changed-order questions and target an observed negative segment where available. They remain hypotheses. `analysis_coverage` explicitly lists required, available and missing analyses. Historical v1 reports retain their original wording and remain valid.

## Real development checks, separate from holdout

Before the fresh holdout suite, development seed 17 was run at a **12,000-token** budget. These are actual model executions, not evaluation test doubles:

- [Mobile-volume case](../evaluation/development-v2/mobile_drop.json): completed; six analytical calls, five LLM requests, 3,343 input tokens, 40 output tokens, 12.137520 seconds including model loading when incurred. The model selected device ranking, a mobile-filtered daily check, decomposition, an unfiltered daily check, then finish.
- [Stable-order ticket case](../evaluation/development-v2/ticket_drop.json): completed; five analytical calls, four LLM requests, 2,424 input tokens, 32 output tokens, 4.629331 seconds. The model selected device ranking, decomposition, an unfiltered daily check, then finish.

Both had zero invalid selections and no unknown usage. These two demonstrations are not a holdout success rate or a controlled comparison with the historical run. Planning code was frozen before the fresh real holdout execution.

## Reproduce without overwriting history

Credential-free smoke evaluation uses an explicitly labelled deterministic test double:

```bash
uv sync --frozen --extra dev
uv run python -m metric_detective.evaluation --provider scripted --output work/my-smoke-v2 --seeds 303 404 --max-tokens 20000
uv run pytest
```

The [recorded smoke suite](../evaluation/smoke-scripted-v2/results.json) contains 36 executed investigations, with fourteen completed and four blocked per method. It makes zero model requests and is not a real-LLM benchmark.

With Ollama running and the tested model available:

```bash
ollama pull qwen3.5:4b
uv run python -m metric_detective.evaluation --provider ollama --output work/my-real-v2 --seeds 303 404 --max-seconds 180 --max-tokens 20000
```

Use a new output directory for a new experiment. The evaluator refuses to overwrite an existing `results.json`. Default output names now end in `-v2`; the published result directories already contain recorded experiments. `MD_LLM_MODEL`, `MD_OLLAMA_BASE_URL` and optional `MD_LLM_API_KEY` configure the implemented Ollama provider; `MD_LLM_PROVIDER` values other than `ollama` are rejected. Other model tags are configurable but have not been validated by this recorded suite.

## Interpretation limits

The score definitions remain those in the [evaluation methodology](evaluation.md#what-each-measurement-verifies): independent CSV/Decimal/Fraction arithmetic, four targeted leading-device checks and eight abstention checks. The abstention measures primarily verify deterministic quality/report guardrails. Zero unsupported findings verifies a closed observation grammar and evidence reconciliation, not arbitrary natural-language entailment or calibrated LLM uncertainty.

Seeds differ but generator families are shared with development. The suite is small, synthetic and structured around the supported metric contract. Latency is graph wall time after engine construction; it excludes CSV validation/DuckDB ingestion, upload, queue delay and browser rendering. Means include four rapid quality-blocked runs. Hardware, caching and model digest can change inference decisions and timing. Runtime bounds remain cooperative between nodes and per-attempt transport timeouts; they are not a preemptive hard deadline.

Official interfaces used: [Ollama chat schema and measured token counts](https://docs.ollama.com/api/chat), [LangChain ChatOllama integration](https://docs.langchain.com/oss/python/integrations/chat/ollama), and [LangGraph state and conditional routing](https://docs.langchain.com/oss/python/langgraph/graph-api).
