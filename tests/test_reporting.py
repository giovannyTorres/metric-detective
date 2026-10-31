import copy
import json
from pathlib import Path

import pytest

from metric_detective.agent import run_investigation
from metric_detective.contracts import InvestigationRequest
from metric_detective.engine import AnalysisEngine
from metric_detective.reporting import canonical_findings, render_html, validate_report
from metric_detective.synthetic import generate_scenario


@pytest.fixture
def report(tmp_path):
    item = generate_scenario(tmp_path, "mobile_drop", seed=17)
    request = InvestigationRequest(
        dataset_id="test",
        reference_start=item["reference_start"],
        comparison_start=item["comparison_start"],
        config=item["config"],
        mode="baseline",
    )
    return run_investigation(
        AnalysisEngine(Path(item["orders_path"]), Path(item["loads_path"]), request), request
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("value", "999999"),
        ("unit", "USD"),
        ("evidence_id", "nonexistent"),
        ("text", "Una falla de checkout causó la caída de ingresos."),
    ],
)
def test_rejects_number_unit_reference_and_semantic_tampering(report, field, value):
    report["findings"][0][field] = value
    with pytest.raises(ValueError):
        validate_report(report)


def test_rejects_causal_text_even_with_valid_evidence_id(report):
    claim = report["findings"][0]
    assert claim["evidence_id"] in {e["id"] for e in report["evidence"]}
    claim["text"] = "El canal orgánico provocó la caída."
    with pytest.raises(ValueError, match="semantics"):
        validate_report(report)


def test_rejects_changed_period(report):
    report["evidence"][0]["periods"]["reference"]["start"] = "2026-01-06"
    report["findings"] = canonical_findings(report["evidence"], "MXN")
    with pytest.raises(ValueError, match="period"):
        validate_report(report)


@pytest.mark.parametrize("units", [{}, {"revenue": "USD"}])
def test_rejects_missing_or_wrong_evidence_units(report, units):
    next(e for e in report["evidence"] if e["tool"] == "compare_periods")["units"] = units
    with pytest.raises(ValueError, match="unit"):
        validate_report(report)


def test_rejects_cross_tool_contradiction_even_if_internal_sum_matches(report):
    item = next(e for e in report["evidence"] if e["tool"] == "decompose_revenue_change")
    item["values"]["volume_effect"] = "1"
    item["values"]["ticket_effect"] = "2"
    item["values"]["delta_revenue"] = "3"
    report["findings"] = canonical_findings(report["evidence"], "MXN")
    with pytest.raises(ValueError, match="contradicts_comparison"):
        validate_report(report)


def test_rejects_daily_totals_that_disagree_with_comparison(report):
    item = next(e for e in report["evidence"] if e["tool"] == "inspect_daily_pattern")
    item["values"]["days"][0]["revenue"] = "999999"
    with pytest.raises(ValueError, match="daily_contradicts"):
        validate_report(report)


def test_rejects_fabricated_hypothesis(report):
    report["hypotheses"][0]["text"] = "La publicidad defectuosa es la causa demostrada."
    with pytest.raises(ValueError, match="hypothesis"):
        validate_report(report)


def test_exports_json_roundtrip_and_offline_html(report):
    restored = json.loads(json.dumps(report))
    validate_report(restored)
    html = render_html(restored)
    assert "Evolución diaria" in html
    assert "Contribuciones por device" in html
    assert "Descomposición exacta" in html
    assert "plotly.js" in html
    assert "<script src=" not in html
    assert all(e["id"] in html for e in report["evidence"])
    header = html.split("</header>", 1)[0]
    assert "Referencia:</strong> 2026-01-05 → 2026-01-12" in header
    assert "Comparación:</strong> 2026-01-12 → 2026-01-19" in header
    assert "fin exclusivo" in header
    assert "Zona horaria: America/Mexico_City · Moneda: MXN" in header
    assert "Proveedor: deterministic_baseline · Modelo: none" in header
    decisions = html.split("<h2>Decisiones registradas</h2>", 1)[1]
    first_decision = report["decisions"][0]
    assert first_decision["summary"] in decisions
    assert f"href='#{first_decision['evidence_ids'][0]}'" in decisions
    assert "Métricas de ejecución" in decisions
    assert "Tokens de entrada medidos" in decisions

    partial = copy.deepcopy(restored)
    partial["status"] = "partial"
    partial["errors"] = ["token_budget_exhausted"]
    partial["metrics"].update(model="<model&name>", provider="<provider>")
    partial["decisions"][0]["summary"] = "<script>alert(1)</script>"
    exported_partial = render_html(partial)
    partial_header = exported_partial.split("</header>", 1)[0]
    assert "Incidencias de ejecución" in partial_header
    assert "token_budget_exhausted" in partial_header
    assert "&lt;model&amp;name&gt;" in partial_header
    assert "&lt;provider&gt;" in partial_header
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in exported_partial
    assert "<script>alert(1)</script>" not in exported_partial


def test_rejects_unexplained_deleted_limitations(report):
    altered = copy.deepcopy(report)
    altered["missing_data"] = []
    with pytest.raises(ValueError, match="missing_information"):
        validate_report(altered)
