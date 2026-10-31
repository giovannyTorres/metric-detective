"""Reproducible holdout evaluation; real Ollama and CI doubles stay separate.

Numerical checks use csv + Python Decimal/Fraction, not the engine's SQL/helpers.
Generator labels are visible to this evaluator only; never to the investigator.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import platform
import statistics
import tempfile
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .agent import run_investigation
from .contracts import InvestigationRequest
from .engine import AnalysisEngine
from .provider import (
    SELECTION_PROTOCOL,
    SYSTEM_PROMPT,
    OllamaProvider,
    ScriptedProvider,
    action_schema,
)
from .reporting import canonical_findings, validate_report
from .synthetic import EXPECTATIONS, SCENARIOS, generate_scenario

EVALUATION_SEEDS = (303, 404)


def _source_records(path: Path, request: InvestigationRequest) -> list[dict[str, Any]]:
    """Independent evaluator normalization; no import of engine helpers."""
    config = request.config
    zone = ZoneInfo(config.timezone)
    records = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row[config.status_column] not in config.included_statuses:
                continue
            stamp = datetime.fromisoformat(row[config.timestamp_column].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=zone)
            local_date = stamp.astimezone(zone).date()
            period = None
            for key, start in (
                ("reference", request.reference_start),
                ("comparison", request.comparison_start),
            ):
                if start <= local_date < start + timedelta(days=7):
                    period = key
            if period is None:
                continue
            records.append(
                {
                    "period": period,
                    "date": local_date.isoformat(),
                    "amount": Decimal(row[config.amount_column]),
                    **{
                        name: row[column].strip() or "__missing__"
                        for name, column in config.dimensions.items()
                    },
                }
            )
    return records


def numerical_checks(report: dict[str, Any], orders: Path) -> dict[str, int]:
    """Check observable amounts/counts and exact decomposition against raw CSV."""
    request = InvestigationRequest.model_validate(report["request"])
    records = _source_records(orders, request)
    correct = total = 0

    def check(actual: Any, expected: Any) -> None:
        nonlocal correct, total
        total += 1
        if actual is None or expected is None:
            correct += actual is None and expected is None
        elif isinstance(expected, Fraction):
            with localcontext() as context:
                context.prec = 60
                error = abs(Decimal(str(actual)) * expected.denominator - expected.numerator)
                correct += error <= Decimal("0.00000000000000000001")
        else:
            correct += Decimal(str(actual)) == Decimal(str(expected))

    def totals(rows: list[dict[str, Any]], period: str) -> tuple[Decimal, int]:
        eligible = [r for r in rows if r["period"] == period]
        return sum((r["amount"] for r in eligible), Decimal(0)), len(eligible)

    r0, n0 = totals(records, "reference")
    r1, n1 = totals(records, "comparison")
    for item in report["evidence"]:
        values, tool = item["values"], item["tool"]
        filtered = [
            r for r in records if all(r.get(k) == v for k, v in item.get("filters", {}).items())
        ]
        if tool == "compare_periods":
            for period, revenue, count in (("reference", r0, n0), ("comparison", r1, n1)):
                check(values[period]["revenue"], revenue)
                check(values[period]["orders"], count)
                check(values[period]["aov"], Fraction(revenue) / count if count else None)
            check(values["delta_revenue"], r1 - r0)
            check(values["delta_orders"], n1 - n0)
            check(values["delta_pct"], Fraction((r1 - r0) * 100) / Fraction(r0) if r0 else None)
        if tool == "decompose_revenue_change":
            a0 = Fraction(r0) / n0 if n0 else (Fraction(r1) / n1 if n1 else Fraction(0))
            a1 = Fraction(r1) / n1 if n1 else a0
            volume = (n1 - n0) * (a0 + a1) / 2
            ticket = Fraction(r1 - r0) - volume
            interaction = (n1 - n0) * (a1 - a0)
            with localcontext() as context:
                context.prec = 60
                rounded_volume = (Decimal(volume.numerator) / volume.denominator).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_EVEN
                )
            check(values["volume_effect"], rounded_volume)
            check(values["ticket_effect"], r1 - r0 - rounded_volume)
            check(values["delta_revenue"], r1 - r0)
            for key, fraction in (
                ("volume_effect", volume),
                ("ticket_effect", ticket),
                ("interaction", interaction),
            ):
                exact = values["exact_rational"][key]
                total += 1
                correct += Fraction(int(exact["numerator"]), int(exact["denominator"])) == fraction
        if tool in {"rank_segment_changes", "drill_down"}:
            dimensions = values["dimensions"]
            for segment in values["segments"]:
                labels = (
                    segment["segment"]
                    if isinstance(segment["segment"], dict)
                    else {dimensions[0]: segment["segment"]}
                )
                subset = [r for r in filtered if all(r[d] == value for d, value in labels.items())]
                sr0, sn0 = totals(subset, "reference")
                sr1, sn1 = totals(subset, "comparison")
                check(segment["reference_revenue"], sr0)
                check(segment["comparison_revenue"], sr1)
                check(segment["reference_orders"], sn0)
                check(segment["comparison_orders"], sn1)
                check(segment["delta_revenue"], sr1 - sr0)
        if tool == "inspect_daily_pattern":
            for day in values["days"]:
                subset = [r for r in filtered if r["date"] == day["date"]]
                check(day["revenue"], sum((r["amount"] for r in subset), Decimal(0)))
                check(day["orders"], len(subset))
    return {"correct": int(correct), "total": total}


def score_report(report: dict[str, Any], orders: Path, scenario: str) -> dict[str, Any]:
    expected = EXPECTATIONS[scenario]
    evidence = report["evidence"]
    canonical = canonical_findings(evidence, report["request"]["config"]["currency"])
    unsupported = sum(claim not in canonical for claim in report["findings"])
    try:
        validate_report(report)
        valid = True
    except (ValueError, KeyError, TypeError):
        valid = False
    segment_expected = expected.get("leading_device")
    segment_hit = None
    if segment_expected:
        ranked = next(
            (
                e
                for e in evidence
                if e["tool"] == "rank_segment_changes" and e["values"].get("dimension") == "device"
            ),
            None,
        )
        leading = (
            max(ranked["values"]["segments"], key=lambda row: abs(Decimal(row["delta_revenue"])))
            if ranked and ranked["values"]["segments"]
            else None
        )
        segment_hit = bool(leading and leading["segment"] == segment_expected)
    abstention_checks = []
    if expected.get("abstain_business"):
        abstention_checks.append(
            report["status"] == "blocked_data_quality"
            and all(e["tool"] == "check_data_quality" for e in evidence)
        )
    if expected.get("causal_abstention"):
        abstention_checks.append(
            valid
            and bool(report["hypotheses"])
            and not any(f["kind"] == "causal" for f in report["findings"])
        )
    if expected.get("undefined_comparison_ticket"):
        comparison = next((e for e in evidence if e["tool"] == "compare_periods"), None)
        abstention_checks.append(
            bool(
                comparison
                and comparison["values"]["comparison"]["aov"] is None
                and any("ticket" in text for text in report["missing_data"])
            )
        )
    return {
        "numeric": numerical_checks(report, orders),
        "relevant_segment_expected": segment_expected,
        "relevant_segment_identified": segment_hit,
        "unsupported_claims": unsupported,
        "claim_count": len(report["findings"]),
        "report_valid": valid,
        "abstention_correct": sum(abstention_checks),
        "abstention_total": len(abstention_checks),
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["method"]].append(record)
    result = {}
    for method, rows in groups.items():
        correct = sum(r["score"]["numeric"]["correct"] for r in rows)
        total = sum(r["score"]["numeric"]["total"] for r in rows)
        segments = [
            r["score"]["relevant_segment_identified"]
            for r in rows
            if r["score"]["relevant_segment_identified"] is not None
        ]
        abstention_correct = sum(r["score"]["abstention_correct"] for r in rows)
        abstention_total = sum(r["score"]["abstention_total"] for r in rows)
        result[method] = {
            "runs": len(rows),
            "status_counts": {
                status: sum(r["status"] == status for r in rows)
                for status in ("completed", "partial", "blocked_data_quality", "failed")
            },
            "numeric_correct": correct,
            "numeric_total": total,
            "numeric_accuracy": correct / total if total else None,
            "relevant_segments_identified": sum(segments),
            "relevant_segments_total": len(segments),
            "unsupported_claims": sum(r["score"]["unsupported_claims"] for r in rows),
            "claim_count": sum(r["score"]["claim_count"] for r in rows),
            "abstention_correct": abstention_correct,
            "abstention_total": abstention_total,
            "mean_analytic_calls": statistics.mean(r["metrics"]["analytic_calls"] for r in rows),
            "mean_latency_seconds": statistics.mean(r["metrics"]["latency_seconds"] for r in rows),
            "median_latency_seconds": statistics.median(
                r["metrics"]["latency_seconds"] for r in rows
            ),
            "input_tokens": sum(r["metrics"]["input_tokens"] for r in rows),
            "output_tokens": sum(r["metrics"]["output_tokens"] for r in rows),
            "llm_calls": sum(r["metrics"]["llm_calls"] for r in rows),
            "usage_unknown_runs": sum(bool(r["metrics"].get("usage_unknown")) for r in rows),
            "cost": None,
        }
    return result


def evaluate(
    output: Path,
    *,
    provider_name: str = "scripted",
    seeds: tuple[int, ...] = EVALUATION_SEEDS,
    scenarios: list[str] | None = None,
    max_seconds: float = 180,
    max_tokens: int = 20000,
) -> dict[str, Any]:
    selected = scenarios or [item["id"] for item in SCENARIOS]
    if any(seed == 17 for seed in seeds):
        raise ValueError("development_seed_is_not_holdout")
    if provider_name not in {"scripted", "ollama"}:
        raise ValueError("unsupported_evaluation_provider")
    output = Path(output)
    if (output / "results.json").exists():
        raise ValueError("evaluation_output_already_exists_choose_new_directory")
    output.mkdir(parents=True, exist_ok=True)
    provider = OllamaProvider() if provider_name == "ollama" else ScriptedProvider()
    records = []
    metadata: dict[str, Any] = {
        "schema_version": "1.0",
        "investigator_version": "2.0",
        "selection_protocol": SELECTION_PROTOCOL,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "evaluation_kind": "real_llm"
        if provider_name == "ollama"
        else "deterministic_test_double_smoke",
        "provider": provider.name,
        "model": provider.model,
        "model_digest": None,
        "config": {
            "temperature": 0,
            "reasoning": False,
            "max_calls": 8,
            "max_depth": 2,
            "max_seconds": max_seconds,
            "max_tokens": max_tokens,
            "max_retries": 1,
            "output_token_cap": 64,
            "num_ctx": 8192,
        },
        "development_seed": 17,
        "evaluation_seeds": list(seeds),
        "scenarios": selected,
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "schema_template_sha256": hashlib.sha256(
            json.dumps(action_schema(["<admissible_action_id>"]), sort_keys=True).encode()
        ).hexdigest(),
        "schema_note": "Each request expands the schema enum to its actual admitted action IDs. Model choice selects the associated controlled tool and parameters.",
        "python": platform.python_version(),
        "latency_scope": "LangGraph execution wall time; excludes CSV validation and DuckDB ingestion performed before run_investigation in this evaluator.",
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("duckdb", "pydantic", "langgraph", "langchain-ollama")
        },
        "cost": None,
        "cost_note": "Unavailable: no verified hosted-provider prices; local hardware/energy cost not measured.",
        "limitations": [
            "Small synthetic holdout; seeds differ but generator families are shared with development. Not a real-world generalization benchmark.",
            "No claim of causal identification or agent superiority; both methods share controlled analytical tools and canonical report templates.",
            "Unsupported-claim checks validate a closed observation-template grammar, not arbitrary natural-language entailment.",
            "Usage totals contain measured provider responses only; interrupted or transport-failed calls are marked usage_unknown.",
            "Latency is graph execution wall time on this host and includes model inference/load; it excludes engine construction/ingestion. Hardware and caching can change it.",
            "Abstention checks mainly measure deterministic quality/report guardrails, not calibrated LLM refusal or uncertainty.",
            "The time budget is cooperative between ingestion/analytical nodes plus per-attempt transport timeouts. There is no preemptive hard wall deadline; a hung native query or vendor request can exceed it.",
        ],
    }
    if provider_name == "ollama":
        try:
            import httpx

            tags = httpx.get(provider.base_url + "/api/tags", timeout=5).json()
            match = next(
                (item for item in tags.get("models", []) if item.get("name") == provider.model),
                None,
            )
            metadata["model_digest"] = match.get("digest") if match else None
        except Exception:
            pass
    with tempfile.TemporaryDirectory(prefix="metric-detective-eval-") as scratch:
        for seed in seeds:
            for scenario in selected:
                item = generate_scenario(Path(scratch) / str(seed), scenario, seed=seed)
                for method in ("baseline", "agent"):
                    request = InvestigationRequest(
                        dataset_id="evaluation_dataset",
                        reference_start=item["reference_start"],
                        comparison_start=item["comparison_start"],
                        config=item["config"],
                        mode=method,
                        limits={"max_seconds": max_seconds, "max_tokens": max_tokens},
                    )
                    engine = AnalysisEngine(
                        Path(item["orders_path"]), Path(item["loads_path"]), request
                    )
                    selected_provider = (
                        OllamaProvider() if provider_name == "ollama" else ScriptedProvider()
                    )
                    report = run_investigation(
                        engine, request, selected_provider if method == "agent" else None
                    )
                    engine.close()
                    filename = f"seed-{seed}-{scenario}-{method}.json"
                    (output / filename).write_text(
                        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    record = {
                        "seed": seed,
                        "scenario": scenario,
                        "method": method,
                        "status": report["status"],
                        "metrics": report["metrics"],
                        "errors": report["errors"],
                        "score": score_report(report, Path(item["orders_path"]), scenario),
                        "report": filename,
                        "data_fingerprint": report["data_fingerprint"],
                    }
                    records.append(record)
                    # Useful when a long real evaluation is interrupted: only
                    # genuinely completed runs appear in the incremental file.
                    result = {**metadata, "records": records, "summary": summarize(records)}
                    (output / "results.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    print(
                        json.dumps(
                            {
                                "seed": seed,
                                "scenario": scenario,
                                "method": method,
                                "status": report["status"],
                                "seconds": report["metrics"]["latency_seconds"],
                            }
                        ),
                        flush=True,
                    )
    return {**metadata, "records": records, "summary": summarize(records)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["scripted", "ollama"], default="scripted")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seeds", type=int, nargs="+", default=list(EVALUATION_SEEDS))
    parser.add_argument("--scenarios", nargs="+", choices=[item["id"] for item in SCENARIOS])
    parser.add_argument("--max-seconds", type=float, default=180)
    parser.add_argument("--max-tokens", type=int, default=20000)
    args = parser.parse_args()
    output = args.output or Path("evaluation") / (
        "real-ollama-v2" if args.provider == "ollama" else "smoke-scripted-v2"
    )
    result = evaluate(
        output,
        provider_name=args.provider,
        seeds=tuple(args.seeds),
        scenarios=args.scenarios,
        max_seconds=args.max_seconds,
        max_tokens=args.max_tokens,
    )
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
