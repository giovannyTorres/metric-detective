"""Public, versioned contracts. Monetary evidence uses decimal strings."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricConfig(Contract):
    name: str = Field(default="Ecommerce revenue", min_length=1, max_length=120)
    id_column: str = "order_id"
    timestamp_column: str = "paid_at"
    status_column: str = "status"
    amount_column: str = "amount"
    currency_column: str = "currency"
    dimensions: dict[str, str] = Field(
        default_factory=lambda: {"device": "device", "channel": "channel", "country": "country"},
        min_length=1,
        max_length=3,
    )
    included_statuses: list[str] = Field(
        default_factory=lambda: ["paid"], min_length=1, max_length=10
    )
    timezone: str = "America/Mexico_City"
    currency: str = Field(default="MXN", pattern=r"^[A-Z]{3}$")
    expected_sources: list[str] = Field(
        default_factory=lambda: ["orders"], min_length=1, max_length=10
    )
    relevant_change_pct: float = Field(default=1.0, ge=0, le=100, allow_inf_nan=False)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a known IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def valid_definition(self) -> MetricConfig:
        columns = [
            self.id_column,
            self.timestamp_column,
            self.status_column,
            self.amount_column,
            self.currency_column,
            *self.dimensions.values(),
        ]
        if any(not IDENTIFIER.fullmatch(item) for item in [*columns, *self.dimensions]):
            raise ValueError("column and dimension names must be safe identifiers")
        if len(columns) != len(set(columns)):
            raise ValueError("configured source columns must be distinct")
        logical_dimensions = [name.casefold() for name in self.dimensions]
        if len(logical_dimensions) != len(set(logical_dimensions)):
            raise ValueError("logical dimensions must be distinct ignoring case")
        reserved = {"order_id", "local_date", "status", "amount", "currency", "period"}
        if reserved.intersection(logical_dimensions):
            raise ValueError("logical dimension names conflict with canonical columns")
        for items in (self.included_statuses, self.expected_sources):
            if len(items) != len(set(items)):
                raise ValueError("statuses and sources must be unique")
            if any(not item.strip() or len(item) > 100 or item != item.strip() for item in items):
                raise ValueError("statuses and sources must be nonblank bounded strings")
        return self

    def source_columns(self) -> list[str]:
        return [
            self.id_column,
            self.timestamp_column,
            self.status_column,
            self.amount_column,
            self.currency_column,
            *self.dimensions.values(),
        ]


class Limits(Contract):
    max_calls: int = Field(default=8, ge=1, le=8)
    max_depth: int = Field(default=2, ge=1, le=2)
    max_seconds: float = Field(default=180, gt=0, le=900, allow_inf_nan=False)
    max_tokens: int = Field(default=20000, ge=256, le=100000)
    max_retries: int = Field(default=1, ge=0, le=2)


class InvestigationRequest(Contract):
    dataset_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    reference_start: date
    comparison_start: date
    config: MetricConfig = Field(default_factory=MetricConfig)
    mode: Literal["agent", "baseline"] = "agent"
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def full_weeks(self) -> InvestigationRequest:
        # Check bounds before adding seven days; date.max input must produce a
        # contract validation error rather than an uncaught arithmetic overflow.
        if max(self.reference_start, self.comparison_start) > date(9999, 12, 24):
            raise ValueError("selected period exceeds the supported date range")
        if self.reference_start.weekday() or self.comparison_start.weekday():
            raise ValueError("periods must start on Monday; each covers seven complete local days")
        if self.comparison_start < self.reference_start + timedelta(days=7):
            raise ValueError("comparison must follow the complete reference week without overlap")
        return self

    def periods(self) -> dict[str, dict[str, str]]:
        return {
            name: {
                "start": start.isoformat(),
                "end_exclusive": (start + timedelta(days=7)).isoformat(),
            }
            for name, start in (
                ("reference", self.reference_start),
                ("comparison", self.comparison_start),
            )
        }


class Evidence(Contract):
    id: str
    tool: str
    values: dict[str, Any]
    units: dict[str, str]
    periods: dict[str, Any]
    filters: dict[str, str] = Field(default_factory=dict)
    row_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    reproduction: dict[str, Any]
    data_fingerprint: str


class RankArgs(Contract):
    dimension: str


class DailyArgs(Contract):
    filters: dict[str, str] = Field(default_factory=dict, max_length=2)


class DrillArgs(DailyArgs):
    dimensions: list[str] = Field(min_length=1, max_length=2)
