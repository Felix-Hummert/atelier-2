# ADR 0016: The queue projection owns one item's derived identity and its CAS-guarded admission

- Status: ACCEPTED 2026-08-23 — a derived item identity, the OBSERVED-to-ADMITTED
  transition, its typed refusals, and the durable V29 row are implemented; every
  later capability this record names is explicitly not built
- Depends on: [ADR 0007](0007-catalog-identity.md) (the named workflow lineage a
  queue admission binds to)
- Requirement authority: [Issue #79](https://github.com/FlexOr2/atelier-2/issues/79),
  REQ-QUEUE-14 ("kein Tracker-Nachbau; Items leben im angebundenen Tracker")
- Decision authority: the operator's two-claim-invariant clarification of 23.08.
  on #79 ("Item-Claim ... und Write-Set-Ausschluss ... sind getrennte
  Invarianten"), and the Desk plan-measurement precision of 23.08. that named this
  slice ("die typisierte Admission allein")

## Context

The atelier's Core/Tracker boundary was sharpened on 21.08.: the tracker owns
CRUD, status, comments, and parent; Core owns a versioned, idempotent, durable
orchestration projection over what the tracker reports. This record is the first
slice of that projection -- narrow on purpose. A dependency graph, readiness, and
priority are later slices; this one proves only that one observed item can be
identified, and admitted into the queue once, durably.

REQ-QUEUE-14 draws the line this record must not cross: the tracker (GitHub
first, others behind the same door) is the source of truth for the item itself.
Core never reconstructs a title, a description, or a comment thread. What Core
owns is orchestration state keyed by a reference into that tracker -- workflow
binding, admission, and later the graph and its claims.

## Decision

### Identity is derived, never accepted

A `WorkItemReference` pairs a project id (the existing host-configuration
`ProjectId`) with an opaque `TrackerItemReference` -- a string whose meaning is
the connected platform adapter's contract (ADR 0010), never reinterpreted here.
The pair's `QueueItemId` is a SHA-256 digest framed from both fields, exactly as
`CatalogLineageId` is framed from a lineage's kind and founding revision (ADR
0007). No constructor accepts an identity; every reader recomputes it from the
fields a durable row actually carries and refuses a row whose stored id
disagrees. Two references naming the same project and the same tracker item are
therefore the same queue row by construction -- REQ-QUEUE-14's dedupe half of
"a work item ... is deduplicated ... exactly once" holds by derivation, not by a
lookup that could disagree with itself.

### The lifecycle this slice proves is two states and one transition

`QueueItemState` is `OBSERVED` or `ADMITTED`. `QueueItemSnapshot.admit` is the
one pure, CAS-guarded transition: it takes an `AdmitQueueItem` command carrying
the revision the caller inspected, and returns one of four typed outcomes --
`QueueItemAdmitted`, `QueueAdmissionAlreadyCurrent` (an idempotent repeat of the
exact same admission), `QueueAdmissionRevisionConflict` (the caller inspected a
revision the item has since moved past), or `QueueAdmissionAlreadyDecided` (the
item already carries a different admission). The pattern mirrors
`EffectIntent.resolve_reconciliation` for the pure CAS decision and the
`catalog_v3` admission family for typed, exhaustive refusals rather than a
caught exception -- a caller must read what happened, not guess from a raised
message.

### An admission names a workflow lineage, never a fixed revision

`QueueAdmission` carries a `CatalogLineageId` and a durable
`QueueAdmissionRationale`. Binding to a lineage rather than one revision lets
the workflow a lineage names keep publishing later members without re-admitting
every already-queued item -- the same reasoning that lets a catalog name resolve
to `head`. Resolving *which* lineage a workflow query names is the
application layer's job (`admit_queue_item`), through the existing
`CatalogResolver`; the projection itself accepts only an already-resolved
`CatalogLineageId` and never interprets a query.

### The store: one table, a derived-identity CAS row, immutable history

`queue_items` follows the `effect_intents` shape: identity columns
(`item_id`, `project_id`, `tracker_item_reference`) that a trigger refuses to
update, `state` and `state_version` that a CAS `UPDATE ... WHERE
state_version = :expected` may advance, and a CHECK binding `state = 'ADMITTED'`
to its required payload (`workflow_lineage_id`, `admission_rationale`) exactly
as `state = 'RECONCILING'` binds to its owner command. No row is ever deleted.
A caller's first admission request for one derived identity also establishes
that identity's row, `OBSERVED` at revision 0 -- there is no separate durable
"observed" write in this slice, because nothing yet reads an item before its
first admission attempt.

### Core mirrors tracker facts; it never writes them back

Nothing in this table is authored here. `project_id` and
`tracker_item_reference` are read verbatim from wherever the caller resolved
them (a future ingestion slice behind ADR 0010); the workflow binding and its
rationale are Core's own orchestration decision. No field in `queue_items`
holds a title, a description, or a comment -- REQ-QUEUE-14's other half. Nothing
here writes back to the tracker; that remains the platform adapter's authorized-
action contract (Koordinationsakzeptanz Regel 6).

## The two-claim-invariant separation

The operator's 23.08. clarification names two invariants this record must not
conflate: **item-claim** (dedupe -- one item, one run) and **write-set
exclusion** (no two concurrent runs on the same surface). Item-claim is what
`QueueItemId`'s derivation and the admission CAS give this slice. Write-set
exclusion is explicitly **not** this record's subject: it hangs off a run, not
an item, so an item-less mutating run can declare a write-set and share the
same exclusion table a claimed run would use, while a read-only run holds no
write-set at all. This record's admission row carries no write-set field and
makes no claim about one. Generalizing a write-set beyond repository files to
an external surface (the same pull request, ADR 0010's receipt world) is a
named, later edge.

## Named and not built

- **Observation as its own durable write.** This slice's `OBSERVED` row is
  established only as a side effect of the first admission attempt. A real
  ingestion pipeline that observes a tracker item before anyone tries to admit
  it, and writes that fact durably on its own, is later work behind ADR 0010.
- **Dependency edges, readiness, and priority.** REQ-QUEUE-06's cross-project DAG,
  Kahn-ordered readiness, and weighted priority are the next slice's subject and
  name no type here.
- **The HTTP door.** No route projects this table yet; it follows the #566 wire
  collision this record deliberately stays behind.
- **Write-set exclusion.** Named above -- a distinct invariant with its own
  later table, not a field squeezed into this one.

## Consequences

- `queue_items` is schema V29; the migration is a pure additive table hop
  (`_added_table_step`), so every store below V28 gains the table with zero
  rows and no reinterpretation of anything it already held.
- A queue item's identity can never drift from its project and tracker
  reference: nothing durable stores an identity the fields it was built from do
  not reproduce.
- An admission is either accepted once, repeated for free, or refused by name
  with the row provably unchanged -- there is no path that leaves a queue item
  partially admitted.
