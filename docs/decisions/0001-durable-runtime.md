# ADR 0001: DBOS owns durable execution behind an Atelier adapter

- Status: accepted for the first product slice
- Date: 2026-08-10
- Evidence: H0/AD0 probe for board item `fc45ff2cff8d46e397c16ea12d94affa`

## Context

Atelier 2 must resume one versioned workflow run after a process restart without
inventing a second source of truth, silently changing the workflow revision, or
replaying an external effect whose outcome is unknown. The first product slice
also needs one caller transaction to create the Atelier run and enqueue durable
execution. V1 is single-user and may use one canonical SQLite file for Atelier
product state and the runtime ledger.

We evaluated DBOS rather than building a workflow engine. Temporal is not scored
or rejected by the probe; it is outside the V1 single-process operating model.

## Decision

Use DBOS 2.29.0 behind the public `src/atelier2/adapters/dbos/` boundary for the
first vertical slice. Product code does not import DBOS outside that adapter.
The adapter owns the runtime, canonical SQLAlchemy/SQLite datasource, durable
start/advance/reconcile implementations, and effect ledger so each caller
decision and its DBOS enqueue share one transaction.

One canonical SQLite engine and file contains Atelier product tables, DBOS
system tables, and `datasource_outputs`. The persistent loopback adapter uses a
separately configured SQLite file as its external destination; it is not a
second Atelier store.

Atelier product rows are cockpit truth. DBOS `operation_outputs` and
`workflow_status` are a recoverable executor ledger, so they may lag a committed
datasource transaction without making the cockpit lie. Atelier's immutable
`WorkflowRevisionHash` is a product identity and remains distinct from DBOS
`application_version`, which fences executor compatibility.

Before an external call, Atelier durably records an effect intent bound to the
logical key, exact request bytes and hash, workflow revision, adapter revision,
destination, and external-store identity. Recovery reads back the external
outcome. It executes only after authoritative absence; an unknown outcome
becomes durable `WAITING_RECONCILIATION`, never a blind retry. An immutable
operator command resolves the exact waiting state by confirming a found effect
or authorizing that same request's execution.

## Production boundary

The runtime lives behind `atelier2.adapters.dbos`. Contracts own immutable
workflow revisions, caller-supplied run identifiers, effect lifecycles, exact
payloads, and reconciliation decisions. Application functions depend on narrow
start, advance, and reconcile ports. The adapter owns the canonical engine,
schema, durable codecs and transactions, explicit application version, and the
three-operation effect and reconciliation workflows. DBOS and SQLAlchemy do not
cross that boundary. Workflow-revision hashes remain product identity, while
the configured DBOS application version remains the executor recovery fence.

A process owns exactly one compatible DBOS binding of canonical database path,
application version, and resource-free effect-adapter binding. The latter
contains revision, destination, and stable operational/store identity and is
persisted in intents and receipts. Restart refuses configuration contradicting
durable intents. Identical callers share one opened adapter and runtime under
counted leases; an incompatible lease is refused before adapter open or global
mutation. Only the last release destroys DBOS, closes the adapter, and disposes
the engine, each exactly once. H2 has one concrete file-backed adapter, so its
resolved operational identity is also checked against the canonical file
identity, including hardlink aliases, before either store is opened. This is a
bounded loopback invariant rather than a generic provider contract.

## Executable evidence

| Production proof | What it establishes |
| --- | --- |
| Atomic start and advance | Revision/run/bootstrap and intent/effect enqueue each commit or roll back together; exact retries do not enqueue again. |
| Bootstrap recovery | A matching application version fills the outer DBOS ledger after a datasource commit without changing or regressing the product run. |
| Effect recovery | Real subprocess kills after recorded observation (C1), after external commit (C2), and after product confirmation converge with one external call and one receipt. |
| Unknown outcome | A committed unknown remains waiting across restart and provider-state change; no effect occurs until an operator command owns the intent. |
| Reconciliation | FOUND and authorized-absence commands preserve operator provenance; concurrent opposing commands commit one CAS winner and one rejected loser. |
| Atomic final commit | Receipt, intent, run, and owning command roll back together under an injected database failure. |
| Runtime lifecycle | Equivalent leases share one engine and adapter; conflicts, failed initialization, concurrent close, store drift, and two-process recovery preserve one binding and result. |

The repository gate is `.github/workflows/ci.yml`; the local crash lane is
`uv run --locked pytest -n auto tests/crash`.

## Primary-source receipts

Sources were retrieved on 2026-08-10T15:04:02Z. Hashes cover the retrieved raw
bytes so a later review can identify documentation drift.

| Source | Receipt and relevant contract |
| --- | --- |
| [DBOS Python client reference](https://docs.dbos.dev/python/reference/client) | SHA-256 `822c9318674f82c3b6148c21f4b6b264c2a69ffb780075f1b3be2cce6e82bb1f`; `enqueue_in_transaction` uses a caller-owned SQLAlchemy transaction targeting the DBOS system database. |
| [DBOS datasource reference](https://docs.dbos.dev/python/reference/datasources) | SHA-256 `309e687b35abbb86de593d2bb88950acab3486b7a3128a201138524f3439fcf6`; a datasource can use an existing SQLAlchemy engine and installs `datasource_outputs`. |
| [DBOS transaction tutorial](https://docs.dbos.dev/python/tutorials/transaction-tutorial) | SHA-256 `893a51795cf61300da2e47fbdd4d4126660a45ec75e8479707d07edcd0189f0b`; transaction results are recorded atomically and replayed. |
| [Integrating DBOS](https://docs.dbos.dev/python/integrating-dbos) | SHA-256 `f749dd82ab02d940c197321a7e57d704abe21c608d9d88a06bbea111b5615d05`; SQLite is the zero-configuration default while PostgreSQL is the production recommendation. |
| [DBOS 2.29.0 source](https://github.com/dbos-inc/dbos-transact-py/tree/2.29.0) | Tag commit `ab99c997a468e286b2899975ca525eeb05a4d888`; installed package files matched the tagged `_client.py`, `_datasource.py`, `_dbos.py`, and `_sys_db.py` source hashes recorded in the H0 work evidence. |

## Limits and consequences

Production H2 tests replaced the H0 effect, unknown, C1, C2, and concurrency
simulations. The remaining small probe covers only unreplaced C3: durable input
is replayed before following work. It is decision evidence, not a product
feature, UI, deployment, or a claim that GitHub Actions ran.

SQLite remains a V1 single-user choice. Subprocess tests alone wrap DBOS
2.29.0's private `SystemDatabase.record_operation_result` to kill in the
otherwise inaccessible gap after a datasource/product commit and before the
outer ledger write. A signature and named-operation sentinel fail loudly if
that pinned dependency seam drifts. Product code has no crash switch and never
uses the private API.
