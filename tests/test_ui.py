"""Verify that chart encodings preserve the inspected evidence."""

import pytest
from streamlit.testing.v1 import AppTest

from metric_detective.ui import (
    daily_chart,
    decomposition_chart,
    evidence_for,
    money,
    numeric,
    percent,
    segment_chart,
)


def test_undefined_and_zero_are_distinct_in_display():
    assert money(None) == "No definido"
    assert money("0.00") == "0.00 MXN"
    assert money("18322.62", None) == "18,322.62"
    assert percent(None) == "No definido"
    assert percent("0") == "+0.00%"
    assert numeric("NaN") is None
    assert numeric("Infinity") is None


def test_daily_plot_aligns_week_days_without_conflating_dates():
    evidence = {
        "values": {
            "days": [
                {
                    "date": "2026-01-12",
                    "period": "comparison",
                    "day_index": 0,
                    "revenue": "0.00",
                    "orders": 0,
                },
                {
                    "date": "2026-01-05",
                    "period": "reference",
                    "day_index": 0,
                    "revenue": "125.50",
                    "orders": 1,
                },
            ]
        }
    }
    figure = daily_chart(evidence, "MXN")
    assert list(figure.data[0].x) == [1]
    assert list(figure.data[1].x) == [1]
    assert list(figure.data[0].y) == [125.50]
    assert list(figure.data[1].y) == [0]
    assert figure.data[1].customdata[0][0] == "2026-01-12"


def test_decomposition_plot_does_not_double_count_interaction():
    evidence = {
        "values": {"volume_effect": "-150.00", "ticket_effect": "-50.00", "interaction": "20.00"}
    }
    comparison = {
        "values": {"reference": {"revenue": "1000.00"}, "comparison": {"revenue": "800.00"}}
    }
    figure = decomposition_chart(evidence, comparison, "MXN")
    assert list(figure.data[0].y) == [1000, -150, -50, 0]
    assert list(figure.data[0].measure) == ["absolute", "relative", "relative", "total"]


def test_segment_chart_retains_positive_negative_and_zero_contributions():
    evidence = {
        "values": {
            "segments": [
                {"segment": "mobile", "delta_revenue": "-150.00"},
                {"segment": "desktop", "delta_revenue": "50.00"},
                {"segment": "tablet", "delta_revenue": "0.00"},
            ]
        }
    }
    figure = segment_chart(evidence, "MXN")
    assert sum(figure.data[0].x) == pytest.approx(-100)
    assert set(figure.data[0].y) == {"mobile", "desktop", "tablet"}
    assert len(figure.data[0].x) == 3


def test_missing_analysis_does_not_create_evidence():
    assert evidence_for({"evidence": [{"tool": "check_data_quality"}]}, "compare_periods") == []


def _report_app(report):
    from metric_detective.ui import render_report

    render_report(report, replay=True)


@pytest.mark.parametrize("scenario_id", ["mobile_drop", "duplicate_reference", "empty_period"])
def test_report_renders_with_real_evidence_and_explicit_replay(tmp_path, scenario_id):
    from datetime import date
    from pathlib import Path

    from metric_detective.agent import run_investigation
    from metric_detective.contracts import InvestigationRequest
    from metric_detective.engine import AnalysisEngine
    from metric_detective.synthetic import generate_all

    manifest = generate_all(tmp_path / "datasets", seed=17)
    scenario = next(item for item in manifest if item["id"] == scenario_id)
    request = InvestigationRequest(
        dataset_id=scenario_id,
        reference_start=date(2026, 1, 5),
        comparison_start=date(2026, 1, 12),
        config=scenario["config"],
        mode="baseline",
    )
    with AnalysisEngine(
        Path(scenario["orders_path"]), Path(scenario["loads_path"]), request
    ) as engine:
        report = run_investigation(engine, request)
    app = AppTest.from_function(_report_app, args=(report,)).run(timeout=30)
    assert not app.exception
    assert "REPRODUCCIÓN GRABADA" in app.info[0].value
    if scenario_id == "duplicate_reference":
        assert not app.metric
        assert len(app.get("plotly_chart")) == 0
    else:
        assert len(app.metric) == 3
        assert len(app.get("plotly_chart")) >= 3
