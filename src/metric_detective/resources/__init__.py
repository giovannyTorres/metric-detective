"""Small, non-secret resources included in source and wheel installations."""

import json
from importlib.resources import files


def load_recorded_report() -> dict:
    """Load the labelled historical real-model replay, without API or database."""
    return json.loads(
        files(__package__).joinpath("recorded-investigation.json").read_text(encoding="utf-8")
    )
