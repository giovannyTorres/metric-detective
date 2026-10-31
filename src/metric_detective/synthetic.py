"""Small seeded fixtures; scenario labels are never engine or LLM inputs.

Development seed: 17. Holdout seeds: 101, 202. Scenario names are for the
demo selector/evaluator only. The provider must omit dataset IDs and labels.
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .contracts import MetricConfig

REFERENCE_START = date(2026, 1, 5)
COMPARISON_START = date(2026, 1, 12)
DEVELOPMENT_SEED = 17
EVALUATION_SEEDS = (101, 202)
SCENARIOS = [
    {
        "id": "mobile_drop",
        "name": "Menos pedidos en móvil",
        "description": "El cambio se concentra en los pedidos desde móvil.",
    },
    {
        "id": "ticket_drop",
        "name": "Ticket menor",
        "description": "La cantidad de pedidos permanece estable y el importe promedio baja.",
    },
    {
        "id": "incomplete_loads",
        "name": "Cargas incompletas",
        "description": "Una declaración explícita de carga fallida bloquea el diagnóstico.",
    },
    {
        "id": "duplicate_reference",
        "name": "Referencia duplicada",
        "description": "Existen identificadores de pedido duplicados en la referencia.",
    },
    {
        "id": "mixed_segments",
        "name": "Contribuciones opuestas",
        "description": "Unos segmentos crecen mientras otros disminuyen.",
    },
    {
        "id": "ambiguous",
        "name": "Varias explicaciones compatibles",
        "description": "El volumen y el ticket cambian; los datos no identifican la causa.",
    },
    {
        "id": "no_change",
        "name": "Sin cambio relevante",
        "description": "El cambio queda por debajo del umbral explícito de la métrica.",
    },
    {
        "id": "empty_period",
        "name": "Periodo sin pedidos",
        "description": "La comparación no contiene pedidos y el ticket no está observado.",
    },
    {
        "id": "services_ticket_drop",
        "name": "Servicios · menor importe",
        "description": "El mismo motor analiza honorarios liquidados mediante configuración.",
    },
]

# Observable labels only. No assertion of a latent generating cause is scored.
EXPECTATIONS = {
    "mobile_drop": {
        "quality": "ok",
        "delta_sign": -1,
        "leading_device": "mobile",
        "volume_dominant": True,
    },
    "ticket_drop": {
        "quality": "ok",
        "delta_sign": -1,
        "stable_orders": True,
        "ticket_dominant": True,
    },
    "incomplete_loads": {"quality": "blocked", "abstain_business": True},
    "duplicate_reference": {"quality": "blocked", "abstain_business": True},
    "mixed_segments": {
        "quality": "ok",
        "delta_sign": -1,
        "leading_device": "mobile",
        "opposite_contributions": True,
    },
    "ambiguous": {"quality": "ok", "delta_sign": -1, "causal_abstention": True},
    "no_change": {"quality": "ok", "delta_sign": 0, "relevant_change": False},
    "empty_period": {
        "quality": "ok",
        "delta_sign": -1,
        "comparison_orders": 0,
        "undefined_comparison_ticket": True,
    },
    "services_ticket_drop": {
        "quality": "ok",
        "delta_sign": -1,
        "stable_orders": True,
        "ticket_dominant": True,
    },
}


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def scenario_config(scenario_id: str) -> MetricConfig:
    if scenario_id == "services_ticket_drop":
        return MetricConfig(
            name="Settled service fees",
            id_column="engagement_id",
            timestamp_column="settled_at",
            status_column="settlement_status",
            amount_column="fee",
            included_statuses=["settled"],
            dimensions={"device": "delivery", "channel": "acquisition", "country": "region"},
            expected_sources=["services"],
        )
    return MetricConfig()


def generate_scenario(output: Path, scenario_id: str, seed: int = DEVELOPMENT_SEED) -> dict:
    if scenario_id not in {scenario["id"] for scenario in SCENARIOS}:
        raise ValueError("unknown synthetic scenario")
    folder = Path(output) / scenario_id
    folder.mkdir(parents=True, exist_ok=True)
    config = scenario_config(scenario_id)
    rows = []
    for period_index, start in enumerate((REFERENCE_START, COMPARISON_START)):
        for day_index in range(7):
            for device_index, device in enumerate(("mobile", "desktop", "tablet")):
                count = (20, 12, 4)[device_index]
                if period_index:
                    if scenario_id in {"mobile_drop", "incomplete_loads"} and device == "mobile":
                        count = 10
                    if scenario_id == "mixed_segments":
                        count = (9, 17, 4)[device_index]
                    if scenario_id == "ambiguous":
                        count = (15, 9, 3)[device_index]
                    if scenario_id == "empty_period":
                        count = 0
                for order_index in range(count):
                    # Stable keyed randomness makes the no-change scenario exact
                    # and keeps shared orders comparable across selected periods.
                    rng = random.Random(
                        seed * 100000 + day_index * 1000 + device_index * 100 + order_index
                    )
                    amount = Decimal(rng.randrange(9000, 11001)) / 100
                    if period_index and scenario_id in {
                        "ticket_drop",
                        "services_ticket_drop",
                        "ambiguous",
                    }:
                        amount = (amount * Decimal("0.75")).quantize(Decimal("0.01"))
                    paid_at = datetime.combine(
                        start + timedelta(days=day_index), datetime.min.time()
                    ).replace(hour=10 + (order_index % 9), tzinfo=ZoneInfo(config.timezone))
                    logical = {
                        "device": device
                        if scenario_id != "services_ticket_drop"
                        else ("remote", "onsite", "hybrid")[device_index],
                        "channel": ("paid", "organic", "referral")[rng.randrange(3)],
                        "country": ("MX", "US")[rng.randrange(2)],
                    }
                    if scenario_id == "ambiguous":
                        # Device and channel are perfectly associated. Neither
                        # observation alone distinguishes a mechanism.
                        logical["channel"] = {
                            "mobile": "paid",
                            "desktop": "organic",
                            "tablet": "referral",
                        }[device]
                    if scenario_id == "mixed_segments" and order_index == 0:
                        logical["channel"] = "email" if period_index else "affiliate"
                    row = {
                        config.id_column: f"s{seed}-p{period_index}-d{day_index}-v{device_index}-i{order_index}",
                        config.timestamp_column: paid_at.isoformat(),
                        config.status_column: config.included_statuses[0],
                        config.amount_column: format(amount, ".2f"),
                        config.currency_column: config.currency,
                        **{config.dimensions[name]: value for name, value in logical.items()},
                    }
                    rows.append(row)
    if scenario_id == "duplicate_reference":
        rows.append(dict(rows[0]))
    loads = []
    for start in (REFERENCE_START, COMPARISON_START):
        for offset in range(7):
            day = start + timedelta(days=offset)
            status = (
                "failed"
                if scenario_id == "incomplete_loads" and day == COMPARISON_START + timedelta(days=2)
                else "complete"
            )
            for source in config.expected_sources:
                loads.append({"date": day.isoformat(), "source": source, "status": status})
    _write_csv(folder / "orders.csv", config.source_columns(), rows)
    _write_csv(folder / "loads.csv", ["date", "source", "status"], loads)
    (folder / "config.json").write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
    description = next(item for item in SCENARIOS if item["id"] == scenario_id)
    return {
        **description,
        "dataset_id": scenario_id,
        "config": config.model_dump(mode="json"),
        "reference_start": REFERENCE_START.isoformat(),
        "comparison_start": COMPARISON_START.isoformat(),
        "path": folder.as_posix(),
        "orders_path": (folder / "orders.csv").as_posix(),
        "loads_path": (folder / "loads.csv").as_posix(),
    }


def generate_all(output_dir: Path, seed: int = DEVELOPMENT_SEED) -> list[dict]:
    """Generate all demos. Returned metadata is UI-only, never provider input."""
    return [generate_scenario(Path(output_dir), scenario["id"], seed) for scenario in SCENARIOS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--seed", type=int, default=DEVELOPMENT_SEED)
    args = parser.parse_args()
    generate_all(args.output, args.seed)
    print(f"Generated {len(SCENARIOS)} synthetic datasets with seed {args.seed}.")


if __name__ == "__main__":
    main()
