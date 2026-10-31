"""Verify that chart encodings preserve the inspected evidence."""

import pytest
from streamlit.testing.v1 import AppTest

from metric_detective.ui import (
    concise_findings,
    daily_chart,
    decomposition_chart,
    evidence_for,
    job_progress,
    load_local_replay,
    money,
    numeric,
    percent,
    segment_chart,
    segment_label,
)


def test_undefined_and_zero_are_distinct_in_display():
    assert money(None) == "No definido"
    assert money("0.00") == "0.00 MXN"
    assert money("18322.62", None) == "18,322.62"
    assert money("99999999999999999.99", None) == "99,999,999,999,999,999.99"
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
    assert set(figure.data[0].y) == {"Móvil", "Escritorio", "Tableta"}
    assert len(figure.data[0].x) == 3


def test_missing_analysis_does_not_create_evidence():
    assert evidence_for({"evidence": [{"tool": "check_data_quality"}]}, "compare_periods") == []


def test_spanish_segment_display_keeps_unknown_values_and_country_meaning():
    assert segment_label("MX", "country") == "México"
    assert (
        segment_label({"device": "mobile", "country": "US"})
        == "Dispositivo: Móvil · País: Estados Unidos"
    )
    assert segment_label("new-custom-value", "channel") == "new-custom-value"


def test_progress_uses_recorded_stage_and_evidence_count():
    stage, count = job_progress(
        {"status": "running", "progress": {"phase": "execute", "evidence_count": 4}}
    )
    assert "registrando evidencia" in stage
    assert count == 4
    assert job_progress({"status": "queued"})[1] is None


def _poll_nonterminal_job(job_status):
    from unittest.mock import patch

    import metric_detective.ui as ui

    job = {
        "id": "running-job",
        "status": job_status,
        "progress": {"phase": "select", "evidence_count": 2},
    }
    with patch.object(ui, "api", return_value=job):
        ui.poll_job(job["id"])


@pytest.mark.parametrize("job_status", ["queued", "running"])
def test_nonterminal_database_job_keeps_status_indicator_running(job_status):
    app = AppTest.from_function(_poll_nonterminal_job, args=(job_status,)).run(timeout=30)
    assert not app.exception
    assert len(app.status) == 1
    assert "running" in app.status[0].state.lower()
    assert any("2 resultados de evidencia" in caption.value for caption in app.caption)


def test_local_replay_is_available_without_database_or_api():
    report = load_local_replay()
    assert report["evidence"]
    assert report["metrics"]["provider"] == "ollama"


def _offline_workspace():
    from unittest.mock import patch

    import metric_detective.ui as ui

    with patch.object(ui, "api", side_effect=ui.APIError("offline", code="connection_unavailable")):
        ui.main()


def test_api_down_guides_start_and_offline_replay_without_execution():
    app = AppTest.from_function(_offline_workspace).run(timeout=30)
    assert not app.exception
    assert any("local start --open" in code.value for code in app.code)
    next(
        button for button in app.button if button.label == "Explorar informe sin conexión"
    ).click().run(timeout=30)
    assert not app.exception
    assert app.radio[0].value == "Reproducción grabada"
    assert any("REPRODUCCIÓN GRABADA" in item.value for item in app.info)
    assert any("SIN CONEXIÓN CON LA API" in item.value for item in app.caption)


def _workspace_model_unavailable(worker_status="ok"):
    from unittest.mock import patch

    import streamlit as st

    import metric_detective.ui as ui
    from metric_detective.contracts import MetricConfig

    def service(method, path, **kwargs):
        if path == "/health/details":
            return {
                "status": "degraded",
                "database": {"status": "ok"},
                "worker": {"status": worker_status},
                "ollama": {
                    "status": "model_missing",
                    "model_available": False,
                    "model": "not-installed",
                },
            }
        if path == "/scenarios":
            return [
                {
                    "id": "demo",
                    "dataset_id": "demo",
                    "name": "Escenario sintético",
                    "config": MetricConfig().model_dump(mode="json"),
                    "reference_start": "2026-01-05",
                    "comparison_start": "2026-01-12",
                }
            ]
        if method == "POST" and path == "/investigations":
            st.session_state["submitted_request"] = kwargs["json"]
            return {"id": "test-durable-job", "status": "queued"}
        if path == "/investigations/test-durable-job":
            return {"id": "test-durable-job", "status": "queued", "progress": {}}
        raise AssertionError(f"unexpected API path: {path}")

    with patch.object(ui, "api", side_effect=service):
        ui.main()


def test_missing_model_defaults_to_baseline_and_submits_no_model_job():
    app = AppTest.from_function(_workspace_model_unavailable).run(timeout=30)
    assert not app.exception
    method = next(item for item in app.radio if item.label == "Método de investigación")
    assert method.value == "Punto de referencia · secuencia fija"
    next(button for button in app.button if button.label == "Investigar caída").click().run(
        timeout=30
    )
    assert not app.exception
    assert app.session_state["submitted_request"]["mode"] == "baseline"
    assert app.session_state["submitted_request"]["dataset_id"] == "demo"


def test_missing_worker_blocks_submission_and_is_not_reported_healthy():
    app = AppTest.from_function(_workspace_model_unavailable, args=("missing",)).run(timeout=30)
    assert not app.exception
    assert next(button for button in app.button if button.label == "Investigar caída").disabled
    assert any("trabajador activo" in warning.value for warning in app.warning)
    next(item for item in app.radio if item.label == "Espacio de trabajo").set_value(
        "Estado del sistema"
    ).run(timeout=30)
    assert not app.exception
    assert (
        next(metric for metric in app.metric if metric.label == "Trabajador").value
        == "Requiere atención"
    )


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
    original = __import__("copy").deepcopy(report)
    assert {finding["id"] for finding in concise_findings(report)}.issubset(
        {finding["id"] for finding in report["findings"]}
    )
    app = AppTest.from_function(_report_app, args=(report,)).run(timeout=30)
    assert not app.exception
    assert "REPRODUCCIÓN GRABADA" in app.info[0].value
    assert report == original
    if scenario_id == "duplicate_reference":
        assert not app.metric
        assert len(app.get("plotly_chart")) == 0
    else:
        assert len(app.metric) == 3
        assert len(app.get("plotly_chart")) >= 3
