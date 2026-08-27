# ADR 0016: The queue projection owns proposal, admission, and launch identity

- Status: ACCEPTED 2026-08-27 — Phase D1 replaces the direct admission model
  with a durable proposal, its explicit confirmation, and one immutable launch
  binding
- Depends on: [ADR 0007](0007-catalog-identity.md) for catalog lineage identity
- Requirement authority: [Issue #79](https://github.com/FlexOr2/atelier-2/issues/79),
  REQ-QUEUE-01, REQ-QUEUE-05, and REQ-QUEUE-14
- Decision authority: the Phase-D operator rulings of 27.08.2026

## Context

The tracker owns item content and lifecycle facts. Core owns only the durable
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
from the current revision. Proposal revisions and their dependency edges are
append-only, and every edge names the exact proposal revision that declared it.
Dependencies are project-local. A prerequisite is satisfied only when its
launch-bound run is `COMPLETED`; any other run state remains a blocker. Ready
items order by priority rank, then item id, so equal ranks are deterministic.

The projection names blockers with `QueueBlockerKind`, including unset
priority, human confirmation, open or failed prerequisites, capacity,
unresolved bindings, unavailable required orders, start refusal, and legacy
review. These are contract values, not free-form row states.

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

## Consequences

- Proposal and confirmation are independently stale-safe and idempotent.
- The durable binding, rather than a repeated catalog lookup, is the authority
  for which exact workflow revision starts.
- Capacity is not a best-effort count outside the write transaction.
- V43 remains a published predecessor object; V44 is the Phase-D schema.
- Automatic authorization, cross-project dependencies, and tracker write-back
  remain outside D1.
