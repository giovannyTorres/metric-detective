"""Canonical, evidence-checked findings and a self-contained HTML export."""

from __future__ import annotations

import html
import json
from decimal import Decimal
from typing import Any

from .contracts import Evidence, InvestigationRequest


def _decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite_report_value")
    return result


def _display(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _number(value: Any) -> str:
    return format(_decimal(value), ".2f")


def _claim(
    evidence: dict[str, Any], kind: str, path: str, value: Any, unit: str, text: str
) -> dict[str, Any]:
    return {
        "id": f"{evidence['id']}:{path}",
        "kind": kind,
        "text": text,
        "evidence_id": evidence["id"],
        "metric": path.rsplit(".", 1)[-1],
        "path": path,
        "value": value,
        "unit": unit,
        "periods": evidence["periods"],
    }


def canonical_findings(evidence: list[dict[str, Any]], currency: str) -> list[dict[str, Any]]:
    """Every permitted conclusion is an explicit observation, never a cause."""
    findings: list[dict[str, Any]] = []
    for item in evidence:
        values, tool = item["values"], item["tool"]
        if tool == "check_data_quality" and values.get("status") != "ok":
            value = values["status"]
            text = (
                "La calidad de los datos impide el diagnóstico de negocio."
                if value == "blocked"
                else "La completitud de las cargas no está verificada."
            )
            findings.append(_claim(item, "data_quality", "status", value, "status", text))
        if tool == "compare_periods":
            for period, label in (("reference", "referencia"), ("comparison", "comparación")):
                amount = values[period]["revenue"]
                findings.append(
                    _claim(
                        item,
                        "observed_revenue",
                        f"{period}.revenue",
                        amount,
                        currency,
                        f"Ingresos de {label}: {_number(amount)} {currency}.",
                    )
                )
            value = values["delta_revenue"]
            findings.append(
                _claim(
                    item,
                    "observed_change",
                    "delta_revenue",
                    value,
                    currency,
                    f"Cambio observado en ingresos: {_number(value)} {currency}.",
                )
            )
            if values.get("delta_pct") is not None:
                value = values["delta_pct"]
                findings.append(
                    _claim(
                        item,
                        "observed_change_pct",
                        "delta_pct",
                        value,
                        "%",
                        f"Variación relativa frente a la referencia: {_number(value)} %.",
                    )
                )
            if "delta_orders" in values:
                value = values["delta_orders"]
                findings.append(
                    _claim(
                        item,
                        "observed_orders",
                        "delta_orders",
                        value,
                        "orders",
                        f"Cambio observado en pedidos pagados: {_display(value)} pedidos.",
                    )
                )
            if values.get("relevant_change") is False:
                findings.append(
                    _claim(
                        item,
                        "no_relevant_change",
                        "relevant_change",
                        False,
                        "boolean",
                        "El cambio observado no supera el umbral de relevancia configurado.",
                    )
                )
        if tool == "decompose_revenue_change":
            for key, label in (("volume_effect", "volumen"), ("ticket_effect", "ticket promedio")):
                value = values[key]
                findings.append(
                    _claim(
                        item,
                        "arithmetic_contribution",
                        key,
                        value,
                        currency,
                        f"Contribución aritmética de {label}: {_number(value)} {currency}; no demuestra causalidad.",
                    )
                )
        if tool in {"rank_segment_changes", "drill_down"}:
            segments = values.get("segments", [])
            indices = sorted(
                range(len(segments)),
                key=lambda i: abs(_decimal(segments[i]["delta_revenue"])),
                reverse=True,
            )[:3]
            for index in indices:
                segment = segments[index]
                label = _display(segment["segment"])
                dimension = values.get("dimension") or ", ".join(values.get("dimensions", []))
                value = segment["delta_revenue"]
                findings.append(
                    _claim(
                        item,
                        "segment_change",
                        f"segments.{index}.delta_revenue",
                        value,
                        currency,
                        f"En la partición {dimension}, el segmento {label} presenta un cambio de {_number(value)} {currency}.",
                    )
                )
    return findings


def canonical_hypotheses(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparison = next((e for e in evidence if e["tool"] == "compare_periods"), None)
    if comparison is None or _decimal(comparison["values"]["delta_revenue"]) >= 0:
        return []
    return [
        {
            "text": "El cambio puede ser compatible con variaciones de adquisición, conversión, precios o mezcla; la información disponible no permite distinguir esas explicaciones.",
            "evidence_ids": [comparison["id"]],
            "next_check": "Contrastar sesiones, conversión, precios, promociones e incidencias por los mismos periodos y segmentos; una atribución causal requiere evidencia adicional.",
        }
    ]


def missing_information(evidence: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    quality = next((e for e in evidence if e["tool"] == "check_data_quality"), None)
    if quality and quality["values"].get("status") == "unverified":
        missing.append(
            "No se dispone de cargas completas verificadas para cada fecha y fuente esperada."
        )
    comparison = next((e for e in evidence if e["tool"] == "compare_periods"), None)
    if comparison:
        if comparison["values"]["reference"].get("orders", 0) == 0:
            missing.append(
                "La referencia no contiene pedidos pagados: el porcentaje de cambio y su ticket no están definidos."
            )
        if comparison["values"]["comparison"].get("orders", 0) == 0:
            missing.append(
                "La comparación no contiene pedidos pagados: su ticket no está definido."
            )
        missing.append(
            "Los pedidos y las cargas no incluyen sesiones, conversión, precios unitarios ni intervenciones para identificar causas."
        )
    for item in evidence:
        for warning in item.get("warnings", []):
            text = {
                "partitions_overlap_across_dimensions_do_not_add_device_channel_country_contributions": "Las contribuciones de particiones diferentes se superponen: no se suman entre dispositivo, canal y país.",
                "contribution_percent_undefined_when_partition_delta_zero": "El porcentaje de contribución no está definido cuando el cambio total de la partición es cero.",
                "zero_reference_revenue_percentage_undefined": "El porcentaje de cambio no está definido porque los ingresos de referencia son cero.",
                "empty_period_ticket_unobserved_carry_observed_ticket_for_decomposition": "Un periodo vacío no tiene ticket observado: la descomposición usa el ticket del otro periodo y asigna la entrada o salida al volumen.",
                "load_completeness_unverified": "La completitud de las cargas no está verificada.",
                "duplicate_order_identifiers": "Hay identificadores de pedido duplicados; se debe corregir la fuente antes de diagnosticar el negocio.",
                "mixed_or_unexpected_currency": "La fuente contiene monedas mezcladas o diferentes de la moneda configurada.",
                "duplicate_load_declarations": "Hay declaraciones de carga duplicadas para la misma fecha y fuente.",
                "explicit_incomplete_or_failed_loads": "Se declararon cargas incompletas o fallidas en los periodos seleccionados.",
            }.get(
                warning,
                "La fuente presenta una advertencia adicional; consulta su código en la evidencia.",
            )
            if text not in missing:
                missing.append(text)
    return missing


def build_report(state: dict[str, Any], request: InvestigationRequest) -> dict[str, Any]:
    evidence = state.get("evidence", [])
    report = {
        "schema_version": "1.0",
        "status": state.get("status", "partial"),
        "mode": request.mode,
        "request": request.model_dump(mode="json"),
        "data_fingerprint": state.get("data_fingerprint", ""),
        "findings": canonical_findings(evidence, request.config.currency),
        "hypotheses": canonical_hypotheses(evidence),
        "missing_data": missing_information(evidence),
        "evidence": evidence,
        "decisions": state.get("decisions", []),
        "metrics": state.get("metrics", {}),
        "errors": state.get("errors", []),
        "limitations": [
            "Una moneda por investigación; ingresos = suma de importes de pedidos con estados incluidos.",
            "Las devoluciones quedan fuera del alcance; las contribuciones son aritméticas, no efectos causales.",
            "Cada partición suma su propio cambio; no se suman contribuciones entre dispositivo y canal.",
            "El umbral de relevancia es descriptivo y configurable; no es una prueba de significancia estadística.",
        ],
    }
    validate_report(report)
    return report


def _path(values: dict[str, Any], path: str) -> Any:
    result: Any = values
    for part in path.split("."):
        result = result[int(part)] if isinstance(result, list) else result[part]
    return result


def validate_report(report: dict[str, Any]) -> None:
    """Reject fabricated numbers, wrong units/periods, and unsupported semantics.

    Semantic support is enforced with a closed set of observation templates. Merely
    citing an existing evidence ID is intentionally insufficient. This is a narrow
    verifier, not a general natural-language entailment benchmark.
    """
    request = InvestigationRequest.model_validate(report["request"])
    if report.get("status") not in {"completed", "partial", "blocked_data_quality", "failed"}:
        raise ValueError("invalid_report_status")
    evidence = report["evidence"]
    comparison = next((e for e in evidence if e["tool"] == "compare_periods"), None)
    ids: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    for item in evidence:
        Evidence.model_validate(item)
        if item["id"] in ids or item["data_fingerprint"] != report["data_fingerprint"]:
            raise ValueError("invalid_evidence_identity")
        ids.add(item["id"])
        by_id[item["id"]] = item
        if item["periods"] != request.periods():
            raise ValueError("evidence_period_mismatch")
        values = item["values"]
        required_units = {
            "compare_periods": {"revenue", "delta_revenue", "aov", "orders", "delta_pct"},
            "decompose_revenue_change": {
                "volume_effect",
                "ticket_effect",
                "interaction",
                "delta_revenue",
            },
            "rank_segment_changes": {"revenue", "delta_revenue", "contribution_pct", "orders"},
            "drill_down": {"revenue", "delta_revenue", "contribution_pct", "orders"},
            "inspect_daily_pattern": {"revenue", "orders"},
        }.get(item["tool"], set())
        if not required_units.issubset(item["units"]):
            raise ValueError("evidence_unit_missing")
        for key, unit in item["units"].items():
            expected_unit = {
                "revenue": request.config.currency,
                "delta_revenue": request.config.currency,
                "volume_effect": request.config.currency,
                "ticket_effect": request.config.currency,
                "interaction": request.config.currency,
                "aov": f"{request.config.currency}/order",
                "orders": "orders",
                "delta_pct": "%",
                "contribution_pct": "%",
            }.get(key)
            if expected_unit is not None and unit != expected_unit:
                raise ValueError("evidence_unit_mismatch")
        if item["tool"] == "compare_periods":
            expected = _decimal(values["comparison"]["revenue"]) - _decimal(
                values["reference"]["revenue"]
            )
            if _decimal(values["delta_revenue"]) != expected:
                raise ValueError("revenue_change_does_not_reconcile")
        if item["tool"] == "decompose_revenue_change":
            if abs(
                _decimal(values["volume_effect"])
                + _decimal(values["ticket_effect"])
                - _decimal(values["delta_revenue"])
            ) > Decimal("0.00000001"):
                raise ValueError("decomposition_does_not_reconcile")
            if comparison and _decimal(values["delta_revenue"]) != _decimal(
                comparison["values"]["delta_revenue"]
            ):
                raise ValueError("decomposition_contradicts_comparison")
        if item["tool"] in {"rank_segment_changes", "drill_down"}:
            total = sum((_decimal(row["delta_revenue"]) for row in values["segments"]), Decimal(0))
            if abs(total - _decimal(values["delta_revenue"])) > Decimal("0.00000001"):
                raise ValueError("segment_partition_does_not_reconcile")
            if (
                comparison
                and not item.get("filters")
                and _decimal(values["delta_revenue"])
                != _decimal(comparison["values"]["delta_revenue"])
            ):
                raise ValueError("partition_contradicts_comparison")
        if item["tool"] == "inspect_daily_pattern" and comparison and not item.get("filters"):
            for period in ("reference", "comparison"):
                rows = [d for d in values["days"] if d["period"] == period]
                money = sum((_decimal(d["revenue"]) for d in rows), Decimal(0))
                orders = sum(d["orders"] for d in rows)
                if (
                    money != _decimal(comparison["values"][period]["revenue"])
                    or orders != comparison["values"][period]["orders"]
                ):
                    raise ValueError("daily_contradicts_comparison")
    expected_findings = canonical_findings(evidence, request.config.currency)
    if report.get("findings") != expected_findings:
        raise ValueError("unsupported_claim_semantics_or_value")
    for claim in report["findings"]:
        item = by_id.get(claim["evidence_id"])
        if (
            not item
            or _path(item["values"], claim["path"]) != claim["value"]
            or claim["periods"] != item["periods"]
        ):
            raise ValueError("unsupported_claim_reference")
    if report.get("hypotheses") != canonical_hypotheses(evidence):
        raise ValueError("unsupported_hypothesis")
    if report.get("missing_data") != missing_information(evidence):
        raise ValueError("missing_information_was_changed")
    for decision in report.get("decisions", []):
        if not set(decision.get("evidence_ids", [])).issubset(ids):
            raise ValueError("unsupported_decision_reference")
    quality = next((e for e in evidence if e["tool"] == "check_data_quality"), None)
    if report["status"] == "completed":
        if not quality or quality["values"].get("status") != "ok" or comparison is None:
            raise ValueError("completed_report_requires_verified_data")
        if comparison["values"].get("relevant_change") is not False:
            tools = {e["tool"] for e in evidence if not e.get("filters")}
            if not {
                "decompose_revenue_change",
                "inspect_daily_pattern",
                "rank_segment_changes",
            }.issubset(tools):
                raise ValueError("completed_report_missing_required_analyses")
    if quality and quality["values"].get("status") == "blocked":
        if report["status"] != "blocked_data_quality" or any(
            e["tool"] != "check_data_quality" for e in evidence
        ):
            raise ValueError("business_diagnosis_on_blocked_data")


def render_html(report: dict[str, Any]) -> str:
    """Offline HTML: embedded Plotly JS, evidence details, no CDN dependency."""
    import plotly.graph_objects as go

    validate_report(report)
    escape = html.escape
    definition = InvestigationRequest.model_validate(report["request"])
    periods = definition.periods()
    currency = report["request"]["config"]["currency"]
    evidence = report["evidence"]
    charts: list[str] = []
    daily = next(
        (e for e in evidence if e["tool"] == "inspect_daily_pattern" and not e.get("filters")), None
    )
    if daily:
        chart = go.Figure()
        for period, label, color in (
            ("reference", "Referencia", "#8b95a8"),
            ("comparison", "Comparación", "#0c8a7b"),
        ):
            rows = [d for d in daily["values"]["days"] if d["period"] == period]
            chart.add_scatter(
                x=[d["day_index"] for d in rows],
                y=[float(d["revenue"]) for d in rows],
                text=[d["date"] for d in rows],
                mode="lines+markers",
                name=label,
                line_color=color,
            )
        chart.update_layout(
            title="Evolución diaria",
            yaxis_title=currency,
            xaxis_title="Día de la semana comparada",
            template="plotly_white",
        )
        charts.append(chart.to_html(full_html=False, include_plotlyjs=True))
    segmentation = next((e for e in evidence if e["tool"] == "rank_segment_changes"), None)
    if segmentation:
        rows = segmentation["values"]["segments"]
        chart = go.Figure(
            go.Bar(
                x=[_display(d["segment"]) for d in rows],
                y=[float(d["delta_revenue"]) for d in rows],
                marker_color=[
                    "#cb4c52" if _decimal(d["delta_revenue"]) < 0 else "#0c8a7b" for d in rows
                ],
            )
        )
        chart.update_layout(
            title=f"Contribuciones por {segmentation['values'].get('dimension', 'segmento')}",
            yaxis_title=f"Cambio · {currency}",
            template="plotly_white",
        )
        charts.append(chart.to_html(full_html=False, include_plotlyjs=not charts))
    decomposition = next((e for e in evidence if e["tool"] == "decompose_revenue_change"), None)
    if decomposition:
        values = decomposition["values"]
        chart = go.Figure(
            go.Waterfall(
                x=["Volumen", "Ticket", "Cambio total"],
                y=[
                    float(values["volume_effect"]),
                    float(values["ticket_effect"]),
                    float(values["delta_revenue"]),
                ],
                measure=["relative", "relative", "total"],
            )
        )
        chart.update_layout(
            title="Descomposición exacta del cambio", yaxis_title=currency, template="plotly_white"
        )
        charts.append(chart.to_html(full_html=False, include_plotlyjs=not charts))
    claims = "".join(
        f"<li>{escape(f['text'])} <a href='#{escape(f['evidence_id'])}'>{escape(f['evidence_id'])}</a></li>"
        for f in report["findings"]
    )
    hypotheses = "".join(
        f"<li>{escape(h['text'])}<p><strong>Siguiente comprobación:</strong> {escape(h['next_check'])}</p></li>"
        for h in report["hypotheses"]
    )
    missing = "".join(f"<li>{escape(m)}</li>" for m in report["missing_data"])
    details = "".join(
        f"<details id='{escape(e['id'])}'><summary>{escape(e['tool'])} · {escape(e['id'])}</summary><pre>{escape(json.dumps(e, ensure_ascii=False, indent=2))}</pre></details>"
        for e in evidence
    )
    limitations = "".join(f"<li>{escape(m)}</li>" for m in report.get("limitations", []))
    status = escape(report["status"])
    mode = escape(report["mode"])
    metrics = report.get("metrics", {})
    period_labels = "".join(
        f"<p><strong>{label}:</strong> {escape(periods[key]['start'])} → {escape(periods[key]['end_exclusive'])} <span>(fin exclusivo)</span></p>"
        for key, label in (("reference", "Referencia"), ("comparison", "Comparación"))
    )
    error_labels = {
        "token_budget_exhausted": "El presupuesto de tokens no permite admitir otra solicitud al modelo.",
        "analytical_call_limit": "Se alcanzó el límite de llamadas analíticas.",
        "time_budget_exhausted": "Se alcanzó el límite de tiempo durante una comprobación del presupuesto.",
        "provider_not_configured": "No se configuró el proveedor del modelo.",
        "provider_unavailable_or_invalid_response": "El proveedor no está disponible o devolvió una respuesta inválida.",
        "provider_timeout": "La solicitud al proveedor superó su tiempo de espera.",
        "provider_invalid_structured_output": "El proveedor no devolvió una selección estructurada válida.",
        "provider_usage_missing": "El proveedor no informó el uso de tokens; no se presupone consumo cero.",
        "provider_call_interrupted_usage_unknown": "Una solicitud al proveedor fue interrumpida y su consumo final no se conoce; no se volvió a emitir.",
    }
    errors = "".join(
        f"<li>{escape(error_labels.get(str(error), 'La ejecución se detuvo con una incidencia registrada.'))} <code>{escape(str(error))}</code></li>"
        for error in report.get("errors", [])
    )
    error_block = (
        f"<aside class='alert'><strong>Incidencias de ejecución</strong><ul>{errors}</ul></aside>"
        if errors
        else ""
    )
    decisions = []
    for decision in report.get("decisions", []):
        links = " ".join(
            f"<a href='#{escape(str(evidence_id))}'>{escape(str(evidence_id))}</a>"
            for evidence_id in decision.get("evidence_ids", [])
        )
        arguments = escape(json.dumps(decision.get("args", {}), ensure_ascii=False, sort_keys=True))
        decisions.append(
            f"<li><strong>{escape(str(decision.get('tool', '')))}</strong> · {escape(str(decision.get('summary', '')))}<br><code>{arguments}</code><br>{links}</li>"
        )
    metric_labels = (
        ("analytic_calls", "Llamadas analíticas"),
        ("llm_calls", "Solicitudes al modelo"),
        ("input_tokens", "Tokens de entrada medidos"),
        ("output_tokens", "Tokens de salida medidos"),
        ("latency_seconds", "Tiempo de ejecución registrado · segundos"),
        ("rejected_selections", "Selecciones rechazadas"),
        ("usage_unknown", "Consumo final desconocido"),
    )
    metric_rows = "".join(
        f"<dt>{escape(label)}</dt><dd>{escape(str(metrics.get(key, 'No disponible')))}</dd>"
        for key, label in metric_labels
    )
    trace_section = f"<section><h2>Decisiones registradas</h2><ol>{''.join(decisions) or '<li>La ejecución no requirió una selección de herramienta del modelo.</li>'}</ol><details><summary>Métricas de ejecución</summary><dl>{metric_rows}<dt>Costo monetario</dt><dd>No disponible</dd></dl><pre>{escape(json.dumps(metrics, ensure_ascii=False, indent=2))}</pre></details></section>"
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Metric Detective · Informe</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f5f5f1;color:#172a3b;font:16px/1.6 system-ui,sans-serif}}main{{max-width:1120px;margin:48px auto;padding:0 24px}}header{{padding:32px;background:#142b3d;color:#fff;border-radius:20px;overflow-wrap:anywhere}}h1{{font-size:42px;letter-spacing:-1.5px;margin:8px 0}}h2{{margin-top:0}}section{{background:#fff;padding:28px;margin:20px 0;border-radius:16px;border:1px solid #dfe4e4}}.tag{{font-size:13px;letter-spacing:.08em}}a{{color:#08786b}}li{{margin:12px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f2f5f6;padding:16px;border-radius:8px}}details{{padding:16px 0;border-bottom:1px solid #e0e5e5}}summary{{cursor:pointer;font-weight:600}}.muted{{color:#617487}}.plotly-graph-div{{max-width:100%}}.alert{{padding:16px;background:#fff3e2;color:#67400c;border-radius:10px}}dl{{display:grid;grid-template-columns:minmax(160px,1fr) 1fr;gap:8px 16px}}dd{{margin:0;overflow-wrap:anywhere}}code{{overflow-wrap:anywhere}}</style></head><body><main><header><span class="tag">INVESTIGACIÓN CON EVIDENCIA</span><h1>Metric Detective</h1><p>Estado: {status} · Modo: {mode}</p>{period_labels}<p>Zona horaria: {escape(definition.config.timezone)} · Moneda: {escape(currency)}</p><p>Proveedor: {escape(str(metrics.get("provider", "No disponible")))} · Modelo: {escape(str(metrics.get("model", "No disponible")))}</p><p>Huella: {escape(report["data_fingerprint"])}</p>{error_block}</header><section><h2>Hallazgos respaldados</h2><ul>{claims}</ul></section>{"".join(f"<section>{chart}</section>" for chart in charts)}<section><h2>Hipótesis pendientes</h2><ul>{hypotheses or "<li>No se propone una explicación causal con la información disponible.</li>"}</ul><h2>Datos faltantes</h2><ul>{missing or "<li>Sin limitaciones de completitud detectadas.</li>"}</ul></section>{trace_section}<section><h2>Evidencia reproducible</h2>{details}</section><section><h2>Alcance y limitaciones</h2><ul>{limitations}</ul><p class="muted">Costo del modelo: no disponible. No se publican porcentajes de confianza inventados.</p></section></main></body></html>"""
