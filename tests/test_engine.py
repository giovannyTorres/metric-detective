from __future__ import annotations

import csv
import hashlib
import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from metric_detective.contracts import InvestigationRequest, MetricConfig
from metric_detective.engine import MISSING, AnalysisEngine, DataValidationError
from metric_detective.synthetic import generate_all, generate_scenario


def request(config=None, **kwargs):
    return InvestigationRequest(
        dataset_id="opaque",
        reference_start="2026-01-05",
        comparison_start="2026-01-12",
        config=config or MetricConfig(),
        **kwargs,
    )


def engine_for(tmp_path, scenario="mobile_drop", seed=17):
    manifest = generate_scenario(tmp_path, scenario, seed)
    config = MetricConfig.model_validate(manifest["config"])
    return AnalysisEngine(
        Path(manifest["orders_path"]), Path(manifest["loads_path"]), request(config)
    )


def write_orders(path, rows, config=None):
    config = config or MetricConfig()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=config.source_columns())
        writer.writeheader()
        writer.writerows(rows)


def order(identifier, stamp, amount="100.00", **kwargs):
    return {
        "order_id": identifier,
        "paid_at": stamp,
        "status": "paid",
        "amount": amount,
        "currency": "MXN",
        "device": "mobile",
        "channel": "paid",
        "country": "MX",
        **kwargs,
    }


def test_compare_independently_reconciles_raw_csv(tmp_path):
    engine = engine_for(tmp_path)
    evidence = engine.run("compare_periods")
    totals = {"reference": Decimal(0), "comparison": Decimal(0)}
    counts = {"reference": 0, "comparison": 0}
    with engine.orders_path.open() as handle:
        for row in csv.DictReader(handle):
            period = "reference" if row["paid_at"][:10] < "2026-01-12" else "comparison"
            totals[period] += Decimal(row["amount"])
            counts[period] += 1
    for period in totals:
        assert Decimal(evidence.values[period]["revenue"]) == totals[period]
        assert evidence.values[period]["orders"] == counts[period]
    assert Decimal(evidence.values["delta_revenue"]) == totals["comparison"] - totals["reference"]
    # Re-execute the actual published controlled query with bound parameters.
    rows = engine.connection.execute(
        evidence.reproduction["sql"], evidence.reproduction["parameters"]
    ).fetchall()
    assert sum(row[1] for row in rows) == evidence.row_count


def test_exact_symmetric_decomposition_with_interaction(tmp_path):
    path = tmp_path / "orders.csv"
    write_orders(
        path,
        [
            order("a", "2026-01-05T12:00:00-06:00", "100.00"),
            order("b", "2026-01-12T12:00:00-06:00", "80.00"),
            order("c", "2026-01-12T12:00:00-06:00", "80.00"),
        ],
    )
    with AnalysisEngine(path, None, request()) as engine:
        values = engine.run("decompose_revenue_change").values
        # R0=100,N0=1,A0=100; R1=160,N1=2,A1=80.
        assert Decimal(values["interaction"]) == -20
        assert Decimal(values["volume_effect"]) == 90
        assert Decimal(values["ticket_effect"]) == -30
        assert Decimal(values["delta_revenue"]) == 60
        assert Decimal(values["volume_effect"]) + Decimal(values["ticket_effect"]) == 60


def test_recurring_ticket_retains_exact_rational_lineage_and_balances_cents(tmp_path):
    path = tmp_path / "orders.csv"
    write_orders(
        path,
        [
            order("a", "2026-01-05T12:00:00-06:00", "0.01"),
            order("b", "2026-01-05T12:00:00-06:00", "0.01"),
            order("c", "2026-01-05T12:00:00-06:00", "0.02"),
            order("d", "2026-01-12T12:00:00-06:00", "0.09"),
        ],
    )
    with AnalysisEngine(path, None, request()) as engine:
        values = engine.run("decompose_revenue_change").values
        exact = {
            name: Fraction(int(value["numerator"]), int(value["denominator"]))
            for name, value in values["exact_rational"].items()
        }
        assert exact["volume_effect"] == Fraction(-31, 300)
        assert exact["ticket_effect"] == Fraction(23, 150)
        assert exact["volume_effect"] + exact["ticket_effect"] == Fraction(1, 20)
        assert Decimal(values["volume_effect"]) + Decimal(values["ticket_effect"]) == Decimal(
            "0.05"
        )


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
@pytest.mark.parametrize("seed", [17, 101, 202])
def test_partition_and_decomposition_reconcile(tmp_path, scenario, seed):
    with engine_for(tmp_path, scenario, seed) as engine:
        total = Decimal(engine.run("compare_periods").values["delta_revenue"])
        decomposition = engine.run("decompose_revenue_change").values
        assert (
            Decimal(decomposition["volume_effect"]) + Decimal(decomposition["ticket_effect"])
            == total
        )
        for dimension in engine.config.dimensions:
            result = engine.run("rank_segment_changes", {"dimension": dimension})
            assert (
                sum(
                    (Decimal(segment["delta_revenue"]) for segment in result.values["segments"]),
                    Decimal(0),
                )
                == total
            )
        daily = engine.run("inspect_daily_pattern").values["days"]
        assert len(daily) == 14
        assert (
            sum(Decimal(day["revenue"]) for day in daily if day["period"] == "comparison")
            - sum(Decimal(day["revenue"]) for day in daily if day["period"] == "reference")
            == total
        )


@pytest.mark.parametrize(
    "scenario,issue",
    [
        ("duplicate_reference", "duplicate_order_identifiers"),
        ("incomplete_loads", "explicit_incomplete_or_failed_loads"),
    ],
)
def test_quality_blocks_business_analysis(tmp_path, scenario, issue):
    with engine_for(tmp_path, scenario) as engine:
        quality = engine.run("check_data_quality")
        assert quality.values["status"] == "blocked"
        assert issue in quality.values["issues"]
        with pytest.raises(DataValidationError, match="blocked"):
            engine.run("compare_periods")


def test_missing_load_information_never_inferred_from_rows(tmp_path):
    with engine_for(tmp_path) as original:
        path = original.orders_path
    with AnalysisEngine(path, None, request()) as engine:
        quality = engine.run("check_data_quality")
        assert quality.values["status"] == "unverified"
        assert not quality.values["completeness_verified"]
        assert "load_completeness_unverified" in engine.run("compare_periods").warnings


def test_load_grid_gap_and_duplicate_declarations(tmp_path):
    with engine_for(tmp_path) as original:
        orders_path, loads_path = original.orders_path, original.loads_path
    lines = loads_path.read_text().splitlines()
    loads_path.write_text("\n".join(lines[:-1]) + "\n")
    with AnalysisEngine(orders_path, loads_path, request()) as engine:
        assert engine.run("check_data_quality").values["missing_load_declarations"] == 1
        assert engine.run("check_data_quality").values["status"] == "unverified"
    loads_path.write_text("\n".join([*lines, lines[1]]) + "\n")
    with AnalysisEngine(orders_path, loads_path, request()) as engine:
        assert engine.run("check_data_quality").values["status"] == "blocked"


def test_single_currency_and_null_dimension(tmp_path):
    path = tmp_path / "orders.csv"
    write_orders(
        path,
        [
            order("a", "2026-01-05T12:00:00-06:00", device=""),
            order("b", "2026-01-12T12:00:00-06:00", currency="USD"),
        ],
    )
    with AnalysisEngine(path, None, request()) as engine:
        assert engine.run("check_data_quality").values["status"] == "blocked"
    write_orders(path, [order("a", "2026-01-05T12:00:00-06:00", device="")])
    with AnalysisEngine(path, None, request()) as engine:
        assert (
            engine.run("rank_segment_changes", {"dimension": "device"}).values["segments"][0][
                "segment"
            ]
            == MISSING
        )


def test_timezone_boundaries_and_unpaid_exclusion(tmp_path):
    path = tmp_path / "orders.csv"
    write_orders(
        path,
        [
            order("outside", "2026-01-05T05:59:59Z", "900.00"),
            order("start", "2026-01-05T06:00:00Z", "100.00"),
            order("boundary", "2026-01-12T06:00:00Z", "200.00"),
            order("unpaid", "2026-01-12T12:00:00-06:00", "999.00", status="pending"),
            order("end", "2026-01-19T06:00:00Z", "900.00"),
        ],
    )
    with AnalysisEngine(path, None, request()) as engine:
        values = engine.run("compare_periods").values
        assert Decimal(values["reference"]["revenue"]) == 100
        assert Decimal(values["comparison"]["revenue"]) == 200
        assert values["reference"]["orders"] == values["comparison"]["orders"] == 1


@pytest.mark.parametrize("stamp", ["2026-01-05", "2026-03-08T02:30:00", "2026-11-01T01:30:00"])
def test_invalid_or_ambiguous_local_datetimes_rejected(tmp_path, stamp):
    path = tmp_path / "orders.csv"
    write_orders(path, [order("a", stamp)])
    with pytest.raises(DataValidationError):
        AnalysisEngine(path, None, request(MetricConfig(timezone="America/New_York")))


@pytest.mark.parametrize(
    "amount", ["", "NaN", "Infinity", "-1", "0.001", "1000000000000.00", " 1.00", "1e2", "+1"]
)
def test_invalid_amounts_rejected_without_data_leak(tmp_path, amount):
    path = tmp_path / "orders.csv"
    write_orders(path, [order("SECRET-identifier", "2026-01-05T12:00:00-06:00", amount)])
    with pytest.raises(DataValidationError) as exc:
        AnalysisEngine(path, None, request())
    assert "SECRET" not in str(exc.value)


def test_new_disappeared_and_zero_denominators(tmp_path):
    with engine_for(tmp_path, "mixed_segments") as engine:
        segments = engine.run("rank_segment_changes", {"dimension": "channel"}).values["segments"]
        assert any(
            item["segment"] == "email" and item["status"] == "new" and item["delta_pct"] is None
            for item in segments
        )
        assert any(
            item["segment"] == "affiliate" and item["status"] == "disappeared" for item in segments
        )
    with engine_for(tmp_path, "empty_period") as engine:
        assert engine.run("compare_periods").values["comparison"]["aov"] is None
        values = engine.run("decompose_revenue_change").values
        assert values["zero_policy_applied"]
        assert Decimal(values["ticket_effect"]) == 0
    path = tmp_path / "all_empty.csv"
    write_orders(path, [])
    with AnalysisEngine(path, None, request()) as engine:
        assert engine.run("compare_periods").values["delta_pct"] is None
        assert engine.run("rank_segment_changes", {"dimension": "device"}).values["segments"] == []
        assert len(engine.run("inspect_daily_pattern").values["days"]) == 14


def test_controlled_sql_and_segmentation_depth(tmp_path):
    with engine_for(tmp_path) as engine:
        with pytest.raises(ValueError):
            engine.run("execute_sql", {"sql": "DROP TABLE orders"})
        with pytest.raises(ValueError):
            engine.run("rank_segment_changes", {"dimension": "device;DROP TABLE orders"})
        with pytest.raises(ValidationError):
            engine.run("compare_periods", {"sql": "SELECT 1"})
        with pytest.raises(ValueError):
            engine.run(
                "drill_down", {"dimensions": ["device", "channel"], "filters": {"country": "MX"}}
            )
        result = engine.run(
            "drill_down", {"dimensions": ["channel"], "filters": {"device": "mobile"}}
        )
        daily = engine.run("inspect_daily_pattern", {"filters": {"device": "mobile"}})
        assert result.row_count == daily.row_count
        assert result.filters == {"device": "mobile"}
        injected = engine.run("inspect_daily_pattern", {"filters": {"device": "mobile' OR 1=1 --"}})
        assert injected.row_count == 0


def test_evidence_idempotency_fingerprint_and_json_roundtrip(tmp_path):
    with engine_for(tmp_path) as engine:
        first = engine.run("compare_periods")
        second = engine.run("compare_periods")
        assert first.model_dump() == second.model_dump()
        assert len(first.data_fingerprint) == 64
        assert json.loads(first.model_dump_json())["values"] == first.values
        before = first.data_fingerprint
    path = tmp_path / "mobile_drop" / "orders.csv"
    path.write_bytes(path.read_bytes() + b"\n")
    with AnalysisEngine(path, tmp_path / "mobile_drop" / "loads.csv", request()) as changed:
        assert changed.fingerprint != before


def test_evidence_is_bound_to_loaded_bytes_when_source_changes(tmp_path):
    with engine_for(tmp_path) as engine:
        before_quality = engine.run("check_data_quality")
        before_comparison = engine.run("compare_periods")
        original_hash = hashlib.sha256(engine.orders_path.read_bytes()).hexdigest()
        # A loaded engine owns an immutable in-memory snapshot, even if a
        # caller replaces or removes the original files afterwards.
        engine.orders_path.write_bytes(b"changed after loading\n")
        engine.loads_path.unlink()
        after_quality = engine.run("check_data_quality")
        assert after_quality.model_dump() == before_quality.model_dump()
        assert after_quality.reproduction["orders_sha256"] == original_hash
        assert engine.run("compare_periods").model_dump() == before_comparison.model_dump()


def test_missing_input_is_a_safe_contract_error(tmp_path):
    with pytest.raises(DataValidationError) as exc:
        AnalysisEngine(tmp_path / "PRIVATE-PATH-orders.csv", None, request())
    assert str(exc.value) == "orders: input file is unavailable"


def test_zero_threshold_does_not_make_zero_change_relevant(tmp_path):
    manifest = generate_scenario(tmp_path, "no_change")
    config = MetricConfig(relevant_change_pct=0)
    with AnalysisEngine(
        Path(manifest["orders_path"]), Path(manifest["loads_path"]), request(config)
    ) as engine:
        values = engine.run("compare_periods").values
        assert Decimal(values["delta_revenue"]) == 0
        assert values["relevant_change"] is False


def test_synthetic_seeds_are_reproducible_and_holdout_different(tmp_path):
    generate_all(tmp_path / "a", 17)
    generate_all(tmp_path / "b", 17)
    generate_all(tmp_path / "c", 101)
    a = (tmp_path / "a" / "mobile_drop" / "orders.csv").read_bytes()
    b = (tmp_path / "b" / "mobile_drop" / "orders.csv").read_bytes()
    c = (tmp_path / "c" / "mobile_drop" / "orders.csv").read_bytes()
    assert hashlib.sha256(a).digest() == hashlib.sha256(b).digest()
    assert a != c
