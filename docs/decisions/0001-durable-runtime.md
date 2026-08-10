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
first vertical slice. Product code will not import DBOS outside that adapter.
The adapter will own the DBOS runtime, the canonical SQLAlchemy/SQLite
datasource, `RunStore`, and `DurableRunStarter` so one caller transaction can
persist the run and enqueue its DBOS workflow.

One canonical SQLite engine and file will contain Atelier product tables, DBOS
system tables, and `datasource_outputs`. An external service may of course have
its own store; the probe's loopback-effect database represents that independent
system and is not a second Atelier store.

Atelier product rows are cockpit truth. DBOS `operation_outputs` and
`workflow_status` are a recoverable executor ledger, so they may lag a committed
datasource transaction without making the cockpit lie. Atelier's immutable
`WorkflowRevisionHash` is a product identity and remains distinct from DBOS
`application_version`, which fences executor compatibility.

Before an external call, Atelier durably records an effect intent bound to the
logical key, request hash, workflow revision, and adapter revision. Recovery
must read back the external outcome. It may execute only after authoritative
absence; an unknown outcome becomes durable `WAITING_RECONCILIATION`, never a
blind retry. H1 must add the operator path that resolves that state.

## Executable evidence

The parameter-driven integration probe runs each obligation in an isolated
temporary workspace and removes its database, WAL, crash-marker, and backup
artifacts afterwards.

| Criterion | What the probe establishes |
| --- | --- |
| `canonical_sqlite` | Atelier product state, DBOS system state, and datasource records share one SQLite file and engine. |
| `atomic_start` | A hard exit before commit leaves neither the run nor the queued workflow; a committed caller transaction leaves both. |
| `datasource_recovery` | Product state and `datasource_outputs` commit together and survive a kill before the outer DBOS ledger records completion. |
| `version_fence` | An executor with a different DBOS application version does not adopt the unfinished run; the matching executor resumes it. |
| `effect_reconciliation` | Prepared intent, typed authoritative readback, receipt provenance, changed request/revision rejection, and unknown-outcome waiting prevent blind replay. |
| `concurrent_recovery` | Competing recovery processes converge without duplicating the logical effect. |
| `crash_boundaries` | Real subprocess kills at C1, C2, and C3 recover to the same final hash as an uninterrupted run. |

Verification on 2026-08-10:

```text
uv run --locked pytest -n auto tests/integration/test_durable_runtime_probe.py -q
7 passed in 10.57s
```

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

This is a runtime decision probe, not product runtime, UI, deployment, or a
claim that GitHub Actions ran. SQLite remains a V1 single-user choice. The
backup smoke exercised by the probe is not H1's complete migration, downgrade,
or operational recovery proof. The probe uses a private DBOS system-database
method only to inject a kill in the otherwise inaccessible interval after a
datasource commit and before the outer ledger write; product code must never
use that private API.

H1 must implement the cohesive adapter boundary and failing behavioral tests
against production ports. Those tests replace the simulated product schema,
workflow, and effect code in the 850-line spike. Once S1-S6 are proven through
the product boundary, delete the spike and its integration test, retaining at
most a small shared crash-injection harness if production crash tests still need
it. Growth beyond that replacement is a failed deletion ledger.
