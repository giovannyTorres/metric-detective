# Data and metric contract

This contract describes the initial revenue MVP. A configuration changes column
names, included statuses, the currency, the explicit IANA timezone and allowed
dimensions; it does not replace the analytical engine.

Dataset fingerprints include the original CSV bytes. The repository's
`.gitattributes` disables newline conversion for CSV snapshots so a fresh Linux
or Windows checkout reproduces the saved fingerprint. Editing CSV line endings
intentionally creates a different data version, even if its parsed rows match.

## Grain and metric

- Exactly one row per order or paid service invoice, identified by a unique ID.
- Revenue is the sum of the nonnegative `amount` of records whose status is in
  `included_statuses`. It is paid-order revenue, not recognized/accounting revenue.
- Orders are counts of those included records. Average order value is
  `revenue / orders`; when orders are zero it is undefined, not an observed zero.
- One configured ISO currency per investigation. There is no currency conversion.
- Reference and comparison are disjoint Monday–Sunday weeks, exactly seven local
  calendar days each. End boundaries are exclusive. The comparison follows the
  reference; partial weeks are rejected.
- Refunds, returns, taxes, discounts and cancellations do not get independent
  accounting treatment. The supplied order amount is the entire measured amount.
  Negative amounts are rejected. Returns require a future contract and metric.

## `orders.csv`

UTF-8, comma-delimited CSV with exactly the configured headers. No client/customer
identifiers or unrelated columns are accepted. Default headers:

```csv
order_id,paid_at,status,amount,currency,device,channel,country
example-001,2026-01-05T10:00:00-06:00,paid,125.50,MXN,mobile,organic,MX
example-002,2026-01-05T11:00:00-06:00,cancelled,90.00,MXN,desktop,paid_search,MX
```

| Field | Validation and interpretation |
| --- | --- |
| `order_id` | Required nonblank string, at most 200 characters; duplicates block business diagnosis, including duplicates in the reference period. |
| `paid_at` | ISO datetime, with offset preferred. Naive timestamps are interpreted in the configured timezone. Ambiguous or nonexistent local times and date-only values are rejected. |
| `status` | Required string. Only explicitly included statuses enter the metric. |
| `amount` | Required finite nonnegative decimal, at most two fractional digits, maximum `999999999999.99`; lexical syntax `[0-9]{1,12}(\.[0-9]{1,2})?` (no exponent, plus sign or whitespace). Calculated using decimal arithmetic. |
| `currency` | Required three-letter uppercase code. Eligible paid rows within selected weeks must match the configured currency; unexpected/mixed currency blocks diagnosis. |
| `device`, `channel`, `country` | Allowed dimensions. Empty values belong to the explicit `__missing__` segment. |

The parser limits files to 20 MiB and 100,000 data rows. The API's default
`MD_MAX_UPLOAD_BYTES` limit is stricter: 10 MiB combined across both uploaded
files; the Streamlit uploader also limits each file to 10 MiB. A header-only file is a valid empty dataset;
completeness still needs independent load evidence.

Extra/missing headers, malformed rows and invalid numbers or timestamps fail
contract validation. Mixed currencies among selected eligible rows are recorded
as blocking quality evidence. Null dimension values are kept as a visible
segment; the literal `__missing__` is reserved and rejected in input. Missing
required metric fields cannot be silently imputed.

## `loads.csv`

```csv
date,source,status
2026-01-05,orders,complete
2026-01-06,orders,incomplete
```

The complete file contains one unique `(date, source)` row for every local date
in both selected weeks and every configured `expected_sources` value. Dates use
`YYYY-MM-DD`. Status must be `complete`, `incomplete` or `failed`.

`incomplete` or `failed` loads block business diagnosis. Missing load rows or an
absent `loads.csv` mean completeness is **unverified**; existing order rows never
prove completeness. Duplicate load keys are a data-quality problem. A declared
complete load is a supplied assertion; the application cannot audit a source
system from these files alone.

## Configuration

```json
{
  "name": "Ecommerce revenue",
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
  "relevant_change_pct": 1.0
}
```

The no-relevant-change criterion compares the absolute revenue percentage change
with the configured `relevant_change_pct` threshold. A threshold is an operating
rule, not a statistical significance test. Percent change is undefined when the
reference revenue is zero. The report must state that limitation.

The service example maps invoice fields to this same contract. Its revenue is
paid invoice amount and its count is paid invoices; this is not recurring revenue,
MRR or revenue recognition.

## Exact decomposition

For a nonempty period let `R = N × A`, where `N` is order count and `A` is average
order value. The unsplit identity is:

```text
ΔR = ΔN × A0 + N0 × ΔA + ΔN × ΔA
interaction = ΔN × ΔA
volume_effect = ΔN × A0 + interaction / 2
ticket_effect = N0 × ΔA + interaction / 2
ΔR = volume_effect + ticket_effect
```

Splitting the interaction evenly is the symmetric/Shapley two-factor
decomposition. The interaction is retained in evidence for inspection and is not
added a second time to the two displayed contributions. Exact arithmetic and the
reconciliation residual are tested. `values.exact_rational` stores authoritative
numerator/denominator strings for all three terms. Display volume rounds to cents
using half-even rounding; ticket receives the sub-cent balancing residual so the
two displayed monetary contributions reconcile exactly. Charts display these
cent-level contributions.
When a period has no orders, an average ticket is not observable. The engine's
explicit boundary convention carries the observed ticket from the nonempty period
into the empty side for decomposition only, assigning the entire entry/exit
change to volume and none to ticket. If both periods are empty, both effects are
zero. The displayed average ticket remains undefined. This convention and its
warning belong in the evidence; the report must not claim that an unobserved
ticket changed.

## Segments and evidence

Each partition is calculated separately: device, channel and country each explain
their own total. Contributions across different partitions overlap and must not
be added together. New/disappeared and null segments are retained. Drill-down is
limited to two configured dimensions and explicit whitelisted filters.

Every analytical result carries an evidence ID, units, local period boundaries,
filters, selected row count, warnings, reproducible parameterized SQL or a formula,
and a SHA-256 fingerprint of the source/configuration context. The LLM receives
aggregates, never the original table or order identifiers. Generated SQL comes
from controlled Python tools; model-written SQL or Python is never executed.

Use the evidence inspector or JSON export to reproduce a number. A valid evidence
reference alone does not validate a causal interpretation: findings use checked
templates, while proposed mechanisms remain hypotheses.

## Canonical table

The engine validates CSV first, then creates an in-memory DuckDB table `orders`:

```text
order_id VARCHAR, local_date DATE, status VARCHAR,
amount DECIMAL(18,2), currency VARCHAR,
<each configured logical dimension> VARCHAR
```

`local_date` is the payment timestamp converted to the configured timezone and
then reduced to its local calendar date. Source column mappings become the
canonical column names above. Empty dimensions become `__missing__`. This table
contains all validated input rows; controlled SQL applies included statuses,
currency, selected weeks and explicit filters.

With the original files and exported request, recreate that canonical table and
run a result's `reproduction.sql` with its bound `reproduction.parameters`:

```python
import json
from pathlib import Path
from metric_detective.contracts import InvestigationRequest
from metric_detective.engine import AnalysisEngine

report = json.loads(Path("examples/recorded-investigation.json").read_text(encoding="utf-8"))
request = InvestigationRequest.model_validate(report["request"])
with AnalysisEngine(
    Path("data/mobile_drop/orders.csv"), Path("data/mobile_drop/loads.csv"), request
) as engine:
    assert engine.fingerprint == report["data_fingerprint"]
    for evidence in report["evidence"]:
        reproduction = evidence["reproduction"]
        if "sql" in reproduction:
            print(
                evidence["id"],
                engine.connection.execute(
                    reproduction["sql"], reproduction["parameters"]
                ).fetchall(),
            )
```

Quality evidence instead contains explicit checks and source SHA-256 hashes.
Decomposition additionally provides the formula, zero policy and exact fractions;
the SQL reproduces its underlying period aggregates. Always use files from the
same fingerprint; different bytes or configuration mean a different dataset
version.
