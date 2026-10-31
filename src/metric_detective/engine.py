"""Controlled DuckDB analytics over validated, immutable CSV snapshots.

SQL is selected by Python, never accepted from an LLM. DuckDB sums DECIMAL
amounts; Python Decimal performs ratio/decomposition arithmetic. No float is
used for money. See docs/contract.md for canonicalization and zero policy.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from .contracts import Contract, DailyArgs, DrillArgs, Evidence, InvestigationRequest, RankArgs

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000
MAX_AMOUNT = Decimal("999999999999.99")
MISSING = "__missing__"
ENGINE_VERSION = "metric-detective-analytics-v1"
ZERO = Decimal(0)
AMOUNT_PATTERN = re.compile(r"^[0-9]{1,12}(?:\.[0-9]{1,2})?$")


class DataValidationError(ValueError):
    """Safe validation error: no raw row contents or local paths in its message."""


def decimal_string(value: Decimal | int) -> str:
    return format(Decimal(value), "f")


def ratio(numerator: Decimal | int, denominator: Decimal | int) -> str | None:
    if not denominator:
        return None
    with localcontext() as context:
        context.prec = 38
        return decimal_string(Decimal(numerator) / Decimal(denominator))


def _read_csv(path: Path, expected: list[str], label: str) -> list[dict[str, str]]:
    if path.stat().st_size > MAX_BYTES:
        raise DataValidationError(f"{label}: file exceeds 20 MiB")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)) or set(fields) != set(expected):
                raise DataValidationError(
                    f"{label}: headers must match the configured contract exactly"
                )
            rows = []
            for number, row in enumerate(reader, 2):
                if number > MAX_ROWS + 1:
                    raise DataValidationError(f"{label}: more than 100000 records")
                if None in row or any(value is None for value in row.values()):
                    raise DataValidationError(f"{label}: wrong field count at row {number}")
                if any(len(value) > 500 for value in row.values()):
                    raise DataValidationError(
                        f"{label}: field exceeds 500 characters at row {number}"
                    )
                rows.append(row)
            return rows
    except (UnicodeError, csv.Error) as exc:
        raise DataValidationError(f"{label}: invalid UTF-8 CSV") from exc


def _local_date(value: str, zone: ZoneInfo) -> date:
    if "T" not in value and " " not in value:
        raise ValueError("timestamp must include a time")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        first = stamp.replace(tzinfo=zone, fold=0)
        second = stamp.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("ambiguous or nonexistent local timestamp")
        if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != stamp:
            raise ValueError("nonexistent local timestamp")
        stamp = first
    return stamp.astimezone(zone).date()


class AnalysisEngine:
    TOOLS = frozenset(
        {
            "check_data_quality",
            "compare_periods",
            "decompose_revenue_change",
            "rank_segment_changes",
            "inspect_daily_pattern",
            "drill_down",
        }
    )

    def __init__(
        self, orders_path: Path, loads_path: Path | None, request: InvestigationRequest
    ) -> None:
        self.request = request
        self.config = request.config
        self.orders_path = Path(orders_path)
        self.loads_path = Path(loads_path) if loads_path is not None else None
        self._records = self._validate_orders()
        self._loads = self._validate_loads()
        digest = hashlib.sha256()
        digest.update(ENGINE_VERSION.encode())
        digest.update(self.orders_path.read_bytes())
        digest.update(b"\x00loads\x00")
        digest.update(self.loads_path.read_bytes() if self.loads_path is not None else b"ABSENT")
        digest.update(self.config.model_dump_json().encode())
        self.fingerprint = digest.hexdigest()
        self.connection = duckdb.connect(":memory:", config={"threads": 1})
        dimensions = ", ".join(f'"{name}" VARCHAR' for name in self.config.dimensions)
        self.connection.execute(
            "CREATE TABLE orders (order_id VARCHAR, local_date DATE, status VARCHAR, "
            f"amount DECIMAL(18,2), currency VARCHAR, {dimensions})"
        )
        if self._records:
            placeholders = ",".join("?" for _ in range(5 + len(self.config.dimensions)))
            self.connection.executemany(
                f"INSERT INTO orders VALUES ({placeholders})", self._records
            )
        self._quality_status, self._issues, self._quality_values = self._quality()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> AnalysisEngine:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _validate_orders(self) -> list[tuple[Any, ...]]:
        rows = _read_csv(self.orders_path, self.config.source_columns(), "orders")
        result = []
        zone = ZoneInfo(self.config.timezone)
        for number, row in enumerate(rows, 2):
            try:
                identifier = row[self.config.id_column]
                state = row[self.config.status_column]
                currency = row[self.config.currency_column]
                if (
                    not identifier.strip()
                    or len(identifier) > 200
                    or identifier != identifier.strip()
                ):
                    raise ValueError("invalid identifier")
                if not state.strip() or len(state) > 100 or state != state.strip():
                    raise ValueError("invalid state")
                if (
                    len(currency) != 3
                    or not currency.isascii()
                    or not currency.isupper()
                    or not currency.isalpha()
                ):
                    raise ValueError("invalid currency")
                raw_amount = row[self.config.amount_column]
                if not AMOUNT_PATTERN.fullmatch(raw_amount):
                    raise ValueError("amount must use plain nonnegative decimal notation")
                amount = Decimal(raw_amount)
                if not amount.is_finite() or amount < 0 or amount > MAX_AMOUNT:
                    raise ValueError("amount out of range")
                if amount != amount.quantize(Decimal("0.01")):
                    raise ValueError("amount must have at most two fractional digits")
                local_day = _local_date(row[self.config.timestamp_column], zone)
                dims = []
                for source in self.config.dimensions.values():
                    value = row[source].strip()
                    if len(value) > 200 or value == MISSING:
                        raise ValueError("dimension too long")
                    dims.append(value or MISSING)
                result.append((identifier, local_day, state, amount, currency, *dims))
            except (ValueError, InvalidOperation, OverflowError) as exc:
                raise DataValidationError(
                    f"orders: invalid required value at row {number}"
                ) from exc
        return result

    def _validate_loads(self) -> list[tuple[date, str, str]] | None:
        if self.loads_path is None:
            return None
        rows = _read_csv(self.loads_path, ["date", "source", "status"], "loads")
        result = []
        for number, row in enumerate(rows, 2):
            try:
                day = date.fromisoformat(row["date"])
                if day.isoformat() != row["date"]:
                    raise ValueError("invalid date")
                source = row["source"]
                if not source.strip() or len(source) > 100 or source != source.strip():
                    raise ValueError("invalid source")
                if row["status"] not in {"complete", "incomplete", "failed"}:
                    raise ValueError("invalid load state")
                result.append((day, source, row["status"]))
            except ValueError as exc:
                raise DataValidationError(f"loads: invalid required value at row {number}") from exc
        return result

    def _quality(self) -> tuple[str, list[str], dict[str, Any]]:
        issues: list[str] = []
        statuses = self.config.included_statuses
        selected_dates = {
            start + timedelta(days=offset)
            for start in (self.request.reference_start, self.request.comparison_start)
            for offset in range(7)
        }
        duplicates = sum(
            count - 1 for count in Counter(row[0] for row in self._records).values() if count > 1
        )
        if duplicates:
            issues.append("duplicate_order_identifiers")
        eligible = [row for row in self._records if row[1] in selected_dates and row[2] in statuses]
        if any(row[4] != self.config.currency for row in eligible):
            issues.append("mixed_or_unexpected_currency")
        null_dimensions = sum(1 for row in eligible if MISSING in row[5:])
        completeness_verified = False
        missing_count = len(selected_dates) * len(self.config.expected_sources)
        incomplete_count = 0
        duplicate_loads = 0
        if self._loads is None:
            issues.append("load_completeness_unverified")
        else:
            relevant = [
                row
                for row in self._loads
                if row[0] in selected_dates and row[1] in self.config.expected_sources
            ]
            keys = Counter((row[0], row[1]) for row in relevant)
            duplicate_loads = sum(count - 1 for count in keys.values() if count > 1)
            if duplicate_loads:
                issues.append("duplicate_load_declarations")
            missing_count -= len(keys)
            incomplete_count = sum(row[2] != "complete" for row in relevant)
            if incomplete_count:
                issues.append("explicit_incomplete_or_failed_loads")
            if missing_count:
                issues.append("load_completeness_unverified")
            completeness_verified = not (missing_count or incomplete_count or duplicate_loads)
        blocking = {
            "duplicate_order_identifiers",
            "mixed_or_unexpected_currency",
            "duplicate_load_declarations",
            "explicit_incomplete_or_failed_loads",
        }
        status = (
            "blocked"
            if blocking.intersection(issues)
            else ("ok" if completeness_verified else "unverified")
        )
        values = {
            "status": status,
            "issues": issues,
            "completeness_verified": completeness_verified,
            "valid_rows": len(self._records),
            "eligible_rows": len(eligible),
            "duplicate_ids": duplicates,
            "missing_load_declarations": missing_count,
            "incomplete_load_declarations": incomplete_count,
            "duplicate_load_declarations": duplicate_loads,
            "rows_with_missing_dimensions": null_dimensions,
        }
        return status, issues, values

    def _where(self, filters: dict[str, str] | None = None) -> tuple[str, list[Any]]:
        statuses = self.config.included_statuses
        clauses = [f"status IN ({','.join('?' for _ in statuses)})", "currency = ?"]
        parameters: list[Any] = [*statuses, self.config.currency]
        clauses.append(
            "((local_date >= ? AND local_date < ?) OR (local_date >= ? AND local_date < ?))"
        )
        parameters.extend(
            [
                self.request.reference_start,
                self.request.reference_start + timedelta(days=7),
                self.request.comparison_start,
                self.request.comparison_start + timedelta(days=7),
            ]
        )
        for name, value in sorted((filters or {}).items()):
            self._dimension(name)
            if not isinstance(value, str) or not value or len(value) > 200:
                raise ValueError("filters require bounded nonempty string values")
            clauses.append(f'"{name}" = ?')
            parameters.append(value)
        return " AND ".join(clauses), parameters

    def _dimension(self, name: str) -> None:
        if name not in self.config.dimensions:
            raise ValueError("dimension is not allowed by metric configuration")

    def _query(self, sql: str, parameters: list[Any]) -> list[tuple[Any, ...]]:
        return self.connection.execute(sql, parameters).fetchall()

    def _reproduction(self, sql: str, parameters: list[Any], **extra: Any) -> dict[str, Any]:
        return {
            "sql": sql,
            "parameters": [
                item.isoformat() if isinstance(item, (date, datetime)) else item
                for item in parameters
            ],
            "canonicalization": "docs/contract.md#canonical-table",
            "engine_version": ENGINE_VERSION,
            **extra,
        }

    def _evidence(
        self,
        tool: str,
        args: dict[str, Any],
        values: dict[str, Any],
        units: dict[str, str],
        row_count: int,
        reproduction: dict[str, Any],
        filters: dict[str, str] | None = None,
        warnings: list[str] | None = None,
    ) -> Evidence:
        canonical = json.dumps(
            {
                "tool": tool,
                "args": args,
                "fingerprint": self.fingerprint,
                "periods": self.request.periods(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        inherited = [] if self._quality_status == "ok" else list(self._issues)
        return Evidence(
            id="ev_" + hashlib.sha256(canonical.encode()).hexdigest()[:20],
            tool=tool,
            values=values,
            units=units,
            periods=self.request.periods(),
            filters=filters or {},
            row_count=row_count,
            warnings=list(dict.fromkeys([*inherited, *(warnings or [])])),
            reproduction=reproduction,
            data_fingerprint=self.fingerprint,
        )

    def run(self, tool: str, args: dict[str, Any] | None = None) -> Evidence:
        if tool not in self.TOOLS:
            raise ValueError("unknown analytical tool")
        args = args or {}
        argument_model = {
            "rank_segment_changes": RankArgs,
            "inspect_daily_pattern": DailyArgs,
            "drill_down": DrillArgs,
        }.get(tool, Contract)
        parsed = argument_model.model_validate(args).model_dump()
        if tool != "check_data_quality" and self._quality_status == "blocked":
            raise DataValidationError("business diagnosis blocked by data quality")
        return getattr(self, f"_{tool}")(parsed)

    def _check_data_quality(self, args: dict[str, Any]) -> Evidence:
        return self._evidence(
            "check_data_quality",
            args,
            dict(self._quality_values),
            {"valid_rows": "orders", "eligible_rows": "orders", "duplicate_ids": "orders"},
            len(self._records),
            {
                "checks": [
                    "strict_csv_contract",
                    "unique_order_id",
                    "single_currency",
                    "complete_load_grid",
                ],
                "formula": "Every selected local date × expected source needs one explicit complete declaration; row presence is never proof of completeness.",
                "orders_sha256": hashlib.sha256(self.orders_path.read_bytes()).hexdigest(),
                "loads_sha256": hashlib.sha256(self.loads_path.read_bytes()).hexdigest()
                if self.loads_path
                else None,
                "engine_version": ENGINE_VERSION,
                "canonicalization": "docs/contract.md#canonical-table",
            },
        )

    def _totals(
        self, filters: dict[str, str] | None = None
    ) -> tuple[dict[str, Any], str, list[Any], int]:
        where, parameters = self._where(filters)
        sql = (
            "SELECT CASE WHEN local_date >= ? AND local_date < ? THEN 'reference' ELSE 'comparison' END AS period, "
            "COUNT(*) AS orders, SUM(amount) AS revenue FROM orders WHERE "
            + where
            + " GROUP BY period ORDER BY period"
        )
        parameters = [
            self.request.reference_start,
            self.request.reference_start + timedelta(days=7),
            *parameters,
        ]
        result = {
            name: {"revenue": "0.00", "orders": 0, "aov": None}
            for name in ("reference", "comparison")
        }
        row_count = 0
        for period, count, revenue in self._query(sql, parameters):
            result[period] = {
                "revenue": decimal_string(revenue),
                "orders": count,
                "aov": ratio(revenue, count),
            }
            row_count += count
        delta = Decimal(result["comparison"]["revenue"]) - Decimal(result["reference"]["revenue"])
        reference = Decimal(result["reference"]["revenue"])
        delta_orders = result["comparison"]["orders"] - result["reference"]["orders"]
        pct = ratio(delta * 100, reference)
        values = {
            **result,
            "delta_revenue": decimal_string(delta),
            "delta_pct": pct,
            "delta_orders": delta_orders,
            "delta_orders_pct": ratio(delta_orders * 100, result["reference"]["orders"]),
            "relevant_change": abs(Decimal(pct)) >= Decimal(str(self.config.relevant_change_pct))
            if pct is not None
            else bool(delta),
        }
        return values, sql, parameters, row_count

    def _compare_periods(self, args: dict[str, Any]) -> Evidence:
        values, sql, parameters, count = self._totals()
        warnings = (
            ["zero_reference_revenue_percentage_undefined"] if values["delta_pct"] is None else []
        )
        return self._evidence(
            "compare_periods",
            args,
            values,
            {
                "revenue": self.config.currency,
                "delta_revenue": self.config.currency,
                "aov": f"{self.config.currency}/order",
                "orders": "orders",
                "delta_pct": "%",
            },
            count,
            self._reproduction(sql, parameters),
            warnings=warnings,
        )

    def _decompose_revenue_change(self, args: dict[str, Any]) -> Evidence:
        totals, sql, parameters, count = self._totals()
        n0, n1 = totals["reference"]["orders"], totals["comparison"]["orders"]
        r0, r1 = Decimal(totals["reference"]["revenue"]), Decimal(totals["comparison"]["revenue"])
        with localcontext() as context:
            context.prec = 38
            # An absent period has no observed ticket. Carry the observed opposite
            # ticket into it: all entry/exit revenue is allocated to volume.
            a0_exact = Fraction(r0) / n0 if n0 else (Fraction(r1) / n1 if n1 else Fraction(0))
            a1_exact = Fraction(r1) / n1 if n1 else a0_exact
            interaction_exact = (n1 - n0) * (a1_exact - a0_exact)
            volume_exact = (n1 - n0) * a0_exact + interaction_exact / 2
            ticket_exact = n0 * (a1_exact - a0_exact) + interaction_exact / 2
            assert volume_exact + ticket_exact == Fraction(r1 - r0)
            a0 = Decimal(a0_exact.numerator) / a0_exact.denominator
            a1 = Decimal(a1_exact.numerator) / a1_exact.denominator
            interaction = Decimal(interaction_exact.numerator) / interaction_exact.denominator
            volume = (Decimal(volume_exact.numerator) / volume_exact.denominator).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_EVEN
            )
            ticket = r1 - r0 - volume
            values = {
                "volume_effect": decimal_string(volume),
                "ticket_effect": decimal_string(ticket),
                "interaction": decimal_string(interaction),
                "delta_revenue": decimal_string(r1 - r0),
                "reference_orders": n0,
                "comparison_orders": n1,
                "reference_ticket_for_decomposition": decimal_string(a0),
                "comparison_ticket_for_decomposition": decimal_string(a1),
                "zero_policy_applied": not (n0 and n1),
                "method": "symmetric_shapley",
                "exact_rational": {
                    name: {"numerator": str(value.numerator), "denominator": str(value.denominator)}
                    for name, value in (
                        ("volume_effect", volume_exact),
                        ("ticket_effect", ticket_exact),
                        ("interaction", interaction_exact),
                    )
                },
                "rounding_policy": "Exact rational values are authoritative. Volume is rounded half-even to cents; ticket receives the sub-cent balancing residual so displayed allocations reconcile exactly.",
            }
        warnings = (
            ["empty_period_ticket_unobserved_carry_observed_ticket_for_decomposition"]
            if not (n0 and n1)
            else []
        )
        return self._evidence(
            "decompose_revenue_change",
            args,
            values,
            {
                key: self.config.currency
                for key in ("volume_effect", "ticket_effect", "interaction", "delta_revenue")
            },
            count,
            self._reproduction(
                sql,
                parameters,
                formula={
                    "interaction": "(N1-N0)*(A1-A0)",
                    "volume_effect": "(N1-N0)*A0 + interaction/2",
                    "ticket_effect": "delta_revenue-volume_effect = N0*(A1-A0)+interaction/2",
                    "reconciliation": "volume_effect + ticket_effect = R1-R0; interaction is already allocated, never add it again",
                    "zero_policy": "If one period has no orders, carry the observed ticket into that period; if both are empty use zero. This is a convention, not an observed ticket.",
                    "rounding": "exact_rational stores authoritative fractions; decimal volume rounds half-even to cents and ticket=delta_revenue-volume, preserving the exact monetary total.",
                },
            ),
            warnings=warnings,
        )

    def _segments(
        self, tool: str, args: dict[str, Any], dimensions: list[str], filters: dict[str, str]
    ) -> Evidence:
        if len(set(dimensions)) != len(dimensions):
            raise ValueError("segmentation dimensions must be unique")
        if len(set(dimensions).union(filters)) > self.request.limits.max_depth:
            raise ValueError("segmentation exceeds configured depth")
        for name in dimensions:
            self._dimension(name)
        where, parameters = self._where(filters)
        select_dims = ", ".join(f'"{name}"' for name in dimensions)
        sql = (
            f"SELECT {select_dims}, "
            "SUM(CASE WHEN local_date >= ? AND local_date < ? THEN amount ELSE 0 END) AS reference_revenue, "
            "SUM(CASE WHEN local_date >= ? AND local_date < ? THEN amount ELSE 0 END) AS comparison_revenue, "
            "COUNT(*) FILTER (WHERE local_date >= ? AND local_date < ?) AS reference_orders, "
            "COUNT(*) FILTER (WHERE local_date >= ? AND local_date < ?) AS comparison_orders "
            f"FROM orders WHERE {where} GROUP BY {select_dims}"
        )
        ref = [self.request.reference_start, self.request.reference_start + timedelta(days=7)]
        comp = [self.request.comparison_start, self.request.comparison_start + timedelta(days=7)]
        parameters = [*ref, *comp, *ref, *comp, *parameters]
        rows = self._query(sql, parameters)
        delta = sum((row[len(dimensions) + 1] - row[len(dimensions)] for row in rows), ZERO)
        segments = []
        for row in rows:
            r0, r1, n0, n1 = row[len(dimensions) :]
            change = r1 - r0
            segment = (
                row[0]
                if len(dimensions) == 1
                else dict(zip(dimensions, row[: len(dimensions)], strict=True))
            )
            segments.append(
                {
                    "segment": segment,
                    "reference_revenue": decimal_string(r0),
                    "comparison_revenue": decimal_string(r1),
                    "delta_revenue": decimal_string(change),
                    "reference_orders": n0,
                    "comparison_orders": n1,
                    "contribution_pct": ratio(change * 100, delta),
                    "delta_pct": ratio(change * 100, r0),
                    "status": "new" if n0 == 0 else ("disappeared" if n1 == 0 else "present"),
                }
            )
        segments.sort(
            key=lambda item: (
                -abs(Decimal(item["delta_revenue"])),
                json.dumps(item["segment"], sort_keys=True),
            )
        )
        values: dict[str, Any] = {
            "segments": segments,
            "delta_revenue": decimal_string(delta),
            "partition": dimensions,
            "dimensions": dimensions,
            "reconciles": sum((Decimal(item["delta_revenue"]) for item in segments), ZERO) == delta,
        }
        if len(dimensions) == 1:
            values["dimension"] = dimensions[0]
        warnings = (
            ["contribution_percent_undefined_when_partition_delta_zero"] if delta == 0 else []
        )
        warnings.append(
            "partitions_overlap_across_dimensions_do_not_add_device_channel_country_contributions"
        )
        return self._evidence(
            tool,
            args,
            values,
            {
                "revenue": self.config.currency,
                "delta_revenue": self.config.currency,
                "contribution_pct": "%",
                "orders": "orders",
            },
            sum(item["reference_orders"] + item["comparison_orders"] for item in segments),
            self._reproduction(
                sql,
                parameters,
                formula="Within this partition only: sum(segment delta_revenue) = filtered revenue change.",
            ),
            filters=filters,
            warnings=warnings,
        )

    def _rank_segment_changes(self, args: dict[str, Any]) -> Evidence:
        return self._segments("rank_segment_changes", args, [args["dimension"]], {})

    def _drill_down(self, args: dict[str, Any]) -> Evidence:
        return self._segments("drill_down", args, args["dimensions"], args["filters"])

    def _inspect_daily_pattern(self, args: dict[str, Any]) -> Evidence:
        filters = args["filters"]
        if len(filters) > self.request.limits.max_depth:
            raise ValueError("daily filters exceed configured depth")
        where, parameters = self._where(filters)
        sql = f"SELECT local_date, COUNT(*) AS orders, SUM(amount) AS revenue FROM orders WHERE {where} GROUP BY local_date ORDER BY local_date"
        rows = self._query(sql, parameters)
        observed = {row[0]: row[1:] for row in rows}
        days = []
        for period, start in (
            ("reference", self.request.reference_start),
            ("comparison", self.request.comparison_start),
        ):
            for offset in range(7):
                day = start + timedelta(days=offset)
                count, revenue = observed.get(day, (0, ZERO))
                days.append(
                    {
                        "date": day.isoformat(),
                        "period": period,
                        "day_index": offset,
                        "revenue": decimal_string(revenue),
                        "orders": count,
                    }
                )
        return self._evidence(
            "inspect_daily_pattern",
            args,
            {"days": days},
            {"revenue": self.config.currency, "orders": "orders"},
            sum(item["orders"] for item in days),
            self._reproduction(
                sql,
                parameters,
                fill="Explicit 7-day date spine for each period; no orders => 0, load completeness remains separate.",
            ),
            filters=filters,
        )
