"""One investigator: mandatory checks, bounded LLM selection, controlled tools.

The durable worker stores complete JSON snapshots after graph nodes. A phase
field is the explicit recovery cursor, independent of process-local memory.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .contracts import InvestigationRequest
from .provider import DecisionProvider, OllamaProvider, ProviderError, ToolDecision
from .reporting import build_report

Checkpoint = Callable[[dict[str, Any]], None]


class InvestigationState(TypedDict, total=False):
    phase: str
    request: dict[str, Any]
    started_at: float
    data_fingerprint: str
    evidence: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    metrics: dict[str, Any]
    errors: list[str]
    status: str
    selection: dict[str, Any] | None
    provider_inflight: bool
    report: dict[str, Any]


REASONS = {
    "volume_change": "Investigar la distribución del cambio de pedidos.",
    "ticket_change": "Separar la contribución del volumen y del ticket promedio.",
    "segment_concentration": "Examinar la concentración observada por segmento.",
    "daily_pattern": "Comparar la evolución por día de las semanas seleccionadas.",
    "interaction_check": "Cruzar dimensiones para revisar la concentración observada.",
    "no_relevant_change": "El cambio observado no supera el umbral de relevancia configurado.",
    "insufficient_evidence": "Terminar porque la información disponible no permite resolver más preguntas.",
    "sufficient_evidence": "Terminar con los análisis disponibles y sus limitaciones explícitas.",
}


def initial_state(request: InvestigationRequest) -> InvestigationState:
    return {
        "phase": "validate",
        "request": request.model_dump(mode="json"),
        "started_at": time.time(),
        "data_fingerprint": "",
        "evidence": [],
        "decisions": [],
        "metrics": {
            "analytic_calls": 0,
            "llm_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_seconds": 0.0,
            "cost": None,
            "model": "none",
            "provider": "none",
            "usage_unknown": False,
            "rejected_selections": 0,
        },
        "errors": [],
        "status": "completed",
        "selection": None,
        "provider_inflight": False,
    }


def _signature(tool: str, arguments: dict[str, Any]) -> str:
    arguments = {k: v for k, v in arguments.items() if not (k == "filters" and not v)}
    return json.dumps([tool, arguments], sort_keys=True, separators=(",", ":"))


def _executed(state: dict[str, Any]) -> set[str]:
    return {
        _signature(item["tool"], item.get("args", {}))
        for item in state["decisions"]
        if item.get("executed")
    }


def _allowed_actions(state: dict[str, Any], request: InvestigationRequest) -> list[dict[str, Any]]:
    executed = _executed(state)
    actions: list[dict[str, Any]] = [
        {"tool": "decompose_revenue_change"},
        {"tool": "inspect_daily_pattern"},
    ]
    actions.extend(
        {"tool": "rank_segment_changes", "dimension": d} for d in request.config.dimensions
    )
    # A two-dimensional cross-check is useful only after an observed marginal
    # concentration. Never expose high-cardinality raw orders to the provider.
    if request.limits.max_depth >= 2:
        ranked = [e for e in state["evidence"] if e["tool"] == "rank_segment_changes"]
        for item in ranked:
            dimension = item["values"].get("dimension")
            other = next((d for d in request.config.dimensions if d != dimension), None)
            if dimension and other:
                actions.append({"tool": "drill_down", "dimensions": [dimension, other]})
    return [
        a
        for a in actions
        if _signature(a["tool"], {k: v for k, v in a.items() if k != "tool"}) not in executed
    ]


def _aggregate_context(state: dict[str, Any], request: InvestigationRequest) -> dict[str, Any]:
    """A small aggregate packet; no dataset names, scenario labels, or order IDs."""
    aggregates = []
    for item in state["evidence"]:
        values = copy.deepcopy(item["values"])
        for key in (
            "exact_rational",
            "rounding_policy",
            "method",
            "reference_ticket_for_decomposition",
            "comparison_ticket_for_decomposition",
        ):
            values.pop(key, None)
        if "segments" in values:
            values["segments"] = [
                {
                    k: row[k]
                    for k in (
                        "segment",
                        "delta_revenue",
                        "reference_orders",
                        "comparison_orders",
                        "contribution_pct",
                    )
                    if k in row
                }
                for row in sorted(
                    values["segments"],
                    key=lambda row: abs(Decimal(str(row["delta_revenue"]))),
                    reverse=True,
                )[:4]
            ]
            values["segments_truncated_for_prompt"] = len(item["values"]["segments"]) > 4
        if "days" in values:
            rows = values.pop("days")
            by_day: dict[int, dict[str, Any]] = {}
            for row in rows:
                by_day.setdefault(row["day_index"], {})[row["period"]] = row
            values["largest_daily_changes"] = sorted(
                [
                    {
                        "day_index": day,
                        "delta_revenue": str(
                            Decimal(pair["comparison"]["revenue"])
                            - Decimal(pair["reference"]["revenue"])
                        ),
                        "delta_orders": pair["comparison"]["orders"] - pair["reference"]["orders"],
                    }
                    for day, pair in by_day.items()
                ],
                key=lambda row: abs(Decimal(row["delta_revenue"])),
                reverse=True,
            )[:3]
        aggregates.append(
            {
                "id": item["id"],
                "tool": item["tool"],
                "values": values,
                "filters": item.get("filters", {}),
            }
        )
    return {
        "currency": request.config.currency,
        "dimensions": list(request.config.dimensions),
        "timezone": request.config.timezone,
        "reference_start": request.reference_start.isoformat(),
        "comparison_start": request.comparison_start.isoformat(),
        "relevant_change_pct": request.config.relevant_change_pct,
        "remaining_analytical_calls": request.limits.max_calls - state["metrics"]["analytic_calls"],
        "evidence": aggregates,
        "executed": [
            {"tool": d["tool"], "args": d.get("args", {})}
            for d in state["decisions"]
            if d.get("executed")
        ],
        "allowed_actions": _allowed_actions(state, request),
        "required_before_sufficient_finish": [
            "decompose_revenue_change",
            "unfiltered inspect_daily_pattern",
            "rank_segment_changes",
        ],
    }


def _validate_selection(
    decision: ToolDecision, state: dict[str, Any], request: InvestigationRequest
) -> None:
    ids = {e["id"] for e in state["evidence"]}
    if not set(decision.evidence_ids).issubset(ids):
        raise ValueError("invalid_decision_evidence")
    arguments = decision.arguments()
    allowed = set(request.config.dimensions)
    if any(key not in allowed for key in decision.filters):
        raise ValueError("invalid_dimension")
    for value in decision.filters.values():
        if len(value) > 128:
            raise ValueError("invalid_filter")
    if decision.tool == "rank_segment_changes" and decision.dimension not in allowed:
        raise ValueError("invalid_dimension")
    if (
        decision.tool == "inspect_daily_pattern"
        and len(decision.filters) > request.limits.max_depth
    ):
        raise ValueError("invalid_filter_depth")
    if decision.tool == "drill_down":
        if len(decision.dimensions) != 2 or len(set(decision.dimensions)) != 2:
            raise ValueError("invalid_drill_depth")
        if len(decision.dimensions) > request.limits.max_depth or not set(
            decision.dimensions
        ).issubset(allowed):
            raise ValueError("invalid_drill_depth")
        if len(set(decision.dimensions).union(decision.filters)) > request.limits.max_depth:
            raise ValueError("invalid_drill_depth")
        if not any(e["tool"] == "rank_segment_changes" for e in state["evidence"]):
            raise ValueError("drill_requires_segment_evidence")
    if decision.tool != "finish" and _signature(decision.tool, arguments) in _executed(state):
        raise ValueError("repeated_tool_selection")
    comparison = next(e for e in state["evidence"] if e["tool"] == "compare_periods")
    if (
        decision.reason_code == "no_relevant_change"
        and comparison["values"].get("relevant_change") is not False
    ):
        raise ValueError("contradicted_no_relevant_change")
    if decision.tool == "finish":
        if decision.reason_code not in {
            "sufficient_evidence",
            "insufficient_evidence",
            "no_relevant_change",
        }:
            raise ValueError("invalid_finish_reason")
        if decision.reason_code == "sufficient_evidence":
            tools = {e["tool"] for e in state["evidence"] if not e.get("filters")}
            if not {
                "decompose_revenue_change",
                "inspect_daily_pattern",
                "rank_segment_changes",
            }.issubset(tools):
                raise ValueError("missing_required_analysis_before_finish")


def _decision_record(decision: ToolDecision, *, executed: bool = False) -> dict[str, Any]:
    return {
        "tool": decision.tool,
        "args": decision.arguments(),
        "reason_code": decision.reason_code,
        "summary": REASONS[decision.reason_code],
        "evidence_ids": decision.evidence_ids,
        "executed": executed,
    }


def build_graph(
    engine: Any,
    provider: DecisionProvider | None,
    request: InvestigationRequest,
    *,
    before_provider: Checkpoint | None = None,
) -> Any:
    graph = StateGraph(InvestigationState)

    def deadline(state: dict[str, Any]) -> bool:
        return time.time() - state["started_at"] >= request.limits.max_seconds

    def partial(state: dict[str, Any], code: str) -> dict[str, Any]:
        state["status"] = "partial" if state["evidence"] else "failed"
        if code not in state["errors"]:
            state["errors"].append(code)
        state["phase"] = "finalize"
        return state

    def execute_tool(
        state: dict[str, Any], tool: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if deadline(state):
            return partial(state, "time_budget_exhausted")
        if state["metrics"]["analytic_calls"] >= request.limits.max_calls:
            return partial(state, "analytical_call_limit")
        result = engine.run(tool, arguments or {}).model_dump(mode="json")
        by_id = {e["id"]: e for e in state["evidence"]}
        by_id[result["id"]] = result
        state["evidence"] = list(by_id.values())
        state["metrics"]["analytic_calls"] += 1
        return state

    def validate(state: InvestigationState) -> InvestigationState:
        state = copy.deepcopy(state)
        InvestigationRequest.model_validate(state["request"])
        state["data_fingerprint"] = engine.fingerprint
        state["phase"] = "quality"
        return state

    def quality(state: InvestigationState) -> InvestigationState:
        state = execute_tool(copy.deepcopy(state), "check_data_quality")
        if state["phase"] == "finalize":
            return state
        item = state["evidence"][-1]
        if item["values"]["status"] == "blocked":
            state.update(status="blocked_data_quality", phase="finalize")
        else:
            if item["values"]["status"] == "unverified":
                state["status"] = "partial"
            state["phase"] = "compare"
        return state

    def compare(state: InvestigationState) -> InvestigationState:
        state = execute_tool(copy.deepcopy(state), "compare_periods")
        if state["phase"] == "finalize":
            return state
        state["phase"] = "select"
        return state

    baseline_sequence = [
        ("decompose_revenue_change", {}),
        *[("rank_segment_changes", {"dimension": d}) for d in request.config.dimensions],
        ("inspect_daily_pattern", {}),
    ]
    dims = list(request.config.dimensions)
    if len(dims) >= 2 and request.limits.max_depth >= 2:
        baseline_sequence.append(("drill_down", {"dimensions": dims[:2], "filters": {}}))

    def select(state: InvestigationState) -> InvestigationState:
        state = copy.deepcopy(state)
        if deadline(state):
            return partial(state, "time_budget_exhausted")
        if state["metrics"]["analytic_calls"] >= request.limits.max_calls:
            # A fully explored fixed baseline reaches exactly eight calls.
            if request.mode == "baseline" and len(_executed(state)) >= len(baseline_sequence):
                state["phase"] = "finalize"
                return state
            return partial(state, "analytical_call_limit")
        if request.mode == "baseline":
            selected = next(
                ((t, a) for t, a in baseline_sequence if _signature(t, a) not in _executed(state)),
                None,
            )
            if selected is None:
                state["phase"] = "finalize"
                return state
            tool, args = selected
            decision = ToolDecision(
                tool=tool,
                **args,
                reason_code="sufficient_evidence",
                evidence_ids=[state["evidence"][-1]["id"]],
            )
        else:
            if provider is None:
                return partial(state, "provider_not_configured")
            # Persist intent BEFORE the external effect. An interrupted unknown
            # provider response is not reissued: recovery finishes partial with
            # usage_unknown. This avoids silently double-spending the budget.
            previous_error = None
            for attempt in range(request.limits.max_retries + 1):
                remaining = (
                    request.limits.max_tokens
                    - state["metrics"]["input_tokens"]
                    - state["metrics"]["output_tokens"]
                )
                timeout = request.limits.max_seconds - (time.time() - state["started_at"])
                if timeout <= 0:
                    return partial(state, "time_budget_exhausted")
                context = _aggregate_context(state, request)
                if previous_error:
                    context["previous_selection_error"] = previous_error
                state["provider_inflight"] = True
                if before_provider:
                    before_provider(copy.deepcopy(state))
                try:
                    selection = provider.select(
                        context,
                        remaining_tokens=remaining,
                        timeout=max(0.1, timeout),
                        max_retries=0,
                    )
                    state["metrics"]["llm_calls"] += selection.calls
                    state["metrics"]["input_tokens"] += selection.input_tokens
                    state["metrics"]["output_tokens"] += selection.output_tokens
                    decision = selection.decision
                    _validate_selection(decision, state, request)
                    break
                except ProviderError as exc:
                    state["metrics"]["llm_calls"] += exc.calls
                    state["metrics"]["input_tokens"] += exc.input_tokens
                    state["metrics"]["output_tokens"] += exc.output_tokens
                    unknown_usage = bool(exc.calls) and exc.code in {
                        "provider_unavailable_or_invalid_response",
                        "provider_timeout",
                        "provider_usage_missing",
                    }
                    if unknown_usage:
                        state["metrics"]["usage_unknown"] = True
                    previous_error = exc.code
                    if (
                        attempt >= request.limits.max_retries
                        or exc.code == "token_budget_exhausted"
                        or unknown_usage
                    ):
                        state["provider_inflight"] = False
                        return partial(state, exc.code)
                except (ValueError, TypeError) as exc:
                    state["metrics"]["rejected_selections"] += 1
                    previous_error = (
                        str(exc) if type(exc) is ValueError else "invalid_tool_selection"
                    )
                    if attempt >= request.limits.max_retries:
                        state["provider_inflight"] = False
                        return partial(state, f"invalid_tool_selection:{previous_error}")
            state["provider_inflight"] = False
        record = _decision_record(decision)
        state["decisions"].append(record)
        state["selection"] = decision.model_dump(mode="json")
        if decision.tool == "finish":
            if decision.reason_code == "insufficient_evidence":
                state["status"] = "partial"
            state["phase"] = "finalize"
        else:
            state["phase"] = "execute"
        return state

    def execute(state: InvestigationState) -> InvestigationState:
        state = copy.deepcopy(state)
        decision = ToolDecision.model_validate(state["selection"])
        state = execute_tool(state, decision.tool, decision.arguments())
        if state["phase"] != "finalize":
            state["decisions"][-1]["executed"] = True
            state["phase"] = "review"
        return state

    def review(state: InvestigationState) -> InvestigationState:
        state = copy.deepcopy(state)
        state["selection"] = None
        state["phase"] = "select"
        return state

    def finalize(state: InvestigationState) -> InvestigationState:
        state = copy.deepcopy(state)
        state["metrics"]["latency_seconds"] = round(time.time() - state["started_at"], 6)
        state["report"] = build_report(state, request)
        state["phase"] = "done"
        return state

    nodes = {
        "validate": validate,
        "quality": quality,
        "compare": compare,
        "select": select,
        "execute": execute,
        "review": review,
        "finalize": finalize,
    }
    for name, node in nodes.items():
        graph.add_node(name, node)
    routes = {**{name: name for name in nodes}, "done": END}
    graph.add_conditional_edges(START, lambda state: state["phase"], routes)
    for name in nodes:
        graph.add_conditional_edges(name, lambda state: state["phase"], routes)
    return graph.compile()


def run_investigation(
    engine: Any,
    request: InvestigationRequest,
    provider: DecisionProvider | None = None,
    on_checkpoint: Checkpoint | None = None,
    resume_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = copy.deepcopy(resume_state) if resume_state else initial_state(request)
    if state["request"] != request.model_dump(mode="json"):
        raise ValueError("checkpoint_request_mismatch")
    if state.get("data_fingerprint") not in {"", engine.fingerprint}:
        raise ValueError("checkpoint_dataset_mismatch")
    if request.mode == "agent" and provider is None:
        provider = OllamaProvider()
    if (
        resume_state
        and request.mode == "agent"
        and provider
        and state["metrics"].get("llm_calls", 0)
    ):
        if (
            state["metrics"].get("provider") != provider.name
            or state["metrics"].get("model") != provider.model
        ):
            raise ValueError("checkpoint_provider_mismatch")
    state["metrics"]["provider"] = (
        provider.name if request.mode == "agent" and provider else "deterministic_baseline"
    )
    state["metrics"]["model"] = provider.model if request.mode == "agent" and provider else "none"
    if state.get("provider_inflight"):
        state["provider_inflight"] = False
        state["metrics"]["usage_unknown"] = True
        state["status"] = "partial"
        state["errors"].append("provider_call_interrupted_usage_unknown")
        state["phase"] = "finalize"
    if state["phase"] == "done":
        return state["report"]
    graph = build_graph(engine, provider, request, before_provider=on_checkpoint)
    last: dict[str, Any] = state
    for snapshot in graph.stream(state, stream_mode="values", config={"recursion_limit": 64}):
        last = snapshot
        if on_checkpoint:
            on_checkpoint(copy.deepcopy(snapshot))
    return last["report"]
