"""Spanish investigation workspace. Run with ``streamlit run .../ui.py``."""

from __future__ import annotations

import html
import json
import math
import os
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

load_dotenv(override=False)
API_URL = os.getenv("MD_API_URL", "http://127.0.0.1:18000").rstrip("/")
TEAL = "#5EEAD4"
BLUE = "#93B4FF"
CORAL = "#FF927D"
INK = "#111B2C"
STATUS = {
    "queued": "En espera",
    "running": "Investigando",
    "completed": "Completada",
    "partial": "Parcial",
    "blocked_data_quality": "Bloqueada por calidad de datos",
    "failed": "Fallida",
}
DIMENSION_LABELS = {"device": "Dispositivo", "channel": "Canal", "country": "País"}
SOURCE_LABELS = {
    "delivery": "Modalidad de servicio",
    "acquisition": "Adquisición",
    "region": "País",
}
SEGMENT_LABELS = {
    "mobile": "Móvil",
    "desktop": "Escritorio",
    "tablet": "Tableta",
    "remote": "Remoto",
    "onsite": "Presencial",
    "on_site": "Presencial",
    "organic": "Orgánico",
    "paid": "Publicidad",
    "paid_search": "Búsqueda pagada",
    "email": "Correo electrónico",
    "referral": "Referidos",
    "direct": "Directo",
    "social": "Redes sociales",
    "partner": "Socios",
    "__missing__": "Sin valor",
}
COUNTRY_LABELS = {
    "MX": "México",
    "US": "Estados Unidos",
    "CA": "Canadá",
    "ES": "España",
    "CO": "Colombia",
    "AR": "Argentina",
    "BR": "Brasil",
    "CL": "Chile",
    "PE": "Perú",
    "GB": "Reino Unido",
}
ISSUE_LABELS = {
    "load_completeness_unverified": "La completitud de las cargas no está verificada.",
    "duplicate_order_identifiers": "Hay identificadores de pedido duplicados.",
    "mixed_or_unexpected_currency": "Hay monedas mezcladas o diferentes de la moneda configurada.",
    "duplicate_load_declarations": "Hay declaraciones de carga duplicadas para la misma fecha y fuente.",
    "explicit_incomplete_or_failed_loads": "Se declararon cargas incompletas o fallidas.",
    "zero_reference_revenue_percentage_undefined": "El porcentaje de cambio no se puede calcular: la referencia tiene ingresos de cero.",
    "empty_period_ticket_unobserved_carry_observed_ticket_for_decomposition": "Un periodo sin pedidos no tiene ticket observado; la descomposición asigna la entrada o salida al volumen.",
    "partitions_overlap_across_dimensions_do_not_add_device_channel_country_contributions": "Las particiones se superponen. No sumes las contribuciones de dispositivo, canal y país.",
    "contribution_percent_undefined_when_partition_delta_zero": "El porcentaje de contribución no se define cuando el cambio total es cero.",
}
ERROR_LABELS = {
    "token_budget_exhausted": "Se agotó el presupuesto de tokens. El informe conserva la evidencia obtenida.",
    "analytical_call_limit": "Se alcanzó el límite de análisis; quedan comprobaciones pendientes.",
    "time_budget_exhausted": "Se alcanzó el tiempo permitido; revisa el alcance parcial.",
    "provider_timeout": "El modelo no respondió dentro del tiempo permitido.",
    "provider_unavailable_or_invalid_response": "El modelo no estuvo disponible o devolvió una respuesta incompatible.",
    "provider_call_interrupted_usage_unknown": "La llamada al modelo se interrumpió. Su consumo final es desconocido y no se repitió.",
    "invalid_tool_selection": "El modelo no seleccionó un análisis válido dentro de los intentos permitidos.",
    "required_analysis_missing": "El agente terminó antes de completar los análisis requeridos.",
}
API_ERROR_LABELS = {
    "database_unavailable": "PostgreSQL no está disponible. Revisa el estado del entorno local.",
    "dataset_not_found": "No se encontró el conjunto de datos. Vuelve a seleccionarlo o cargarlo.",
    "invalid_metric_configuration": "La configuración no cumple el contrato de la métrica.",
    "invalid_dataset_contract": "Los CSV no cumplen el contrato. Revisa columnas, tipos y valores obligatorios.",
    "configuration_must_match_validated_dataset": "La definición debe coincidir con la del conjunto validado. Carga los archivos de nuevo para cambiarla.",
    "files_too_large": "Los archivos superan el límite de carga configurado.",
    "request_too_large": "La solicitud supera el límite de tamaño permitido.",
    "report_not_ready": "El informe todavía no está disponible. Espera a que termine el trabajo.",
    "investigation_not_found": "No se encontró esta investigación.",
}
DEFAULT_CONFIG: dict[str, Any] = {
    "name": "Ingresos de comercio electrónico",
    "id_column": "order_id",
    "timestamp_column": "paid_at",
    "status_column": "status",
    "amount_column": "amount",
    "currency_column": "currency",
    "dimensions": {"device": "device", "channel": "channel", "country": "country"},
    "included_statuses": ["paid"],
    "timezone": "America/Mexico_City",
    "currency": "MXN",
    "expected_sources": ["orders"],
    "relevant_change_pct": 1.0,
}


class APIError(RuntimeError):
    """A safe, user-facing service error."""

    def __init__(
        self, message: str, *, code: str = "request_failed", status_code: int | None = None
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def api(method: str, path: str, **kwargs: Any) -> Any:
    """Bounded API requests; raw provider errors or credentials never reach UI."""
    try:
        with httpx.Client(base_url=API_URL, timeout=30) as client:
            response = client.request(method, path, **kwargs)
        if response.is_error:
            try:
                detail = response.json().get("detail", "Solicitud rechazada")
                if not isinstance(detail, str):
                    detail = "Revisa el contrato, los archivos y los periodos seleccionados."
            except (ValueError, AttributeError):
                detail = "El servicio no pudo completar la solicitud."
            message = API_ERROR_LABELS.get(
                detail,
                "El servicio rechazó la solicitud. Revisa los datos y el estado del sistema.",
            )
            raise APIError(message, code=detail, status_code=response.status_code)
        return response.json()
    except httpx.RequestError as exc:
        raise APIError(
            "El servicio de investigaciones no está disponible.", code="connection_unavailable"
        ) from exc


def load_local_replay(path: Path | None = None) -> dict[str, Any]:
    """Read and validate an existing report; never start an investigation."""
    from metric_detective.reporting import validate_report

    if path is None:
        from metric_detective.resources import load_recorded_report

        report = load_recorded_report()
    else:
        report = json.loads(path.read_text(encoding="utf-8"))
    validate_report(report)
    return report


def _go_to_replay() -> None:
    st.session_state["workspace_page"] = "Reproducción grabada"


def unavailable_page(*, job_id: str | None = None) -> None:
    st.markdown('<div class="eyebrow">ENTORNO LOCAL</div>', unsafe_allow_html=True)
    st.subheader("El servicio de investigaciones está detenido")
    st.warning(
        "La interfaz está abierta, pero no puede conectarse con la API. Inicia el entorno para crear trabajos o consultar el historial."
    )
    if job_id:
        st.caption(
            f"Investigación seleccionada: {job_id}. Vuelve a consultarla cuando el servicio esté disponible."
        )
    st.markdown("**Desde una terminal en la carpeta del proyecto:**")
    st.code(
        "uv run metric-detective local start --open\nuv run metric-detective local status",
        language="sh",
    )
    st.caption(
        "El inicio local prepara y abre los servicios. Para Docker: docker compose up --build -d. Consulta el README si es tu primera ejecución."
    )
    left, right = st.columns(2)
    if left.button(
        "Volver a comprobar conexión", width="stretch", key=f"retry_{job_id or 'service'}"
    ):
        st.rerun()
    right.button(
        "Explorar informe sin conexión",
        on_click=_go_to_replay,
        width="stretch",
        key=f"offline_{job_id or 'service'}",
    )
    st.caption(
        "La reproducción usa un informe guardado; no calcula cifras nuevas ni ejecuta un modelo."
    )


def fetch_health() -> dict[str, Any]:
    try:
        return api("GET", "/health/details", timeout=3)
    except APIError as exc:
        if exc.status_code == 404:
            try:
                return api("GET", "/health", timeout=3)
            except APIError:
                pass
        return {"status": "unavailable", "api_available": False}


def service_status(health: dict[str, Any], service: str) -> str:
    item = health.get(service)
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        if item.get("status"):
            return str(item["status"])
        for field in ("available", "reachable", "healthy"):
            if isinstance(item.get(field), bool):
                return "ok" if item[field] else "unavailable"
    return "unknown"


def model_available(health: dict[str, Any]) -> bool | None:
    item = health.get("ollama", {})
    if not isinstance(item, dict):
        return None
    for field in ("model_available", "model_installed"):
        if isinstance(item.get(field), bool):
            return item[field]
    return None


def system_page(health: dict[str, Any]) -> None:
    st.markdown('<div class="eyebrow">ESTADO DEL SISTEMA</div>', unsafe_allow_html=True)
    st.title("Todo listo antes de investigar")
    st.caption(
        "Comprueba la base de datos, el trabajador y el modelo configurado. El análisis fijo no necesita un modelo."
    )
    if health.get("status") == "unavailable":
        unavailable_page()
        return
    columns = st.columns(3)
    for column, service, label in zip(
        columns,
        ["database", "worker", "ollama"],
        ["Base de datos", "Trabajador", "Modelo local"],
        strict=True,
    ):
        state = service_status(health, service)
        okay = state in {"ok", "healthy", "ready", "running", "available"}
        text = (
            "Disponible"
            if okay
            else ("Sin verificar" if state == "unknown" else "Requiere atención")
        )
        column.metric(label, text)
    item = health.get("ollama", {})
    if isinstance(item, dict) and item.get("model"):
        st.caption(f"Modelo configurado: {item['model']}")
    if model_available(health) is False:
        st.warning(
            "El modelo configurado no está instalado o el servidor no está disponible. Puedes usar el punto de referencia o la reproducción grabada."
        )
        if isinstance(item, dict) and item.get("model"):
            st.code(f"ollama pull {item['model']}", language="sh")
    if service_status(health, "worker") in {
        "missing",
        "stopped",
        "unavailable",
        "error",
        "offline",
    }:
        st.warning(
            "No se detectó un trabajador activo. Inicia el entorno local para procesar investigaciones nuevas."
        )
    st.code(
        "uv run metric-detective local status\nuv run metric-detective local start", language="sh"
    )
    if st.button("Actualizar comprobaciones", type="primary"):
        st.rerun()
    with st.expander("Detalles de diagnóstico"):
        st.json(health)


def numeric(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(Decimal(str(value)))
        return result if math.isfinite(result) else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def money(value: Any, currency: str | None = "MXN") -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return "No definido"
    if not number.is_finite():
        return "No definido"
    suffix = f" {currency}" if currency else ""
    return f"{number:,.2f}{suffix}"


def percent(value: Any) -> str:
    number = numeric(value)
    return "No definido" if number is None else f"{number:+.2f}%"


def evidence_for(report: dict[str, Any], tool: str) -> list[dict[str, Any]]:
    return [item for item in report.get("evidence", []) if item.get("tool") == tool]


def chart_style(figure: go.Figure, height: int = 330) -> go.Figure:
    figure.update_layout(
        height=height,
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Inter, ui-sans-serif, system-ui", "color": "#CFD9E8", "size": 12},
        margin={"l": 15, "r": 20, "t": 25, "b": 35},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.05, "x": 0},
        hoverlabel={"bgcolor": INK, "font_size": 13},
    )
    figure.update_xaxes(showgrid=False, zeroline=False)
    figure.update_yaxes(gridcolor="rgba(148,163,184,.12)", zerolinecolor="#64748B")
    return figure


def daily_chart(evidence: dict[str, Any], currency: str) -> go.Figure:
    figure = go.Figure()
    days = evidence.get("values", {}).get("days", [])
    for period, label, color in [
        ("reference", "Referencia", BLUE),
        ("comparison", "Comparación", TEAL),
    ]:
        rows = sorted(
            [day for day in days if day.get("period") == period],
            key=lambda row: row.get("day_index", 0),
        )
        figure.add_trace(
            go.Scatter(
                x=[row.get("day_index", index) + 1 for index, row in enumerate(rows)],
                y=[numeric(row.get("revenue")) for row in rows],
                name=label,
                mode="lines+markers",
                line={"color": color, "width": 3},
                marker={"size": 7},
                customdata=[[row.get("date"), row.get("orders")] for row in rows],
                hovertemplate=(
                    f"%{{customdata[0]}}<br>%{{y:,.2f}} {currency}"
                    "<br>%{customdata[1]} pedidos<extra>%{fullData.name}</extra>"
                ),
            )
        )
    figure.update_xaxes(
        title="Día local de la semana",
        tickmode="array",
        tickvals=list(range(1, 8)),
        ticktext=["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"],
    )
    figure.update_yaxes(title=f"Ingresos · {currency}")
    return chart_style(figure)


def dimension_label(dimension: str, config: dict[str, Any] | None = None) -> str:
    source = (config or {}).get("dimensions", {}).get(dimension)
    return SOURCE_LABELS.get(
        source, DIMENSION_LABELS.get(dimension, dimension.replace("_", " ").capitalize())
    )


def segment_value(value: Any, dimension: str | None = None) -> str:
    text = str(value)
    if dimension == "country":
        return COUNTRY_LABELS.get(text, text)
    return SEGMENT_LABELS.get(text, text)


def segment_label(
    segment: Any, dimension: str | None = None, config: dict[str, Any] | None = None
) -> str:
    if isinstance(segment, dict):
        return " · ".join(
            f"{dimension_label(key, config)}: {segment_value(value, key)}"
            for key, value in segment.items()
        )
    return segment_value(segment, dimension)


def segment_chart(
    evidence: dict[str, Any], currency: str, config: dict[str, Any] | None = None
) -> go.Figure:
    rows = evidence.get("values", {}).get("segments", [])
    rows = sorted(rows, key=lambda row: numeric(row.get("delta_revenue")) or 0)
    changes = [numeric(row.get("delta_revenue")) or 0 for row in rows]
    figure = go.Figure(
        go.Bar(
            x=changes,
            y=[
                segment_label(
                    row.get("segment", ""), evidence.get("values", {}).get("dimension"), config
                )
                for row in rows
            ],
            orientation="h",
            marker_color=[CORAL if value < 0 else TEAL for value in changes],
            text=[money(row.get("delta_revenue"), currency) for row in rows],
            textposition="auto",
            hovertemplate=f"%{{y}}<br>%{{x:+,.2f}} {currency}<extra></extra>",
        )
    )
    figure.update_xaxes(title=f"Contribución al cambio · {currency}")
    figure.update_yaxes(autorange="reversed")
    return chart_style(figure, height=max(280, min(700, len(rows) * 42 + 80)))


def decomposition_chart(
    evidence: dict[str, Any], comparison: dict[str, Any], currency: str
) -> go.Figure:
    values = evidence.get("values", {})
    summary = comparison.get("values", {})
    figure = go.Figure(
        go.Waterfall(
            x=["Referencia", "Volumen", "Ticket promedio", "Comparación"],
            measure=["absolute", "relative", "relative", "total"],
            y=[
                numeric(summary.get("reference", {}).get("revenue")) or 0,
                numeric(values.get("volume_effect")) or 0,
                numeric(values.get("ticket_effect")) or 0,
                0,
            ],
            text=[
                money(summary.get("reference", {}).get("revenue"), currency),
                money(values.get("volume_effect"), currency),
                money(values.get("ticket_effect"), currency),
                money(summary.get("comparison", {}).get("revenue"), currency),
            ],
            textposition="outside",
            increasing={"marker": {"color": TEAL}},
            decreasing={"marker": {"color": CORAL}},
            totals={"marker": {"color": BLUE}},
            connector={"line": {"color": "#53647E"}},
        )
    )
    figure.update_yaxes(title=f"Ingresos · {currency}")
    return chart_style(figure, 370)


def claim_text(claim: Any) -> str:
    if isinstance(claim, str):
        return claim
    if isinstance(claim, dict):
        return str(claim.get("text") or claim.get("statement") or claim.get("description") or "")
    return str(claim)


def references(claim: Any) -> str:
    if not isinstance(claim, dict):
        return ""
    refs = claim.get("evidence_ids") or claim.get("evidence_id") or []
    if isinstance(refs, str):
        refs = [refs]
    return " · ".join(str(ref) for ref in refs)


def concise_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Select existing checked claims without adding a numerical conclusion."""
    selected: list[dict[str, Any]] = []
    partitions: set[tuple[str, ...]] = set()
    by_id = {item["id"]: item for item in report.get("evidence", [])}
    for finding in report.get("findings", []):
        kind = finding.get("kind")
        if kind in {
            "data_quality",
            "observed_orders",
            "no_relevant_change",
            "arithmetic_contribution",
        }:
            selected.append(finding)
        elif kind == "segment_change" and len(partitions) < 2:
            evidence = by_id.get(finding.get("evidence_id"), {})
            values = evidence.get("values", {})
            dimensions = tuple(values.get("dimensions", [values.get("dimension", "")]))
            if dimensions not in partitions:
                selected.append(finding)
                partitions.add(dimensions)
    return selected


def finding_display(finding: dict[str, Any], report: dict[str, Any]) -> str:
    if finding.get("kind") != "segment_change":
        return claim_text(finding)
    by_id = {item["id"]: item for item in report.get("evidence", [])}
    item = by_id.get(finding.get("evidence_id"), {})
    values = item.get("values", {})
    try:
        index = int(finding["path"].split(".")[1])
        row = values["segments"][index]
    except (ValueError, KeyError, IndexError):
        return claim_text(finding)
    config = report.get("request", {}).get("config", {})
    dimension = values.get("dimension")
    title = dimension_label(dimension, config) if dimension else "Cruce de segmentos"
    label = segment_label(row["segment"], dimension, config)
    return f"{title}: {label} · cambio de {money(finding['value'], config.get('currency', 'MXN'))}."


def show_quality(report: dict[str, Any]) -> None:
    quality = next(iter(evidence_for(report, "check_data_quality")), None)
    if quality is None:
        st.warning("Esta ejecución no completó la comprobación de calidad de datos.")
        return
    status = quality.get("values", {}).get("status")
    if status == "blocked":
        st.error(
            "Diagnóstico de negocio bloqueado. Corrige la calidad de los datos antes de interpretar ingresos."
        )
        for issue in quality.get("values", {}).get("issues", []):
            st.markdown(
                f"- {ISSUE_LABELS.get(issue, 'Consulta el detalle de calidad en la evidencia.')}"
            )
    elif status == "unverified":
        st.warning(
            "Completitud no verificada. Las cifras describen los archivos recibidos; falta confirmar las cargas de todas las fechas y fuentes."
        )
    elif status == "ok":
        st.caption(
            "✓ Sin bloqueos de calidad detectados · Cargas declaradas completas para los periodos seleccionados."
        )
    st.caption(f"Comprobación de calidad: {quality['id']}")


def render_report(report: dict[str, Any], *, replay: bool = False, offline: bool = False) -> None:
    from metric_detective.reporting import validate_report

    try:
        validate_report(report)
    except (ValueError, KeyError, TypeError):
        st.error("El informe no superó la validación de evidencia. No se muestran sus cifras.")
        return
    if replay:
        st.info(
            "REPRODUCCIÓN GRABADA · Este informe se calculó previamente con datos sintéticos. "
            "Explorar esta vista no ejecuta un modelo ni inicia una investigación nueva."
        )
        if offline:
            st.caption(
                "SIN CONEXIÓN CON LA API · Archivo incluido con la aplicación. No se consulta el estado actual del servidor."
            )
    config = report.get("request", {}).get("config", {})
    currency = config.get("currency", "MXN")
    status = report.get("status", "partial")
    label = STATUS.get(status, status)
    status_color = CORAL if status in {"failed", "blocked_data_quality"} else TEAL
    st.markdown(
        f'<div class="eyebrow">INFORME DE INVESTIGACIÓN</div>'
        f'<div class="report-heading">La evidencia, antes de la explicación '
        f'<span class="status-pill" style="color:{status_color}">{html.escape(label)}</span></div>',
        unsafe_allow_html=True,
    )
    period_request = report.get("request", {})
    st.caption(
        f"Referencia: {period_request.get('reference_start', '—')} · "
        f"Comparación: {period_request.get('comparison_start', '—')} · "
        f"Semanas de 7 días · {config.get('timezone', 'Zona horaria en la evidencia')}"
    )
    mode = report.get("mode")
    provider = report.get("metrics", {}).get("provider")
    if mode == "baseline":
        st.caption("Método: punto de referencia · secuencia fija de análisis, sin modelo.")
    elif provider == "scripted_test_double":
        st.caption(
            "Método: doble determinista de pruebas · no corresponde a una ejecución real de un modelo."
        )
    else:
        st.caption(
            f"Método: agente · {report.get('metrics', {}).get('model', 'modelo registrado')}"
        )
    show_quality(report)

    summaries = evidence_for(report, "compare_periods")
    summary = summaries[0] if summaries else None
    if summary:
        values = summary.get("values", {})
        reference = values.get("reference", {})
        comparison = values.get("comparison", {})
        columns = st.columns(3)
        columns[0].metric(
            f"Cambio en ingresos · {currency}",
            money(values.get("delta_revenue"), None),
            percent(values.get("delta_pct")),
        )
        columns[1].metric(
            f"Ingresos · comparación · {currency}", money(comparison.get("revenue"), None)
        )
        columns[2].metric(
            f"Ingresos · referencia · {currency}", money(reference.get("revenue"), None)
        )
        st.caption(
            f"Fuente: {summary.get('id', '—')} · Ingresos = importe de pedidos en estados incluidos."
        )
        with st.expander("Pedidos y ticket promedio"):
            st.dataframe(
                [
                    {
                        "Periodo": "Referencia",
                        "Pedidos": reference.get("orders"),
                        "Ticket promedio": money(reference.get("aov"), currency),
                    },
                    {
                        "Periodo": "Comparación",
                        "Pedidos": comparison.get("orders"),
                        "Ticket promedio": money(comparison.get("aov"), currency),
                    },
                ],
                hide_index=True,
                width="stretch",
            )
    elif status == "blocked_data_quality":
        st.warning(
            "El diagnóstico de negocio se detuvo: primero hay que resolver los problemas de datos."
        )

    overview, charts, evidence_tab = st.tabs(
        ["Hallazgos y límites", "Gráficos", "Evidencia y decisiones"]
    )
    with overview:
        findings = concise_findings(report)
        st.markdown("#### Qué muestran los datos")
        if not findings:
            st.caption("No hay hallazgos de negocio respaldados para esta ejecución.")
        for finding in findings:
            text = finding_display(finding, report)
            if text:
                st.markdown(f"- {text}")
            if refs := references(finding):
                st.caption(f"Evidencia: {refs}")
        with st.expander(f"Ver los {len(report.get('findings', []))} hallazgos verificados"):
            for finding in report.get("findings", []):
                st.markdown(f"- {finding_display(finding, report)}")
                st.caption(f"Evidencia: {references(finding)}")
        left, right = st.columns(2, gap="large")
        with left:
            st.markdown("#### Hipótesis pendientes")
            hypotheses = report.get("hypotheses", [])
            if not hypotheses:
                st.caption("No se han propuesto mecanismos causales.")
            for hypothesis in hypotheses:
                st.markdown(f"- {claim_text(hypothesis)}")
        with right:
            st.markdown("#### Datos faltantes y siguientes comprobaciones")
            missing = report.get("missing_data", [])
            checks = [
                *report.get("next_checks", []),
                *[
                    hypothesis["next_check"]
                    for hypothesis in hypotheses
                    if isinstance(hypothesis, dict) and hypothesis.get("next_check")
                ],
            ]
            if not missing and not checks:
                st.caption(
                    "Consulta las advertencias de cada evidencia y las limitaciones del alcance."
                )
            for item in [*missing, *checks]:
                st.markdown(f"- {claim_text(item)}")
        st.caption(
            "Las contribuciones describen dónde cambió el ingreso; no prueban una causa. Devoluciones fuera de alcance."
        )
        for error in report.get("errors", []):
            code = str(error)
            st.warning(
                ERROR_LABELS.get(
                    code,
                    "La ejecución tiene una limitación adicional. Consulta el registro y su código en evidencia.",
                )
            )
    with charts:
        daily = evidence_for(report, "inspect_daily_pattern")
        decompositions = evidence_for(report, "decompose_revenue_change")
        partitions = [
            *evidence_for(report, "rank_segment_changes"),
            *evidence_for(report, "drill_down"),
        ]
        if not daily and not decompositions and not partitions:
            st.caption(
                "Esta ejecución no produjo análisis adicionales. No se generan gráficos sin evidencia."
            )
        if daily:
            st.markdown("#### Evolución diaria")
            for index, item in enumerate(daily):
                if item.get("filters"):
                    st.caption(f"Filtros: {segment_label(item['filters'], config=config)}")
                else:
                    st.caption("Todos los pedidos · sin filtros de segmento")
                st.plotly_chart(daily_chart(item, currency), width="stretch", key=f"daily_{index}")
                st.caption(f"Fuente: {item['id']} · Los días se alinean dentro de cada semana.")
        if decompositions and summary:
            st.markdown("#### ¿Volumen o ticket promedio?")
            item = decompositions[0]
            st.plotly_chart(
                decomposition_chart(item, summary, currency), width="stretch", key="decomposition"
            )
            st.caption(
                f"Fuentes: {item['id']} · {summary['id']}. "
                "La interacción se reparte por mitades entre volumen y ticket; no se suma otra vez."
            )
        if partitions:
            st.markdown("#### Dónde se concentra el cambio")
            st.caption(
                "Cada partición explica su propio total. No se suman contribuciones entre dispositivo, canal y país."
            )
            for index, item in enumerate(partitions):
                values = item.get("values", {})
                dimension = (
                    dimension_label(values["dimension"], config)
                    if values.get("dimension")
                    else " × ".join(
                        dimension_label(name, config) for name in values.get("dimensions", [])
                    )
                )
                st.markdown(f"**{dimension or 'Segmentos'}**")
                st.plotly_chart(
                    segment_chart(item, currency, config), width="stretch", key=f"segments_{index}"
                )
                st.caption(f"Fuente: {item['id']}")
                with st.expander(f"Ver cifras de la partición {index + 1}"):
                    st.dataframe(values.get("segments", []), hide_index=True, width="stretch")
    with evidence_tab:
        st.caption(f"Huella de los datos: {report.get('data_fingerprint', '—')}")
        st.markdown("#### Decisiones registradas")
        for index, decision in enumerate(report.get("decisions", []), start=1):
            with st.expander(
                f"Decisión {index}: {decision.get('tool', decision.get('action', 'siguiente paso'))}"
                if isinstance(decision, dict)
                else f"Decisión {index}"
            ):
                st.json(decision)
        st.markdown("#### Resultados reproducibles")
        for item in report.get("evidence", []):
            with st.expander(f"{item.get('tool', 'Análisis')} · {item.get('id', '—')}"):
                for warning in item.get("warnings", []):
                    st.warning(
                        ISSUE_LABELS.get(
                            warning,
                            "Consulta el código de esta advertencia en el resultado original.",
                        )
                    )
                st.json(item)
        with st.expander("Registro de ejecución"):
            st.json({"metrics": report.get("metrics", {}), "error_codes": report.get("errors", [])})
            st.caption(
                "Los costos no disponibles no equivalen a costo cero. Los tokens pertenecen al proveedor registrado."
            )
    st.divider()
    download_left, download_right = st.columns(2)
    report_json = json.dumps(report, ensure_ascii=False, indent=2)
    download_left.download_button(
        "Descargar informe JSON",
        report_json,
        "metric-detective-report.json",
        "application/json",
        width="stretch",
    )
    try:
        from metric_detective.reporting import render_html

        report_html = render_html(report)
        download_right.download_button(
            "Descargar informe HTML",
            report_html,
            "metric-detective-report.html",
            "text/html",
            width="stretch",
        )
    except (ValueError, KeyError, TypeError):
        download_right.caption("La exportación HTML requiere un informe validado.")


PHASE_LABELS = {
    "validate": "Validando la solicitud y la definición de la métrica",
    "quality": "Comprobando pedidos y completitud de las cargas",
    "compare": "Calculando el cambio entre las dos semanas",
    "select": "Seleccionando la siguiente comprobación",
    "execute": "Ejecutando un análisis y registrando evidencia",
    "review": "Revisando la evidencia y los límites de la investigación",
    "finalize": "Validando el informe y sus referencias",
    "done": "Informe preparado",
    "finished": "Informe preparado",
}


def job_progress(job: dict[str, Any]) -> tuple[str, int | None]:
    progress = job.get("progress") or {}
    phase = (
        progress.get("phase", progress.get("stage", ""))
        if isinstance(progress, dict)
        else str(progress)
    )
    label = PHASE_LABELS.get(phase, "Procesando la investigación")
    if job.get("status") == "queued":
        label = "Esperando a que el trabajador recoja el trabajo"
    count = progress.get("evidence_count") if isinstance(progress, dict) else None
    return label, count if isinstance(count, int) and count >= 0 else None


def poll_job(job_id: str) -> None:
    @st.fragment(run_every="2s")
    def progress_fragment() -> None:
        try:
            job = api("GET", f"/investigations/{job_id}")
        except APIError:
            unavailable_page(job_id=job_id)
            return
        status = job.get("status", "queued")
        stage, count = job_progress(job)
        if status in {"queued", "running"}:
            # A status context manager marks itself complete on exit. The
            # database job, rather than this render, determines completion.
            progress_box = st.status(STATUS.get(status, status), state="running", expanded=True)
            progress_box.write(stage)
            if count is not None:
                progress_box.caption(f"{count} resultados de evidencia registrados.")
            progress_box.caption(
                "El trabajo se guarda en el servidor. Puedes consultar su avance desde Historial."
            )
            st.caption(f"ID de investigación: {job_id}")
            return
        st.session_state["active_job"] = None
        st.session_state["last_job"] = job
        st.rerun()

    progress_fragment()


def new_investigation(health: dict[str, Any] | None = None) -> None:
    st.markdown('<div class="eyebrow">NUEVA INVESTIGACIÓN</div>', unsafe_allow_html=True)
    st.title("¿Dónde cambió el ingreso?")
    st.markdown(
        "Compara dos semanas completas. Obtén cifras reproducibles, segmentos relevantes y límites claros."
    )
    health = health or {}
    if health.get("status") == "unavailable":
        unavailable_page()
        return
    try:
        response = api("GET", "/scenarios")
    except APIError as exc:
        if exc.code in {"connection_unavailable", "database_unavailable"}:
            unavailable_page()
        else:
            st.error(str(exc))
        return
    scenarios = response.get("scenarios", []) if isinstance(response, dict) else response
    if not scenarios:
        st.error(
            "No hay escenarios disponibles. Genera los datos sintéticos antes de iniciar la API."
        )
        return
    source = st.radio(
        "Origen de los datos", ["Escenario de demostración", "Cargar archivos"], horizontal=True
    )
    config = DEFAULT_CONFIG.copy()
    dataset_id = ""
    reference_default = date(2026, 1, 5)
    comparison_default = date(2026, 1, 12)
    uploaded_orders = uploaded_loads = None
    if source == "Escenario de demostración":
        scenario = st.selectbox(
            "Conjunto de datos", scenarios, format_func=lambda item: item.get("name", item["id"])
        )
        st.caption(
            scenario.get("description", "Datos sintéticos; no corresponden a clientes reales.")
        )
        config = scenario.get("config", DEFAULT_CONFIG)
        dataset_id = scenario.get("dataset_id", scenario["id"])
        reference_default = date.fromisoformat(scenario.get("reference_start", "2026-01-05"))
        comparison_default = date.fromisoformat(scenario.get("comparison_start", "2026-01-12"))
    else:
        st.caption(
            "CSV UTF-8, una fila por pedido. Límite de API predeterminado: 10 MiB entre ambos archivos; 100 000 filas por archivo. No incluyas datos personales."
        )
        upload_left, upload_right = st.columns(2)
        uploaded_orders = upload_left.file_uploader(
            "orders.csv · pedidos", type=["csv"], max_upload_size=10
        )
        uploaded_loads = upload_right.file_uploader(
            "loads.csv · completitud (opcional)", type=["csv"], max_upload_size=10
        )
        if uploaded_loads is None:
            st.caption(
                "Sin loads.csv se registrará completitud no verificada; la existencia de pedidos no la demuestra."
            )
    with st.expander("Definición de la métrica y contrato", expanded=source == "Cargar archivos"):
        config_text = st.text_area(
            "Configuración JSON",
            value=json.dumps(config, ensure_ascii=False, indent=2),
            height=270,
            key=f"config_{source}_{dataset_id}",
            disabled=source == "Escenario de demostración",
            help="Moneda, zona horaria, estados incluidos y correspondencia de columnas. Las dimensiones permitidas delimitan la investigación.",
        )
        st.caption(
            "Ingresos = suma del importe de pedidos en los estados incluidos. Devoluciones fuera del alcance inicial."
        )
        if source == "Escenario de demostración":
            st.caption(
                "La definición de esta demo está fijada. Para cambiarla, carga los archivos con una configuración nueva."
            )
    dates_left, dates_right = st.columns(2)
    reference = dates_left.date_input("Semana de referencia · lunes", reference_default)
    comparison = dates_right.date_input("Semana de comparación · lunes", comparison_default)
    st.caption(
        f"Referencia: {reference} → {reference + timedelta(days=6)} · "
        f"Comparación: {comparison} → {comparison + timedelta(days=6)}"
    )
    live_ready = model_available(health)
    method = st.radio(
        "Método de investigación",
        ["Agente · modelo local", "Punto de referencia · secuencia fija"],
        horizontal=True,
        index=1 if live_ready is False else 0,
    )
    st.caption(
        "El agente selecciona análisis a partir de los resultados. El punto de referencia ejecuta una secuencia fija sin LLM."
    )
    if live_ready is False:
        st.info(
            "El modelo no está disponible. El punto de referencia permite investigar ahora; revisa Estado del sistema para activar el agente."
        )
    worker_missing = service_status(health, "worker") in {
        "missing",
        "stopped",
        "unavailable",
        "error",
        "offline",
    }
    if worker_missing:
        st.warning(
            "No se detectó un trabajador activo. Inicia el entorno local antes de crear una investigación."
        )
    with st.expander("Límites de la investigación"):
        limit_left, limit_right = st.columns(2)
        max_calls = limit_left.number_input(
            "Máximo de llamadas analíticas del ciclo", min_value=1, max_value=8, value=8
        )
        max_seconds = limit_right.number_input(
            "Tiempo máximo · segundos", min_value=10, max_value=900, value=180, step=10
        )
        max_tokens = limit_left.number_input(
            "Presupuesto de tokens", min_value=256, max_value=100000, value=20000, step=256
        )
        max_depth = limit_right.number_input(
            "Niveles de segmentación", min_value=1, max_value=2, value=2
        )
    if st.button(
        "Investigar caída",
        type="primary",
        width="stretch",
        disabled=bool(st.session_state.get("active_job"))
        or worker_missing
        or (method.startswith("Agente") and live_ready is False),
    ):
        try:
            from metric_detective.contracts import InvestigationRequest, MetricConfig

            definition = MetricConfig.model_validate_json(config_text)
            if source == "Cargar archivos":
                if uploaded_orders is None:
                    st.warning("Selecciona el archivo de pedidos para iniciar.")
                    return
                files: dict[str, Any] = {
                    "orders": ("orders.csv", uploaded_orders.getvalue(), "text/csv")
                }
                if uploaded_loads is not None:
                    files["loads"] = ("loads.csv", uploaded_loads.getvalue(), "text/csv")
                dataset = api(
                    "POST", "/datasets", data={"config": definition.model_dump_json()}, files=files
                )
                dataset_id = dataset.get("id", dataset.get("dataset_id"))
            request = InvestigationRequest.model_validate(
                {
                    "dataset_id": dataset_id,
                    "reference_start": reference.isoformat(),
                    "comparison_start": comparison.isoformat(),
                    "config": definition.model_dump(mode="json"),
                    "mode": "agent" if method.startswith("Agente") else "baseline",
                    "limits": {
                        "max_calls": max_calls,
                        "max_depth": max_depth,
                        "max_seconds": max_seconds,
                        "max_tokens": max_tokens,
                        "max_retries": 1,
                    },
                }
            )
            job = api("POST", "/investigations", json=request.model_dump(mode="json"))
            st.session_state["active_job"] = job["id"]
            st.session_state["last_job"] = None
            st.rerun()
        except APIError as exc:
            st.error(str(exc))
        except (ValueError, TypeError):
            st.error(
                "Revisa la configuración de la métrica y los periodos: deben empezar en lunes, durar siete días y no superponerse."
            )
    if active := st.session_state.get("active_job"):
        st.divider()
        poll_job(active)
    if last := st.session_state.get("last_job"):
        st.divider()
        if last.get("report"):
            render_report(last["report"])
        else:
            st.error(
                last.get("error")
                or "La investigación terminó sin informe. Consulta los registros del servicio."
            )


def history(health: dict[str, Any] | None = None) -> None:
    st.markdown('<div class="eyebrow">HISTORIAL PERSISTENTE</div>', unsafe_allow_html=True)
    st.title("Cada investigación deja rastro")
    st.caption("Abre trabajos anteriores y consulta su evidencia o su estado actual.")
    if (health or {}).get("status") == "unavailable":
        unavailable_page()
        return
    try:
        response = api("GET", "/investigations")
        jobs = (
            response.get("jobs", response.get("investigations", []))
            if isinstance(response, dict)
            else response
        )
        if not jobs:
            st.info("Aún no hay investigaciones. Empieza con un escenario sintético.")
            return
        job = st.selectbox(
            "Selecciona una investigación",
            jobs,
            format_func=lambda item: (
                f"{STATUS.get(item.get('status'), item.get('status', '—'))} · {item.get('created_at', '')} · {item['id']}"
            ),
        )
        if st.button("Actualizar estado"):
            st.rerun()
        detail = api("GET", f"/investigations/{job['id']}")
        if detail.get("status") in {"queued", "running"}:
            poll_job(job["id"])
        elif detail.get("report"):
            render_report(detail["report"])
        else:
            st.error(detail.get("error") or "La investigación no tiene un informe disponible.")
    except APIError as exc:
        if exc.code in {"connection_unavailable", "database_unavailable"}:
            unavailable_page()
        else:
            st.error(str(exc))


def replay_page() -> None:
    st.markdown('<div class="eyebrow">EXPLORAR SIN CLAVE</div>', unsafe_allow_html=True)
    st.title("Una investigación, paso a paso")
    st.markdown(
        "Explora un informe guardado, sus cifras y las decisiones que condujeron al resultado."
    )
    try:
        response = api("GET", "/replay")
        report = response.get("report", response)
        render_report(report, replay=True)
    except APIError:
        try:
            report = load_local_replay()
            render_report(report, replay=True, offline=True)
        except (OSError, ValueError, KeyError, TypeError):
            st.error(
                "No hay una reproducción local válida disponible. Inicia el entorno o reinstala los archivos del proyecto."
            )


def main() -> None:
    st.set_page_config(
        page_title="Metric Detective · Investiga el ingreso",
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="auto",
    )
    st.markdown(
        """<style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@500;600;700;800&display=swap');
        html,body,[class*="css"],.stApp {font-family:'DM Sans',system-ui,sans-serif;}
        .stApp {background:#0B1322;color:#E9EEF6;}
        [data-testid="stSidebar"] {background:#111C30;border-right:1px solid #22324B;}
        .block-container {max-width:1250px;padding-top:2.7rem;padding-bottom:4rem;}
        h1,h2,h3,.report-heading {font-family:'Manrope',system-ui,sans-serif;letter-spacing:-.035em;}
        h1 {font-size:2.6rem!important;line-height:1.18!important;margin-bottom:.3rem;}
        .eyebrow {font-size:.72rem;font-weight:700;letter-spacing:.18em;color:#7CDACB;margin-bottom:.6rem;}
        .brand {font-family:'Manrope',system-ui,sans-serif;font-size:1.5rem;font-weight:800;letter-spacing:-.05em;line-height:1.13;}
        .brand-mark {color:#5EEAD4;font-size:2.4rem;margin-bottom:.6rem;}
        .sidebar-note {font-size:.78rem;color:#A8B8CE;line-height:1.6;}
        [data-testid="stMetric"] {background:#132035;border:1px solid #293A53;border-radius:15px;padding:1.1rem 1.2rem;}
        [data-testid="stMetricLabel"] {color:#BCCAE0;}
        [data-testid="stMetricValue"] {font-family:'Manrope',system-ui,sans-serif;font-size:clamp(1.05rem,2.1vw,1.65rem)!important;}
        [data-testid="stMetricValue"]>div {white-space:normal;overflow-wrap:anywhere;}
        [data-testid="stVerticalBlockBorderWrapper"] {border-color:#293A53!important;}
        .report-heading {font-size:1.65rem;font-weight:700;margin-top:.3rem;margin-bottom:.8rem;}
        .status-pill {font-family:'DM Sans',system-ui,sans-serif;font-size:.72rem;border:1px solid #38516C;border-radius:30px;padding:.35rem .7rem;white-space:nowrap;vertical-align:middle;letter-spacing:0;}
        .stButton>button[kind="primary"] {background:#5EEAD4;color:#092325;font-weight:700;border:0;border-radius:10px;padding:.6rem 1.2rem;}
        .stButton>button[kind="primary"]:hover {background:#90F2E2;color:#092325;}
        button[data-baseweb="tab"] {padding:.65rem .4rem;margin-right:1.2rem;}
        [data-testid="stExpander"] {border-color:#293A53;border-radius:10px;}
        a {color:#7FE8D6;}
        </style>""",
        unsafe_allow_html=True,
    )
    health = fetch_health()
    with st.sidebar:
        st.markdown(
            '<div class="brand-mark">◈</div><div class="brand">Metric<br>Detective</div>',
            unsafe_allow_html=True,
        )
        st.caption("DEL CAMBIO A LA EVIDENCIA")
        st.divider()
        page = st.radio(
            "Espacio de trabajo",
            ["Nueva investigación", "Reproducción grabada", "Historial", "Estado del sistema"],
            label_visibility="collapsed",
            key="workspace_page",
        )
        st.divider()
        if health.get("status") == "unavailable":
            st.caption("○ API sin conexión · Reproducción local disponible")
        else:
            st.caption("● API conectada")
            worker = service_status(health, "worker")
            if worker in {"ok", "running", "ready", "healthy", "available"}:
                st.caption("● Trabajador disponible")
            elif worker == "unknown":
                st.caption("○ Trabajador sin verificar")
            else:
                st.caption("○ Trabajador requiere atención")
            available = model_available(health)
            st.caption(
                "● Modelo disponible"
                if available is True
                else "○ Modelo no disponible"
                if available is False
                else "○ Modelo sin verificar"
            )
        st.divider()
        st.markdown(
            '<div class="sidebar-note">Una métrica definida.<br>Dos semanas comparables.<br>Cifras que puedes comprobar.</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="sidebar-note" style="margin-top:1.3rem">Datos sintéticos en las demostraciones.<br>Las asociaciones no prueban causas.</div>',
            unsafe_allow_html=True,
        )
    if page == "Historial":
        history(health)
    elif page == "Reproducción grabada":
        replay_page()
    elif page == "Estado del sistema":
        system_page(health)
    else:
        new_investigation(health)


if __name__ == "__main__":
    main()
