"""Spanish investigation workspace. Run with ``streamlit run .../ui.py``."""

from __future__ import annotations

import html
import json
import math
import os
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import plotly.graph_objects as go
import streamlit as st

API_URL = os.getenv("MD_API_URL", "http://127.0.0.1:8000").rstrip("/")
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
            raise APIError(f"{response.status_code}: {detail}")
        return response.json()
    except httpx.RequestError as exc:
        raise APIError("No se pudo conectar con la API. Comprueba que esté iniciada.") from exc


def numeric(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(Decimal(str(value)))
        return result if math.isfinite(result) else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def money(value: Any, currency: str | None = "MXN") -> str:
    number = numeric(value)
    suffix = f" {currency}" if currency else ""
    return "No definido" if number is None else f"{number:,.2f}{suffix}"


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
        title="Día de la semana comparada", tickmode="array", tickvals=list(range(1, 8))
    )
    figure.update_yaxes(title=f"Ingresos · {currency}")
    return chart_style(figure)


def segment_label(segment: Any) -> str:
    if isinstance(segment, dict):
        return " · ".join(
            f"{key}: {'Sin valor' if value == '__missing__' else value}"
            for key, value in segment.items()
        )
    return str(segment).replace("__missing__", "Sin valor")


def segment_chart(evidence: dict[str, Any], currency: str) -> go.Figure:
    rows = evidence.get("values", {}).get("segments", [])
    rows = sorted(rows, key=lambda row: numeric(row.get("delta_revenue")) or 0)
    changes = [numeric(row.get("delta_revenue")) or 0 for row in rows]
    figure = go.Figure(
        go.Bar(
            x=changes,
            y=[segment_label(row.get("segment", "")) for row in rows],
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


def render_report(report: dict[str, Any], *, replay: bool = False) -> None:
    if replay:
        st.info(
            "REPRODUCCIÓN GRABADA · Este informe se calculó previamente con datos sintéticos. "
            "Explorar esta vista no ejecuta un modelo ni inicia una investigación nueva."
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
        findings = report.get("findings", [])
        st.markdown("#### Hallazgos respaldados")
        if not findings:
            st.caption("No hay hallazgos de negocio respaldados para esta ejecución.")
        for finding in findings:
            text = claim_text(finding)
            if text:
                st.markdown(f"- {text}")
            if refs := references(finding):
                st.caption(f"Evidencia: {refs}")
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
            st.warning(claim_text(error) or str(error))
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
                    st.caption(f"Filtros: {item['filters']}")
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
                dimension = values.get("dimension") or " × ".join(values.get("dimensions", []))
                st.markdown(f"**{dimension or 'Segmentos'}**")
                st.plotly_chart(
                    segment_chart(item, currency), width="stretch", key=f"segments_{index}"
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
                    st.warning(warning)
                st.json(item)
        with st.expander("Registro de ejecución"):
            st.json(report.get("metrics", {}))
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


def poll_job(job_id: str) -> None:
    @st.fragment(run_every="2s")
    def progress_fragment() -> None:
        try:
            job = api("GET", f"/investigations/{job_id}")
        except APIError as exc:
            st.error(str(exc))
            return
        status = job.get("status", "queued")
        st.markdown(f"**{STATUS.get(status, status)}** · `{job_id}`")
        progress = job.get("progress")
        if progress:
            if isinstance(progress, dict):
                st.caption(
                    str(
                        progress.get("stage")
                        or progress.get("phase")
                        or progress.get("message")
                        or "Procesando evidencia"
                    )
                )
            else:
                st.caption(str(progress))
        if status in {"queued", "running"}:
            st.progress(
                0.12 if status == "queued" else 0.55,
                text="El trabajo continúa aunque cierres esta vista.",
            )
            return
        st.session_state["active_job"] = None
        st.session_state["last_job"] = job
        st.rerun()

    progress_fragment()


def new_investigation() -> None:
    st.markdown('<div class="eyebrow">NUEVA INVESTIGACIÓN</div>', unsafe_allow_html=True)
    st.title("¿Dónde cambió el ingreso?")
    st.markdown(
        "Compara dos semanas completas. Obtén cifras reproducibles, segmentos relevantes y límites claros."
    )
    try:
        response = api("GET", "/scenarios")
    except APIError as exc:
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
        uploaded_orders = upload_left.file_uploader("orders.csv · pedidos", type=["csv"])
        uploaded_loads = upload_right.file_uploader(
            "loads.csv · completitud (opcional)", type=["csv"]
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
    method = st.radio(
        "Método de investigación",
        ["Agente · modelo local", "Punto de referencia · secuencia fija"],
        horizontal=True,
    )
    st.caption(
        "El agente selecciona análisis a partir de los resultados. El punto de referencia ejecuta una secuencia fija sin LLM."
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
        disabled=bool(st.session_state.get("active_job")),
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
        except (APIError, ValueError, TypeError) as exc:
            st.error(str(exc))
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


def history() -> None:
    st.markdown('<div class="eyebrow">HISTORIAL PERSISTENTE</div>', unsafe_allow_html=True)
    st.title("Cada investigación deja rastro")
    st.caption("Abre trabajos anteriores y consulta su evidencia o su estado actual.")
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
    except APIError as exc:
        st.error(str(exc))


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
    with st.sidebar:
        st.markdown(
            '<div class="brand-mark">◈</div><div class="brand">Metric<br>Detective</div>',
            unsafe_allow_html=True,
        )
        st.caption("DEL CAMBIO A LA EVIDENCIA")
        st.divider()
        page = st.radio(
            "Espacio de trabajo",
            ["Nueva investigación", "Reproducción grabada", "Historial"],
            label_visibility="collapsed",
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
        history()
    elif page == "Reproducción grabada":
        replay_page()
    else:
        new_investigation()


if __name__ == "__main__":
    main()
