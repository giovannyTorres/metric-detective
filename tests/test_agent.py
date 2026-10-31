"""Credential-free graph tests use an explicitly identified provider DOUBLE."""

import copy
import json
import time
from pathlib import Path

import pytest

from metric_detective.agent import _aggregate_context, initial_state, run_investigation
from metric_detective.contracts import InvestigationRequest
from metric_detective.engine import AnalysisEngine
from metric_detective.provider import ProviderError, ScriptedProvider
from metric_detective.synthetic import generate_scenario


def make_engine(tmp_path: Path, scenario="mobile_drop", *, mode="agent", limits=None, loads=True):
    item = generate_scenario(tmp_path, scenario, seed=17)
    request = InvestigationRequest(
        dataset_id="opaque_dataset",
        reference_start=item["reference_start"],
        comparison_start=item["comparison_start"],
        config=item["config"],
        mode=mode,
        limits=limits or {},
    )
    return AnalysisEngine(
        Path(item["orders_path"]), Path(item["loads_path"]) if loads else None, request
    ), request


@pytest.mark.parametrize(
    "scenario",
    [
        "mobile_drop",
        "ticket_drop",
        "mixed_segments",
        "ambiguous",
        "no_change",
        "empty_period",
        "services_ticket_drop",
    ],
)
def test_double_runs_bounded_observation_graph(tmp_path, scenario):
    engine, request = make_engine(tmp_path, scenario)
    report = run_investigation(engine, request, ScriptedProvider())
    assert report["status"] == "completed"
    assert report["metrics"]["analytic_calls"] <= 8
    assert report["metrics"]["llm_calls"] == 0
    assert report["metrics"]["provider"] == "scripted_test_double"
    assert report["metrics"]["cost"] is None
    assert len({e["id"] for e in report["evidence"]}) == len(report["evidence"])


@pytest.mark.parametrize("scenario", ["incomplete_loads", "duplicate_reference"])
def test_data_quality_stops_before_business_analysis(tmp_path, scenario):
    engine, request = make_engine(tmp_path, scenario)
    report = run_investigation(engine, request, ScriptedProvider())
    assert report["status"] == "blocked_data_quality"
    assert [e["tool"] for e in report["evidence"]] == ["check_data_quality"]
    assert report["decisions"] == []


def test_missing_loads_remains_explicitly_unverified(tmp_path):
    engine, request = make_engine(tmp_path, loads=False)
    report = run_investigation(engine, request, ScriptedProvider())
    assert report["status"] == "partial"
    assert any("complet" in text for text in report["missing_data"])


def test_results_change_tool_selection(tmp_path):
    first_tools = []
    for scenario in ("mobile_drop", "ticket_drop"):
        engine, request = make_engine(tmp_path, scenario)
        report = run_investigation(engine, request, ScriptedProvider())
        first_tools.append(report["decisions"][0]["tool"])
    assert first_tools == ["rank_segment_changes", "decompose_revenue_change"]


def test_fixed_baseline_has_same_tools_and_no_model(tmp_path):
    engine, request = make_engine(tmp_path, mode="baseline")
    report = run_investigation(engine, request)
    assert report["status"] == "completed"
    assert report["metrics"]["analytic_calls"] == 8
    assert report["metrics"]["llm_calls"] == 0
    assert report["metrics"]["provider"] == "deterministic_baseline"


def test_calls_cap_includes_mandatory_steps(tmp_path):
    engine, request = make_engine(tmp_path, limits={"max_calls": 3})
    report = run_investigation(engine, request, ScriptedProvider())
    assert report["status"] == "partial"
    assert report["metrics"]["analytic_calls"] == 3
    assert "analytical_call_limit" in report["errors"]


def test_max_depth_guard(tmp_path):
    engine, request = make_engine(tmp_path, limits={"max_depth": 1, "max_retries": 0})
    double = ScriptedProvider(
        [
            {"tool": "rank_segment_changes", "dimension": "device", "reason_code": "volume_change"},
            {
                "tool": "drill_down",
                "dimensions": ["device", "channel"],
                "reason_code": "interaction_check",
            },
        ]
    )
    report = run_investigation(engine, request, double)
    assert report["status"] == "partial"
    assert "drill_down" not in {e["tool"] for e in report["evidence"]}


@pytest.mark.parametrize(
    "depth,invalid_decision",
    [
        (
            1,
            {
                "tool": "inspect_daily_pattern",
                "filters": {"device": "mobile", "channel": "organic"},
                "reason_code": "daily_pattern",
            },
        ),
        (
            2,
            {
                "tool": "drill_down",
                "dimensions": ["device", "channel"],
                "filters": {"country": "MX"},
                "reason_code": "interaction_check",
            },
        ),
    ],
)
def test_excessive_filter_depth_uses_bounded_selection_retry(tmp_path, depth, invalid_decision):
    engine, request = make_engine(tmp_path, limits={"max_depth": depth, "max_retries": 1})
    double = ScriptedProvider(
        [
            {"tool": "rank_segment_changes", "dimension": "device", "reason_code": "volume_change"},
            invalid_decision,
            {"tool": "finish", "reason_code": "insufficient_evidence"},
        ]
    )
    report = run_investigation(engine, request, double)
    assert report["status"] == "partial"
    assert report["metrics"]["rejected_selections"] == 1
    assert report["metrics"]["analytic_calls"] == 3
    assert report["errors"] == []
    assert [d["tool"] for d in report["decisions"]] == ["rank_segment_changes", "finish"]


def test_finish_prerequisites_enforced_by_code(tmp_path):
    engine, request = make_engine(tmp_path, limits={"max_retries": 0})
    report = run_investigation(
        engine,
        request,
        ScriptedProvider(
            [
                {"tool": "finish", "reason_code": "sufficient_evidence"},
            ]
        ),
    )
    assert report["status"] == "partial"
    assert "missing_required_analysis_before_finish" in report["errors"][0]


def test_contradicted_no_change_reason_is_rejected(tmp_path):
    engine, request = make_engine(tmp_path, limits={"max_retries": 0})
    report = run_investigation(
        engine,
        request,
        ScriptedProvider(
            [
                {"tool": "finish", "reason_code": "no_relevant_change"},
            ]
        ),
    )
    assert report["status"] == "partial"
    assert "contradicted_no_relevant_change" in report["errors"][0]


def test_injected_tool_parameter_cannot_be_executed(tmp_path):
    engine, request = make_engine(tmp_path, limits={"max_retries": 0})
    report = run_investigation(
        engine,
        request,
        ScriptedProvider(
            [
                {
                    "tool": "rank_segment_changes",
                    "dimension": "device; DROP TABLE orders",
                    "reason_code": "volume_change",
                },
            ]
        ),
    )
    assert report["status"] == "partial"
    assert report["metrics"]["analytic_calls"] == 2
    assert "invalid_dimension" in report["errors"][0]


def test_repeat_selection_has_bounded_retry(tmp_path):
    engine, request = make_engine(tmp_path)
    report = run_investigation(
        engine,
        request,
        ScriptedProvider(
            [
                {"tool": "decompose_revenue_change", "reason_code": "ticket_change"},
                {"tool": "decompose_revenue_change", "reason_code": "ticket_change"},
            ]
        ),
    )
    assert report["status"] == "completed"
    assert report["metrics"]["rejected_selections"] == 1
    assert sum(e["tool"] == "decompose_revenue_change" for e in report["evidence"]) == 1


def test_provider_error_is_safe_and_partial(tmp_path):
    class UnavailableProvider:
        name = "unavailable_test_double"
        model = "none"

        def select(self, *args, **kwargs):
            raise ProviderError("provider_unavailable_or_invalid_response", calls=1)

    engine, request = make_engine(tmp_path)
    report = run_investigation(engine, request, UnavailableProvider())
    assert report["status"] == "partial"
    assert report["metrics"]["llm_calls"] == 1
    assert report["metrics"]["usage_unknown"] is True
    assert report["errors"] == ["provider_unavailable_or_invalid_response"]


def test_resume_after_real_graph_node_does_not_duplicate_evidence(tmp_path):
    engine, request = make_engine(tmp_path, mode="baseline")
    checkpoint = None

    def interrupt(state):
        nonlocal checkpoint
        if state["phase"] == "select" and len(state["evidence"]) == 4:
            checkpoint = json.loads(json.dumps(state))
            raise InterruptedError("test interruption after persisted node")

    with pytest.raises(InterruptedError):
        run_investigation(engine, request, on_checkpoint=interrupt)
    recovered = run_investigation(engine, request, resume_state=checkpoint)
    uninterrupted = run_investigation(engine, request)
    assert recovered["status"] == "completed"
    assert recovered["evidence"] == uninterrupted["evidence"]
    assert recovered["metrics"]["analytic_calls"] == 8


def test_interrupted_provider_is_not_called_again(tmp_path):
    engine, request = make_engine(tmp_path)
    checkpoint = None

    def interrupt(state):
        nonlocal checkpoint
        if state.get("provider_inflight"):
            checkpoint = copy.deepcopy(state)
            raise InterruptedError("provider intent saved before network")

    with pytest.raises(InterruptedError):
        run_investigation(engine, request, ScriptedProvider(), on_checkpoint=interrupt)
    double = ScriptedProvider()
    report = run_investigation(engine, request, double, resume_state=checkpoint)
    assert double._index == 0
    assert report["status"] == "partial"
    assert report["metrics"]["usage_unknown"] is True
    assert report["errors"] == ["provider_call_interrupted_usage_unknown"]


def test_recovery_budget_includes_downtime(tmp_path):
    engine, request = make_engine(tmp_path, mode="baseline", limits={"max_seconds": 1})
    state = initial_state(request)
    state["started_at"] = time.time() - 2
    report = run_investigation(engine, request, resume_state=state)
    assert report["status"] == "failed"
    assert report["metrics"]["analytic_calls"] == 0


def test_prompt_has_no_generator_labels_or_order_identifiers(tmp_path):
    engine, request = make_engine(tmp_path)
    state = initial_state(request)
    state["evidence"] = [
        engine.run(t).model_dump(mode="json") for t in ("check_data_quality", "compare_periods")
    ]
    serialized = json.dumps(_aggregate_context(state, request))
    assert "opaque_dataset" not in serialized
    assert "mobile_drop" not in serialized
    assert "s17-p0-" not in serialized
    assert "order_id" not in serialized


def test_checkpoint_fingerprint_cannot_be_reused_with_different_data(tmp_path):
    engine, request = make_engine(tmp_path)
    state = initial_state(request)
    state["data_fingerprint"] = "changed"
    with pytest.raises(ValueError, match="checkpoint_dataset_mismatch"):
        run_investigation(engine, request, ScriptedProvider(), resume_state=state)


def test_resume_does_not_mix_models_in_one_investigation(tmp_path):
    engine, request = make_engine(tmp_path)
    state = initial_state(request)
    state["metrics"].update(llm_calls=1, provider="ollama", model="old-model")
    with pytest.raises(ValueError, match="checkpoint_provider_mismatch"):
        run_investigation(engine, request, ScriptedProvider(), resume_state=state)
