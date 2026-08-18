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
start/advance/answer/reconcile implementations, graph-event and effect ledgers,
and node continuations so each caller decision and its DBOS enqueue share one
transaction.

One canonical SQLite engine and file contains Atelier product tables, DBOS
system tables, and `datasource_outputs`. The persistent loopback adapter uses a
separately configured SQLite file as its external destination; it is not a
second Atelier store.

The runtime creates the current schema only in a truly empty canonical store and
reopens only that exact product schema. V9 through V18 remain published
predecessor objects (`V9_SCHEMA_HANDOFF` through `V18_SCHEMA_HANDOFF`) and are
not opened or migrated by runtime. An exact V13 store or later advances to the
current schema only through the offline `atelier2 migrate` command, one published
step at a time; older published predecessors stay refused by name. Older, future,
malformed, or nonempty unowned stores are rejected without mutation. There is no
runtime downgrade. The published `PRODUCT_SCHEMA_HANDOFF` is version 19 with
product-schema fingerprint
`1d787bf0f9b6de10e79d48911e8c964e0f0dac5d0b6ff17248f21de62dd865ab`.

Atelier product rows are cockpit truth. DBOS `operation_outputs` and
`workflow_status` are a recoverable executor ledger, so they may lag a committed
datasource transaction without making the cockpit lie. Atelier's immutable
`WorkflowRevisionHash` is a product identity and remains distinct from DBOS
`application_version`, which fences executor compatibility.

An Agent node delegates its exact request through an injected, provider-neutral
executor port. Its successful result commits one immutable `AgentReceipt`, the
existing `AGENT_COMPLETED` event, and the configured successor transition in one
canonical transaction. The first executor implements the format-version-1 exact
output contract; provider attempts, streams, failures, and cancellation remain
outside this slice.

Format-version-2 Agent nodes name roles rather than providers. Start resolves
every role to immutable, secret-free auth-profile and agent-configuration
revisions, rejects an incomplete matrix or unavailable executor key before any
run mutation, and persists the sorted complete matrix with the run. A host owns
an immutable registry keyed by provider ID and executor revision. Configuration
revision format V1 retains the original hash frame and is restricted to
`headless`; format V2 adds a typed requested capability to its hash frame. New
API publications are V2 and carry the capability the caller requested, which
defaults to `headless` when the request omits it; migrated rows remain
V1/headless. Every registry entry must attest headless and may attest
interactive. Start refuses an unattested requested capability before
run/enqueue mutation or provider process; restart refuses a nonterminal durable
capability mismatch before factory open.
Each V2 Agent request and receipt binds the role, configuration and auth hashes and fields,
executor operational identity, job, and exact result bytes. At most 49,152
output bytes are accepted; an oversized result is rejected before receipt,
event, or run mutation. The receipt, `AGENT_COMPLETED` event, and run CAS share
one canonical transaction.

Every format-version-2 external invocation first persists one exact ordinal-1
attempt. Preparation, launch claim, and finalization use live canonical database
transactions rather than memoized DBOS step results. Only the call whose compare
and-set changes `PREPARED` to `LAUNCH_ARMED` may invoke the executor. Recovery may
claim `PREPARED` again, but it projects unresolved `LAUNCH_ARMED` as
`POSSIBLY_RAN` and does not invoke. The attempt binds the request hash and bounded,
non-secret executor operational identity; that identity is an integrity input,
not process authority. Success atomically commits attempt, receipt, event, and
successor. A typed, authoritatively reaped unsuccessful child atomically commits
`FAILED`, `AGENT_FAILED`, and the same current run node. Ambiguous exceptions
stay `LAUNCH_ARMED` until an exact cancellation owns their cleanup, and a serve
start is what issues it where no live workflow is left to: a workflow that ended
without moving the attempt it drove is not pending, so recovery replays nothing,
and the restart stops each such attempt under one durable command. Schema V8
already makes cancellation durable before any signal; V9 is the version handoff
of that same product shape. A live supervisor binds a Unix
control endpoint, watchdog generation, and delegated cgroup; an exec guard joins
the provider child to that cgroup and dies if the watchdog parent disappears.
The cancellation workflow sends `TERM`, waits the configured finite grace,
escalates to `KILL`, and reaps before it records `CANCELLED`. After an owner
process dies, recovery stores `OWNER_NOT_LOCAL` and uses the exact empty
cgroup—not a persisted PID or invocation—to attest `INTERRUPTED`; a cgroup the
host already removed holds no process either and attests the same, because
requiring the directory left exactly the restarted host unable to converge.
Only an explicit `ONE` policy creates one distinct ordinal-2 `PREPARED`
attempt and workflow after cleanup; ordinal 3, automatic retry, and provider-session resume
are absent.

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
published-start, answer, and reconcile ports; the run workflow prepares each
graph action inside its own transaction step. The adapter owns the canonical
engine, schema, durable codecs and transactions, explicit application version,
and the node, agent-result, effect, reconciliation, answer, and continuation
workflows. DBOS and SQLAlchemy do not cross that boundary. Workflow-revision
hashes remain
product identity, while the configured DBOS application version remains the
executor recovery fence. [ADR 0002](0002-exact-yaml-graph.md) owns the workflow
document and graph semantics above this execution boundary.

A process owns exactly one compatible DBOS binding of canonical database path,
application version, the V1 `AgentExecutorBinding`, the sorted V2 executor
manifest, and the effect-adapter binding. Restart refuses a registry missing a
provider/executor key or requested capability required by a nonterminal durable
V2 run, configuration
contradicting durable V1 Agent receipts, or an effect binding contradicting
durable intents. Identical callers share one opened V1 executor, every V2
executor, effect adapter, and runtime under counted leases; an incompatible
lease is refused before either executable boundary opens or global state
mutates. V2 factories open in manifest order and close in reverse order. Only
the last release destroys DBOS, closes all resources, and disposes the engine,
each exactly once; partial open, registration, and cleanup failures release the
process owner and preserve the initiating failure. H2 has one concrete
file-backed effect adapter, so its resolved operational identity is also checked
against the canonical file identity, including hardlink aliases, before either
store is opened. This is a bounded loopback invariant rather than a generic
provider contract.

## Executable evidence

| Production proof | What it establishes |
| --- | --- |
| Atomic start, advance, and answer | Run/bootstrap enqueue, the prepared effect intent, and exact Wait answer/enqueue each commit or roll back together; exact retries do not enqueue again. |
| Agent result | Pinned request and receipt vectors bind the invocation, executor, and exact output; receipt, `AGENT_COMPLETED`, and successor commit together, exact recovery remains singular, and binding drift is refused before executor open. |
| Bootstrap recovery | A matching application version fills the outer DBOS ledger after a datasource commit without changing or regressing the product run. |
| Effect recovery | Real subprocess kills after recorded observation (C1), after external commit (C2), and after product confirmation converge with one external call, one receipt, and the configured Wait successor. |
| Unknown outcome | A committed unknown remains waiting across restart and provider-state change; no effect occurs until an operator command owns the intent. |
| Reconciliation | FOUND and authorized-absence commands preserve operator provenance; concurrent opposing commands commit one CAS winner and one rejected loser. |
| Atomic product events | Reconciliation state and its required/resolved event, plus receipt, intent, run, and owning command, commit or roll back together under injected database failures. |
| Runtime lifecycle | Equivalent leases share one engine, Agent executor, and effect adapter; conflicts, failed initialization, concurrent close, durable binding drift, and two-process recovery preserve one binding and result. |
| V7→V8→V9 migration | Populated V7 rows and legacy hashes survive one transactional table rebuild, then the V8→V9 version CAS; fresh, reopened, concurrent, malformed, SQL-CHECK, and injected rollback cases establish exact V9 or the unchanged predecessor. |
| V8→V9 handoff | A populated exact V8 store advances only the schema-version row; every product row and hash survives, including failed and cancelled process evidence; crash after CAS SQL and before commit restores exact V8; concurrent openers converge on one V9. |
| V10 thin store | A fresh store is exact V10 with published revisions of the closed kind set, lineage membership bound to those revisions, format-3 runs, and v3 receipts; invented kinds and unpublished member hashes are refused; an injected write failpoint rolls the thin set back; V7/V8/V9 files are refused unmutated. There is no runtime compatibility after the published V9 predecessor. |
| V11 supervised V3 start | A fresh exact V11 store adds immutable node artifact bytes and ordered receipt output/access bindings. An admitted lineage member resolves to the exact published bytes; missing founding, unpublished member, and wrong kind are refused; name lookup reports missing because alias and retirement tables are not in this profile. The start-time writer this row once described fell with the #216 seam cut (`6f0e316`); the family's production writer is the V17 row's. V10 remains unchanged and is refused without mutation. |
| V12 named catalog | A fresh exact V12 store adds append-only alias and retirement histories. One typed founder and admission writer derives `CatalogLineageId` from kind and founding hash and refuses a mismatched id before mutation. `resolve_name` returns the current display name and retirement flag through membership, or the typed missing/retired refusals. A 64-hex query is a lineage id. V11 remains unchanged and is refused without mutation. |
| V13 V3 record preimages | A fresh exact V13 store gives the declared context package (`context-package-declared/v3`, the half a document and its frozen configuration can produce today), the `node-execution-request/v3` preimage and the run configuration snapshot durable, immutable homes, and **every** format-3 run records the configuration revision it was started under. The public start binds that snapshot and, since the V17 row's writer, persists each node's request and declared package inside the run's own start transaction; identical records are shared between runs rather than conflicted on. Foreign keys bind a run to its configuration, and a composite key binds a receipt to the request of its own node execution -- the pair, because each hash alone can name a record that exists while the two together describe an execution nobody ran -- so no row can name a record that does not exist, and an injected failure at any one of those writes leaves none of them. V12 remains unchanged and is refused without mutation. |
| V14 run orders | A fresh exact V14 store gives the order a run was started with a durable, immutable home: name, the schema revision it satisfies and its exact bytes, keyed one order to one name per run, with update and delete refused by trigger. The start resolves the `graph_inputs` schema the document pinned, refuses a missing, undeclared, twice-supplied, wrongly-pinned or schema-violating order before any row exists, and a repeat of the same run id with a different order is an identity conflict rather than the run that already exists. V13 remains unchanged and is refused without mutation. |
| V15 redeemed tool grants | A fresh exact V15 store gives one redeemed tool grant a durable, immutable home: the node execution and attempt that redeemed it, the published grant revision, the capability it granted, the exact command, its exit code and the hash of what it wrote, with update and delete refused by trigger. It is written inside the transaction that makes the attempt succeed, so a succeeded attempt and the proof of what its tool ran are durable together or not at all. V14 remains unchanged and is refused without mutation. |
| V16 receipt in the chain | A fresh exact V16 store gives an `AGENT_COMPLETED` event the receipt hash its preimage binds: `node-event-hash/v3` is chosen by content whenever that field is set, so an event without a binding keeps its frozen V1 or V2 hash byte for byte. The store admits the field only on a completion, mirroring the contract's rule, and a finished run's terminal hash recomputes from presented receipt and event fields alone -- under any other provider, profile, model, executor revision or request hash it misses. V15 remains unchanged and is refused without mutation. |
| V17 named refusal and the record family's writer | A fresh exact V17 store admits `OUTPUT_SCHEMA_REFUSED` beside the process-exit code, and the family writes: the public start persists `node-execution-request/v3` and `context-package/v3` per node -- an order the run carries binds in as a material member under its content hash -- and the terminal write ends the execution in the same transaction as the agent receipt: `failed` carrying the schema owner's reason on a refusal and the supervision's exit signature with a bounded standard-error tail on a dead process, `succeeded` with `node-artifact/v3` and its output binding on success; a judged receipt of either ending also names, on the same `node-receipt/v3` family, the schema revision and the hash of the exact decoded bytes it judged (older plain-reason rows stay readable); a crash inside that write leaves none of them, and a run from before the writer stays honestly receipt-less. A populated exact V16 store migrates through one transactional `agent_attempts` rebuild that keeps every attempt row and the child `tool_redemptions` declaration; a parked-name collision refuses by name and rolls back every earlier step; V16 joins the refused predecessors at runtime. |
| V18 a run may end failed | A fresh exact V18 store admits `FAILED` as a run state, so a line whose open node paths have terminally failed ends under the node's own reason instead of standing STARTED with nothing to continue it. A populated exact V17 store migrates through one transactional `runs` rebuild that keeps every stored run; V17 joins the refused predecessors at runtime. |
| V19 the round a loop turns | A fresh exact V19 store gives the round a durable home on the run and on every event it writes, keys a `node-execution-request/v3` by the execution rather than by the request it repeats, and drops the agent receipt key that said one receipt per node per run -- a sentence that stopped being true when a declared loop could run a node twice. Every round of every looped node is therefore its own request, receipt, artifact, agent receipt and durable workflow, and the first round of a node keeps byte for byte the identity it had before any loop existed. A populated exact V18 store migrates through four transactional table rebuilds that keep every stored row and read each as round one; a parked-name collision refuses by name and rolls back every earlier step; V18 joins the refused predecessors at runtime. |
| V2 provider-neutral Agent | Two test provider factories execute their exact role/configuration bindings across restart; fixed hash vectors, atomic size-bound completion, unavailable-factory refusal, and a real process kill after Agent commit preserve one receipt, one event, the original binding, and one successor. |
| V2 attempt boundary | A real controlled process proves pre-arm reclaim versus post-arm non-replay; concurrent claimers invoke once; terminal failpoints roll back; exact query reconstruction detects forged attempt bindings; public failure state remains bounded and secret-free. |
| V2 cancellation and replacement | Real subprocesses prove natural exit, TERM, KILL escalation, reaping, parent-death cgroup recovery with and without a surviving witness, durable redrive, exact HTTP retry semantics, and one distinct ordinal-2 replacement with no ordinal 3. |
| Driver-loss convergence | A killed driver leaves an armed attempt no workflow owes a move to; the next serve start stops it under one durable command and ends it `INTERRUPTED`, with the cancellation and interruption events carrying that command as the readable reason. |

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

Production crash tests replace the H0 effect, unknown, C1, C2, C3, and
concurrency simulations, so the exploratory probe is no longer retained.

Until a named maturity, the product does not promise store compatibility.
[#16 comment 5307892458](https://github.com/FlexOr2/atelier-2/issues/16#issuecomment-5307892458)
rules that preserving hops, compatibility layers, and keeping old store shapes
openable are unnecessary while the store is a prototype. Runtime still refuses
every predecessor. The offline migrate command is the one exception: an exact
V13 store or later is raised to the current schema, preserving product rows,
because every step is additive in meaning: `run_inputs_v3` was empty in V13,
`tool_redemptions` in V14, V16's `run_events.agent_receipt_hash` is NULL for
every event a V15 store wrote, and V19's rounds are one for every run and event
written before a document could declare a loop -- a fact about those rows, not a
default filled in to make a column fit. Each such step rebuilds its table rather
than appending a column, because SQLite can only append behind a table's
constraints while the shape a store is checked against is the one the declaration
renders; every predecessor row is copied verbatim into the rebuilt table.

SQLite remains a V1 single-user choice. Subprocess tests alone wrap DBOS
2.29.0's private `SystemDatabase.record_operation_result` to kill in the
otherwise inaccessible gap after a datasource/product commit and before the
outer ledger write. A signature and named-operation sentinel fail loudly if
that pinned dependency seam drifts. Product code has no crash switch and never
uses the private API.
