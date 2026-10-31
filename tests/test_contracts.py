from datetime import date

import pytest
from pydantic import ValidationError

from metric_detective.contracts import InvestigationRequest, Limits, MetricConfig


def test_seven_day_monday_periods_and_nonoverlap():
    request = InvestigationRequest(
        dataset_id="demo", reference_start="2026-01-05", comparison_start="2026-01-12"
    )
    assert request.periods() == {
        "reference": {"start": "2026-01-05", "end_exclusive": "2026-01-12"},
        "comparison": {"start": "2026-01-12", "end_exclusive": "2026-01-19"},
    }
    for reference, comparison in (("2026-01-06", "2026-01-13"), ("2026-01-12", "2026-01-12")):
        with pytest.raises(ValidationError):
            InvestigationRequest(
                dataset_id="x", reference_start=reference, comparison_start=comparison
            )


def test_configuration_rejects_sql_names_unknown_timezones_and_column_aliasing():
    for fields in (
        {"dimensions": {"device;DROP TABLE orders": "device"}},
        {"timezone": "Not/A_Zone"},
        {"amount_column": "order_id"},
        {"dimensions": {"amount": "device"}},
        {"included_statuses": ["paid", "paid"]},
        {"expected_sources": [""]},
    ):
        with pytest.raises(ValidationError):
            MetricConfig(**fields)


def test_limits_are_bounded_and_unknown_fields_rejected():
    for fields in ({"max_calls": 9}, {"max_depth": 3}, {"max_tokens": 0}, {"extra": 1}):
        with pytest.raises(ValidationError):
            Limits(**fields)
    with pytest.raises(ValidationError):
        InvestigationRequest(
            dataset_id="../private",
            reference_start=date(2026, 1, 5),
            comparison_start=date(2026, 1, 12),
        )
