# Limitations and extensions

## What the product can establish

The engine measures revenue changes and exact arithmetic contributions. A device
or channel can account for a large share of the observed difference without being
its cause. The data contain no experiment assignment, customer journeys, pricing
history, outages or marketing-spend history. Multiple mechanisms can fit the same
observed orders. Hypotheses and missing data are therefore separate from findings.

Synthetic generator labels are evaluation metadata. They are not supplied to the
investigator and are not treated as identifiable causes unless delivered data
support that interpretation. Small synthetic demonstrations are not evidence of
general production performance or superiority over a deterministic baseline.

## Metric and data scope

- One currency and one row per paid order/invoice; no exchange rates or joins.
- Seven-day, Monday-aligned, disjoint local periods; no partial-week comparisons.
- No refunds, returns, cohort effects, recognized revenue or subscription MRR.
- Average ticket is undefined when no paid orders exist. Percentage change with a
  zero reference denominator is undefined.
- Empty dimensions remain an explicit missing segment. New and disappeared
  segments remain visible.
- Completeness relies on supplied load metadata. Absence of a load declaration
  cannot be repaired by counting records; a declaration is not an audit of the
  upstream source system.
- The relevant-change threshold is an explicit operating rule. It is not a
  confidence level, significance test or automatic causal judgment.

## Model and report scope

The implemented live provider is Ollama through LangChain. Model, base URL and
optional endpoint credentials are environment configuration. Other providers are
not advertised as supported. A local server still needs enough memory and an
installed model. Provider timeouts, structured-output failures and exhausted
budgets are surfaced; no key-free prerecorded result is presented as a fresh
model run.

Quantitative findings use checked templates and evidence paths. This is a useful
semantic boundary but not an open-ended natural-language truth checker. Human
review remains appropriate before acting on a proposed explanation. The system
does not invent confidence percentages. Costs remain unavailable when verified
pricing is not supplied; an unavailable cost is not a zero cost.

## Operational scope

This is a local portfolio MVP with bounded uploads, a persistent worker and
checkpoint recovery. It is not a multi-tenant production service: it does not
include user authentication, tenant isolation, an upload malware scanner,
centralized secret management, retention automation, encrypted object storage or
an internet-facing deployment. Do not upload customer identifiers or private
files to the demonstration.

Leases, fencing and session advisory locks protect ownership and database writes.
An intent checkpoint precedes a provider request. If interrupted while the
request is in flight, recovery ends partial with provider usage marked unknown,
rather than reissuing the request. This cannot establish whether the remote
provider finished or the tokens it consumed. Deadline checks happen between
bounded ingestion/analysis operations, without preemptive interruption; network
timeouts are separate. Read token usage/latency with these limits and the
evaluation's sampling and retry limitations.

## Possible extensions

Extensions require new contracts and tests: refund-aware accounting; explicit
experiment or incident evidence; configurable non-weekly periods; authenticated
multi-user access; durable object storage and retention; separately implemented
and evaluated providers. Additional agents, vector databases and infrastructure
are unnecessary for the initial analytical workflow.
