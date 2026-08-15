# ADR 0008: Node budgets separate hard limits from reported thresholds

- Status: PROPOSED 2026-08-15 — decision only, not implemented
- Date: 2026-08-15
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
- Decision authority: [Issue #26](https://github.com/FlexOr2/atelier-2/issues/26),
  exact body SHA-256
  `69a3f021453bf3b7ae70ca37abce8fe7aa3b47a7133a0bd61bcc9d3b021ed9ba`
- Depends on: [ADR 0001](0001-durable-runtime.md),
  [ADR 0002](0002-exact-yaml-graph.md), and
  [ADR 0006](0006-node-vocabulary.md)
- Feeds: [#60](https://github.com/FlexOr2/atelier-2/issues/60),
  [#63](https://github.com/FlexOr2/atelier-2/issues/63), and the future V3
  Join/Ready/Scheduler owner

## Context

ADR 0006 lets an Agent or Subworkflow node pin a versioned `budget` reference,
but deliberately leaves its units to #26. Without one owner, a caller could
present a provider-reported token count as a limit even though it learns that
count only after the paid work, or infer money from an authentication mode that
reports no charge.

The runtime needs two different concepts: bounds it can enforce while an attempt
runs, and measurements it can judge only after the provider reports them. Their
names and failure behavior must make that difference impossible to hide.

## Decision

### Revision content and scope

`budget-revision/v1` is immutable content identified through the existing
`contracts.hashing.frame` owner. Its first executable form contains:

| Field | Presence | Meaning |
| --- | --- | --- |
| `attempt_deadline_seconds` | required | hard elapsed deadline for each attempt |
| `maximum_assistant_turns` | optional | hard assistant-turn limit, when the executor attests its native enforcement and meter |
| `reported_input_token_threshold` | optional | threshold evaluated from the completed attempt's provider report |
| `reported_output_token_threshold` | optional | threshold evaluated from the completed attempt's provider report |

Every present field is a strict integer scalar in the inclusive range
`1..9223372036854775807` (positive signed 64-bit). Booleans, strings, floats,
zero, negative values, fractions, and overflow are refused before publication.
An optional field is either present with such a value or absent; explicit
`null` is refused. A present value uses the repository's existing fixed-width
eight-byte big-endian integer encoding in its declared frame position, while an
absent optional uses the zero-length field in that same position.

Changing any content bytes changes the revision and every request that binds it.
Catalog lineage, display name, and revision position remain metadata owned by
[#22](https://github.com/FlexOr2/atelier-2/issues/22); none enters the immutable
content hash.

An Agent node's budget applies to each of its attempts. Its independently
versioned, finite Retry policy owns how many attempts may exist; Budget does not
duplicate that decision. The product may show the resulting worst-case hard Node
bound as the per-attempt bound multiplied by the Retry policy's permitted
attempts.

A Subworkflow budget may be published, but remains `UNAVAILABLE` until the V3
Join/Ready/Scheduler owner implements the composition rules below. There is no
separate run budget in this decision.

### Hard limits and reported thresholds

A **hard limit** is enforced before or during an attempt:

- `attempt_deadline_seconds` starts at the authoritative process-owner launch.
  Expiry enters ADR 0001's bounded process-tree termination path and ends in one
  terminal Budget-failure receipt.
- `maximum_assistant_turns` is hard only when the selected executor revision
  attests both the exact native limiter and its matching meter. The executor
  passes the value to that limiter before launch.

If a selected executor cannot enforce an authored hard dimension, run start
refuses before process creation. It never silently downgrades the dimension to a
post-hoc check or clamps it to an executor default.

A **reported threshold** is evaluated only after an attempt supplies its usage
report. The attempt may exceed it. The receipt stores the actual larger value,
the Node ends as `failed`, and no retry follows. UI, APIs, receipts, and logs call
this a `reported threshold`, never a maximum, cap, or ceiling.

If an otherwise successful provider response omits or corrupts a measurement
required by a bound threshold, the Node ends in exactly one typed
provider-contract failure. If a failed attempt has unknown required usage, the
runtime invents no value and does not retry. Reaching a hard limit or exhausting
a Node budget likewise produces exactly one terminal failure receipt, never a
hang.

Publishing a Budget revision validates its own content. Provider, executor,
model, and meter compatibility are known only when the workflow binding is
resolved at run start, so that boundary owns compatibility refusal. Authentication
mode is not a Budget-publication predicate.

### Meter identity and composition

Every reported measurement binds:

```text
(dimension, meter_revision_id, value)
```

The referenced meter revision binds the provider, executor revision, selected
model, and exact mapping from native provider fields. Values are non-negative
signed 64-bit integers; an absent value is not zero.

- `assistant_turns` is the native count of completed assistant turns.
- `input_tokens` names an explicit mapping of every native input category;
  cache-read and cache-creation values are neither silently omitted nor counted
  twice.
- `output_tokens` is the native output-token count.

Claude's first meter revision pins the measured JSON mapping of CLI version
2.1.221. Codex receives a distinct meter revision only after its native stream
mapping has been measured.

Raw values from different meter revisions are never summed. When a composed
scope carries a numerical threshold, every descendant it covers must resolve to
the same meter revision or the runtime refuses before execution. Slice 3 owns
that Subworkflow and parallel composition behavior.

`attempt_deadline_seconds` is runtime control, not provider usage. It is not
summed as tokens, turns, or money. Actual duration is telemetry and never an
invented Budget-consumption value.

### Money is absent

`budget-revision/v1` contains no money field. `AuthMode.API_KEY` selects a
credential path; it does not create a charge meter. A later revision may add
currency only after an exact provider charge or billing-ledger owner supplies
the measurement. Token-to-money estimates never enter a receipt or gate work.

### Temporary foundation frame for #60

After this decision is accepted, #60 may run its one no-retry Foundation canary
under immutable executor revision `claude-subscription-tools-foundation/v1` with:

- `--max-turns 8`;
- an authoritative process deadline of 300 seconds.

This is a visible, non-configurable executor safety frame, not a
`budget-revision/v1`, and proves no V3 Budget acceptance. A workflow cannot raise
either value; changing one requires a new executor revision. Reaching either
bound projects through the existing bounded V2 failure contract without adding
a V2 workflow, request, or receipt field.

The bridge is deleted when the public V3 Claude tool worker proves the same
bounded canary and no V2 attempt depends on this executor revision. The product
must never retain two selectable Budget paths.

## Delivery boundaries

1. **This ADR:** decision and #60 bridge authorization only; no runtime or API
   change.
2. **First budgeted V3 Agent:** after full #15 cutover, the V3 binding owners,
   and an explicitly amended and reviewed #63. It resolves one Budget revision,
   binds executor meter attestations in the immutable request, persists ordered
   per-attempt usage plus aggregate atomically in `node-receipt/v3`, and enforces
   hard limits and reported thresholds. V1/V2 wire and hash contracts remain
   unchanged.
3. **Retry, Subworkflow, and parallel composition:** the future V3
   Join/Ready/Scheduler owner consumes ordered per-attempt usage, composes only
   identical meters, reserves enforceable descendant hard limits, drains running
   siblings, emits one terminal receipt per affected Node, and reconstructs the
   same reservation, usage, and ready set after restart. #57 does not own this
   behavior. This slice is not claimable until that scheduler owner exists.

## Required proof for implementation

- A changed Budget byte changes both Budget-revision and bound request hashes;
  existing V1/V2 hash vectors remain unchanged.
- An unsupported hard meter refuses before any process or provider effect.
- Deadline races before and after a deterministic barrier converge on exactly
  one terminal result.
- A threshold overshoot persists the true larger value, fails the Node, and
  prevents retry.
- Missing or malformed required usage terminates without an invented number.
- A crash around attempt-usage and receipt persistence writes either nothing or
  the complete atomic set.
- Unlike meter revisions are never added or silently treated as one unit.

## Consequences

- Subscription and API-key execution share one truthful Budget vocabulary.
- A provider can implement a hard turn limit only by attesting a native limiter;
  post-hoc counters remain useful without being misrepresented.
- Retry count, catalog identity, V3 storage, and scheduling keep their existing
  owners instead of acquiring competing Budget implementations.
- General tool-workflow availability waits for V3 Budget enforcement even though
  the narrowly fixed #60 Foundation canary may run earlier.

## Out of scope and stop conditions

This record does not decide pricing, project/monthly quotas, Retry policy,
Scheduler implementation, durable failure tokens, or provider-specific auth.
Stop implementation on V2 widening, a second catalog/framing/scheduler owner,
money without an exact charge meter, `maximum` wording for post-hoc values,
cross-meter sums, invented usage, missing per-attempt persistence, or a
Foundation frame a workflow can raise.

## Supersedes

None.
