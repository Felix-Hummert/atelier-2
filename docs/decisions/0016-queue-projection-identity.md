# ADR 0016: The queue projection owns proposal, admission, and launch identity

- Status: ACCEPTED 2026-08-27 — Phase D1 replaces the direct admission model
  with a durable proposal, its explicit confirmation, and one immutable launch
  binding
- Depends on: [ADR 0007](0007-catalog-identity.md) for catalog lineage identity
- Requirement authority: [Issue #79](https://github.com/FlexOr2/atelier-2/issues/79),
  REQ-QUEUE-01, REQ-QUEUE-05, and REQ-QUEUE-14
- Decision authority: the Phase-D operator rulings of 27.08.2026

## Context

The tracker owns item content and lifecycle facts, and core never becomes their
owner: of them it holds at most a dated observation. Core owns the durable
orchestration decision keyed by a project and opaque tracker reference. The
earlier queue row jumped directly from `OBSERVED` to `ADMITTED`, named no
priority or prerequisites, held no project capacity policy, and resolved the
catalog head again after every restart. That made an admission impossible to
inspect as a separate decision and allowed a moved head to select different
workflow bytes before a delayed launch.

The live predecessor is published schema V43. Its fingerprint is
`f7d299ab865b87ca47a399d4897f8c7b273085c4d206fac9eb882d47198b9782` and is
immutable; Phase D therefore occupies the next free hop, V44.

## Decision

### One derived item, three durable states

`WorkItemReference(project, tracker_item_reference)` still derives one
`QueueItemId`; callers never supply it. `QueueItemState` is now `OBSERVED`,
`PROPOSED`, or `ADMITTED`. A proposal is a separate CAS transition and carries
one typed `QueuePriorityRank` (`{"rank": n}` on the wire), a catalog workflow
lineage, project-local prerequisite item ids, an automation disposition, and
the policy revision it was evaluated against. Duplicate prerequisites are
canonicalized by item id.

`POST /queue-admissions` is the confirmation door. It confirms the exact
proposal revision the operator inspected, records `QueueDecisionAuthority`,
and never accepts a replacement workflow. D1 admits manually with `OPERATOR`;
`AUTOMATION_RULE` is a typed authority for the later automation slice, not an
implicit permission.

### Priority, dependencies, and capacity have one owner

Each project has append-only queue-policy revisions. The active-run cap is read
from the current revision. A project with no published policy revision has no
cap: launch reservation skips the capacity check rather than refusing the
reservation, because absence of a revision is a legitimate project state, not
corrupt durable state (operator ruling 28.08.2026). A published revision still
holds `maximum_active_runs` as `NOT NULL`; there is no sentinel or nullable cap
column for "unlimited" — the absence of a revision is what expresses it.

Proposal revisions and their dependency edges are append-only, and every edge
names the exact proposal revision that declared it. Dependencies are
project-local. A prerequisite is satisfied only when its launch-bound run is
`COMPLETED`; any other run state remains a blocker. Ready items order by
priority rank, then item id, so equal ranks are deterministic.

The projection names blockers with `QueueBlockerKind`. `GET /queue-items`
reports only facts the durable read can prove without making a reservation or
starting a run: unset priority, human confirmation, open or failed
prerequisites, and legacy review. An empty blocker list therefore means no
read-time blocker was proved; it is not a start-readiness claim. Capacity,
binding resolution, required-order availability, and start refusal are checked
at advance or reservation time and returned by that decision. All blocker
names are contract values, not free-form row states.

### Launch reservation is the exactly-once boundary

An admitted proposal may receive one immutable `QueueLaunchBinding` from
item/proposal revision to one `RunId` and one exact `WorkflowRevisionHash`.
Capacity inspection and insertion of that binding happen in one
`BEGIN IMMEDIATE` decision. The run id derives from item id and proposal
revision. A crash after reservation reuses the recorded binding; a crash after
run creation finds the same run. Once bound, a later catalog-head movement is
irrelevant.

Project model defaults and agent configuration revisions are resolved by the
canonical run starter after reservation. The queue does not duplicate
`AgentConfigurationRevisionHash` or `ModelResolutionSource`; the run binding
owners record those exact values.

### V43→V44 preserves decisions and invents none

V44 appends policy, proposal, dependency, and launch-binding tables and adds
the proposal pointer and decision authority to the current queue row. The
migration preserves every V43 queue row and leaves all new decision columns
and tables empty. In particular it invents no priority, authorization,
dependency, policy, or run binding. A pre-V44 admitted row cannot prove which
workflow revision ran, so the typed projection retains it and reports
`LEGACY_REVIEW_REQUIRED`; the advancer does not spend it again.

All new history tables reject updates and deletes. Runtime refuses a V43 store;
only the offline migration command performs the atomic hop. A collision or
failpoint rolls the complete hop back to the exact predecessor.

### One projection serves every state

`GET /atelier/api/v1/queue-items` is the only queue read. Every row carries its
typed state, revision, optional proposal, admission, launch binding, and
blockers. The former observed/admitted split reads are removed. Tracker display
enrichment is explicitly separate: if it cannot be read, the durable row still
appears with `tracker_enrichment: ENRICHMENT_UNAVAILABLE` and no invented title.

**2026-09-01 amendment (Operator-Ruling of 01.09.2026,
[#962](https://github.com/FlexOr2/atelier-2/issues/962), over the ruled lines 21
and 22 of [Issue #79](https://github.com/FlexOr2/atelier-2/issues/79)): the
projection may hold a last-observed title, and closedness is derived at
import.** Two facts a queue row could not carry may now be written,
both at import time and both as observations rather than as core truth.

**A title is an observation of a fact the tracker owns.** The projection may hold
the tracker title as it was last observed, together with the marker of when it
was observed. The tracker stays the owner; the core records only what it saw and
when, and never invents a title. A row with no observation keeps
`tracker_enrichment: ENRICHMENT_UNAVAILABLE` — the paragraph above is extended,
not replaced. A stale observation is shown as the observation it is, carrying its
marker, rather than silently refreshed behind the reader.

Import is the honest place for it because the answer is already paid for: the
adapter's open-item listing serves title and state on every entry and discards
both at the boundary, keeping the item number alone. Deriving the title at read
time would instead cost one uncached per-item request per board load, because
that adapter deliberately builds its client without an HTTP cache.

**Closedness arises by set difference at import.** An item missing from a new
open-items answer has left the pullable set, and the import records that
retirement; today the import only inserts and never retires, so no durable row
states the fact. The port keeps answering references only — the tracker is not
asked for lifecycle, and `open_items()` is unchanged in this respect.

This supersedes the Phase-D sentence in #79's body that tracker enrichment is
never written into the queue store. The newer operator line governs.

Carrying the title inside the pinned `ObservedWorkItemRevision` is refused: those
bytes are the item body alone, the queue never takes such a snapshot, and adding
a field would invalidate every workflow document pinned against the current
`WORK_ITEM_ORDER_SCHEMA_REVISION`.

Both facts are durable state, so recording them requires its own published schema
hop — the title, its observation marker, and the retirement fact — serial on the
one live store like every other hop. Its predecessor is whichever published
version is live when it is cut, which the schema shapes owner names; the V43→V44
passage above records what this record decided and is not that floor. This
amendment authorizes the hop; it does not design it.

**2026-09-02 amendment (Operator-Ruling, #962-Journal, over ruled line 1 of
[#962](https://github.com/FlexOr2/atelier-2/issues/962)): `QueueItemResource`
serves the persisted observation and retirement, closing the gap the paragraph
above opened between persisted state and the wire.** `title`, `title_observed_at`,
and `retired_at` are required, nullable fields on `GET /queue-items`'s row — read
straight from `QueueItemSnapshot.observation` and `.retired_at`, no filter, no
re-derivation. `tracker_enrichment` stays `ENRICHMENT_UNAVAILABLE` regardless: a
last-observed title is not enrichment succeeding, it is the import-time
observation this record already ruled durable. A retired row is still served —
the projection remains the operator's full view — but the start sheet that reads
this projection does not offer it as pullable, since retirement means the item
left the tracker's open set.

## Consequences

- Proposal and confirmation are independently stale-safe and idempotent.
- The durable binding, rather than a repeated catalog lookup, is the authority
  for which exact workflow revision starts.
- Capacity is not a best-effort count outside the write transaction.
- A project with no published policy revision has no cap, not a corrupt state
  (operator ruling 28.08.2026).
- V43 remains a published predecessor object; V44 is the Phase-D schema.
- Automatic authorization, cross-project dependencies, and tracker write-back
  remain outside D1.
