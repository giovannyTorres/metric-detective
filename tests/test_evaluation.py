from pathlib import Path

import pytest

from metric_detective.agent import run_investigation
from metric_detective.contracts import InvestigationRequest
from metric_detective.engine import AnalysisEngine
from metric_detective.evaluation import evaluate, numerical_checks, score_report
from metric_detective.synthetic import generate_scenario


def test_evaluator_recomputes_from_raw_csv_independently(tmp_path):
    item = generate_scenario(tmp_path, "mobile_drop", seed=101)
    request = InvestigationRequest(
        dataset_id="eval",
        reference_start=item["reference_start"],
        comparison_start=item["comparison_start"],
        config=item["config"],
        mode="baseline",
    )
    orders = Path(item["orders_path"])
    report = run_investigation(AnalysisEngine(orders, Path(item["loads_path"]), request), request)
    result = numerical_checks(report, orders)
    assert result["correct"] == result["total"] > 0
    compare = next(e for e in report["evidence"] if e["tool"] == "compare_periods")
    compare["values"]["reference"]["revenue"] = "9999999"
    changed = numerical_checks(report, orders)
    assert changed["correct"] < changed["total"]


def test_observable_segment_and_abstention_scoring(tmp_path):
    item = generate_scenario(tmp_path, "incomplete_loads", seed=101)
    request = InvestigationRequest(
        dataset_id="eval",
        reference_start=item["reference_start"],
        comparison_start=item["comparison_start"],
        config=item["config"],
        mode="baseline",
    )
    report = run_investigation(
        AnalysisEngine(Path(item["orders_path"]), Path(item["loads_path"]), request), request
    )
    score = score_report(report, Path(item["orders_path"]), "incomplete_loads")
    assert score["abstention_correct"] == score["abstention_total"] == 1
    assert score["numeric"]["total"] == 0
    assert score["relevant_segment_identified"] is None


def test_smoke_is_explicitly_not_real_llm_evaluation(tmp_path):
    result = evaluate(
        tmp_path / "smoke", seeds=(101,), scenarios=["mobile_drop", "incomplete_loads"]
    )
    assert result["evaluation_kind"] == "deterministic_test_double_smoke"
    assert result["provider"] == "scripted_test_double"
    assert len(result["records"]) == 4
    assert result["summary"]["agent"]["llm_calls"] == 0
    assert result["summary"]["agent"]["input_tokens"] == 0
    assert result["summary"]["agent"]["relevant_segments_identified"] == 1
    assert (tmp_path / "smoke" / "results.json").is_file()


def test_development_seed_cannot_be_used_as_holdout(tmp_path):
    with pytest.raises(ValueError, match="development_seed"):
        evaluate(tmp_path, seeds=(17,))
