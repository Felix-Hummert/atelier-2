# ADR 0013: A bounded `iterate` block repeats a subworkflow until a receipt says green

- Status: ACCEPTED 2026-08-16, structurally SUPERSEDED 2026-08-18 by
  [ADR 0014](0014-in-graph-rounds.md) — the document surface and the boundary
  binding below are implemented and stand; the `iterate` block stays declarable and
  stays unexecutable. What no longer holds is the finding under "A round is a child
  run, so only the child-run binding gains an ordinal": a loop inside one graph is
  not identity-impossible. ADR 0014 gives the node execution identity a round
  dimension and repeats a stretch of one graph, and that is the form the engine
  runs. Read that record before building on this one
- Date: 2026-08-16, written with the three corrections of the independent deputy
  review bound into the record before its first line rather than after it, because
  a record is the one text the next reader does not re-derive; amended 2026-08-16
  while the document surface was being built, with the refusal vocabulary reduced
  from four minted tokens to three — the build showed the fourth was already owned
  (see "Refusal vocabulary")
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1),
  whose "Nodes sind konfigurierbar" and "Nach einem terminalen Node-Receipt startet
  der konfigurierte Folgeknoten automatisch" this record expresses and never
  re-decides
- Decision authority: [Issue #25](https://github.com/FlexOr2/atelier-2/issues/25),
  the plan at
  [comment 5304806862](https://github.com/FlexOr2/atelier-2/issues/25#issuecomment-5304806862)
  and the independent deputy verdict at
  [comment 5304845320](https://github.com/FlexOr2/atelier-2/issues/25#issuecomment-5304845320),
  whose three corrections this record carries
- Depends on: [ADR 0002](0002-exact-yaml-graph.md) (the acyclic graph and the node
  execution identity this record must not weaken),
  [ADR 0006](0006-node-vocabulary.md) (the node contract, the `subworkflow` kind,
  and the passage that hands this author surface to #25),
  [ADR 0008](0008-budget-units.md) (the budget vocabulary this record composes with
  and never duplicates)
- Names, never decides, the owners it borrows from:
  [#16](https://github.com/FlexOr2/atelier-2/issues/16) (the durable failure
  vocabulary), [#57](https://github.com/FlexOr2/atelier-2/issues/57) (the output
  instance evaluator), [#63](https://github.com/FlexOr2/atelier-2/issues/63) (the
  V3 store), [#79](https://github.com/FlexOr2/atelier-2/issues/79) (the admission
  cap), and the future V3 Join/Ready/Scheduler owner ADR 0008 names
- Feeds: [#7](https://github.com/FlexOr2/atelier-2/issues/7) (the conductor, whose
  build-review-fix chain this makes expressible),
  [#8](https://github.com/FlexOr2/atelier-2/issues/8) (the model economy, whose
  cheap-builder / strong-reviewer split materialises here)

## Context

The most common real cycle in this workshop is build → review → fix → re-review
until green. It is the loop the fleet runs by hand every day, and it is the one
shape the format cannot express: ADR 0002's graph is acyclic and fully reachable,
and ADR 0006 closes the question explicitly — "The format expresses no conditional
branching and no loop; per #1 there is no automatic fix-review cycle. **Bounded
iteration is #25's, and this record decides nothing about its author surface.**"

So the surface is unoccupied by decision, not merely unimplemented, and #6 keeps
naming the review loop as a catalog chain regardless. Until this record is
implemented, a review loop in the catalog is honest only as an authored unrolled
chain with a declared depth.

Three properties must survive, because everything durable in this product rests on
them: acyclicity, hash binding, and exactly-once. A loop is where all three are
easiest to lose, and where losing one would not be visible until a run had already
produced receipts.

## Decision

### The construct is a bounded `iterate` block on the existing `subworkflow` node

```yaml
- id: build_review_fix
  type: subworkflow
  depends_on: [plan]
  workflow: {ref: build_review_fix_round, revision: "<workflow revision id>"}
  inputs:
    - name: order
      from: {node: plan, output: brief}
  outputs:
    - name: candidate
      schema: {ref: workspace_candidate, revision: "<schema revision id>"}
  iterate:
    maximum_rounds: 4
    until:
      output: verdict
      schema: {ref: review_verdict, revision: "<schema revision id>"}
    carry:
      - name: previous_verdict
        from_output: verdict
        seed: {node: plan, output: brief}
```

`iterate` is refused on every other node kind. ADR 0006's per-kind field matrix
refuses fields rather than ignoring them, and this field belongs to exactly one
kind.

**No sixth node kind.** A dedicated `iterate` kind would have to rebuild what
`subworkflow` already owns: the one-to-one name-and-schema-revision mapping between
a parent node's inputs/outputs and a child's `graph_input`/`graph_output`, the
child-run re-attachment on restart, the disposition rule, the depth check, and the
`subworkflow_execution` capability. Six kinds, a sixth row in the field matrix, and
a second owner for child-run execution. The repetition would be the defect, not the
saving.

### A round is a child run, so only the child-run binding gains an ordinal

This is the structural finding the whole shape rests on, and it is forced rather
than chosen.

`NodeExecutionId.for_node` binds exactly three things — run id, revision hash, node
id (`src/atelier2/contracts/executions.py`) — and ADR 0002 states it as contract:
"Each node execution identifier binds run, revision, and node." There is no
occurrence dimension. **A loop inside one graph is therefore identity-impossible,
not merely difficult:** the same `node_id` executed a second time in the same run
collides on `NodeExecutionId`, and takes `logical_effect_key_for` and
`node_workflow_id_for` down with it, since both are derived from it alone.

The subworkflow boundary dissolves that at no cost. ADR 0006: "the child is a
durable run of its own, bound one to one to the parent node execution id." A child
run carries its **own** `run_id`, so every `NodeExecutionId` in round K differs from
round K+1 because the first frame element differs. No frame is touched, no landed
hash vector moves, and acyclicity, hash binding and exactly-once survive unchanged.

What gains a counter is therefore only the parent's child-run binding, which ADR
0006 fixes today as one-to-one. This record makes it one-to-N-bounded:

```text
(parent_node_execution_id, round_ordinal)
```

The counter belongs to this construct and lands in the durable V3 form (#63).

**The agent attempt ordinal is untouched, and inside every round it still means
exactly what it means today.** `AGENT_ATTEMPT_ORDINAL = 1` and
`REPLACEMENT_AGENT_ATTEMPT_ORDINAL = 2`
(`src/atelier2/contracts/agent_attempts.py`) are the cancel-and-replacement
vocabulary of **one** node execution — the attempt and at most its one replacement.
Round 7 has its own attempt 1 and at most its own replacement 2.

Three independent reasons keep iteration out of that vocabulary, each sufficient
alone:

1. `AgentAttemptReplacement` has exactly two members, `NONE` and `ONE`. With N
   ordinals its statement — "at most one replacement" — is no longer expressible,
   and the store's rule that a cancellation's current ordinal must be the maximum
   ordinal for that node execution would mean something else.
2. An attempt is by definition the same request again. ADR 0006: "the run id and
   the node id — the same pair under every attempt, so a retry is the same
   operation." A round is the opposite: it reads the previous round's review, and
   under #8 it deliberately runs a different model. That is new work.
3. ADR 0006 forbids the second attempt model outright: "ADR 0001 stays the owner of
   attempt ownership, cancellation and cleanup — V3 adds no second attempt model."

To those the store adds a fourth, mechanical obstacle: `attempt_ordinal IN (1, 2)`
is a `CHECK` constraint inside the fingerprint-locked schema
(`src/atelier2/adapters/dbos/schema.py`), so widening the domain is a schema
cutover, not an edit.

**What is deliberately *not* claimed here:** widening the ordinal's validation
domain would leave every existing attempt id byte-identical. The preimage packs the
ordinal as a fixed-width integer, so the landed vector for ordinal 1 does not move
and its test stays green. The reasons above carry the conclusion without that
claim, and a record must not carry a premise the next reader will not re-check.

### The carry is a runtime binding, never a document edge

Round K+1 sees round K's result through `carry`, which maps a `graph_output` of the
child onto a `graph_input` of the child — one to one, by name and schema revision,
under the rule already landed for the parent-child boundary. `seed` binds round 1,
which has no predecessor.

**This is deliberately not a fifth `InputSource` form.** A form such as
`{from: {previous_round: …}}` would be a document edge pointing at itself; the cycle
checker would have to know it and make an exception for it, and ADR 0002's refusal
of cycles would stop being unconditional. An exception inside an absolute
prohibition costs more than any feature that buys it. The parent graph stays acyclic
with one node, the child document stays acyclic, and the unrolled truth is a chain
of child runs — a DAG in time, which is what "each round materialises as a new
unrolled stage" means.

### The bound is denominated in started rounds

`maximum_rounds` counts started rounds and nothing else. It is **a new unit, and it
belongs to this record** — it is a document-level bound owned by #25, not one of
ADR 0008's admission-cap units.

It is in the same *family* as those units: counted by the atelier itself, carrying
no meter revision, and therefore composable across providers. That family property
is what makes it safe, and the distinction from ADR 0008's list is what keeps a
later reader from treating rounds as an already-blessed cap unit.

**A round start is a child-run start, not a started attempt.** The attempts live in
the agent nodes *inside* a round. When #79 implements the admission cap ADR 0008
denominates — no implementation of it exists today — that cap will bound the loop
through the attempts each round creates, which is a different mechanism from
counting rounds and needs no new unit either.

**Denominating the bound in provider-reported values is refused.** Tokens and
assistant turns carry a meter revision, and ADR 0008 states that raw values from
different meter revisions are never summed, listing "cross-meter sums" among its
stop conditions. A build-review-fix loop under #8 runs a cheap builder and a strong
reviewer in the same loop — different meter revisions by design — so this construct
is precisely where a bound such as "until 200k tokens are spent" would be that
forbidden sum. It is named here so that nobody has to rediscover it.

The existing budget vocabulary is composed with, not duplicated:

- Every node of every round binds its own `budget-revision/v1`, enforced per
  attempt. ADR 0008 is untouched: "An Agent node's budget applies to each of its
  attempts."
- The Retry policy keeps owning how many attempts one node execution may have. This
  construct owns how many rounds may exist. Three levels, three owners: attempt ⊂
  round ⊂ run.
- There is no run budget and no second Budget owner; both are ADR 0008 stop
  conditions.
- Worst-case **display** extends ADR 0008's permitted product — the per-attempt
  bound multiplied by the Retry policy's permitted attempts — by `maximum_rounds`.
  It stays a display, aggregated per meter revision under that record's three-line
  rule, never a gate and never one number.

### Green is two receipt facts, never a self-report

The expensive failure available to this construct is letting an agent judge its own
green: a loop with that property terminates at round 1 on an invented success. #57
carries the live proof that the risk is real — a V2 agent asked for JSON, the
provider answered prose, and the run recorded `AGENT_COMPLETED` and continued.

Green is therefore the conjunction of two facts the core reads:

1. **The round's child run succeeded** — the child run is terminal, every sink holds
   a `SUCCEEDED` receipt, and every `graph_output` was produced. This is not merely
   decided in prose: `decide_parent_disposition`
   (`src/atelier2/contracts/workflow_bindings_v3.py`) is landed as a pure function
   and already refuses on any sink without a succeeded receipt, including a sink
   that sources no output — its docstring names exactly that case.
2. **The output named in `until` exists as a durable artifact and satisfies its
   pinned schema revision** — value bytes and value hash from #63's artifact row,
   evaluated against the bound schema.

**Fact 2 has a named owner, and it is not this record.** #57's first landed head
established that *a published schema is a schema*: `read_schema_document(bytes)` in
`src/atelier2/contracts/schemas_v3.py` checks the schema against the metaschema.
Its second head added the other half of that owner's surface,
`read_instance_document(bytes, schema)`, and its runtime head made it the gate on
an agent's answer: a V3 node's decoded output is read against the schema it pinned
before any success is written. **The evaluator exists, and #295 gave the other
half its writer**: a succeeded V3 agent node keeps its exact output as a
`node-artifact/v3` row at the terminal write. What a round's evaluation still
waits for is the round machinery itself (see Delivery boundaries), and this
record names the one evaluator so that a second is not built here.

Explicitly refused as a green source: an agent output that merely claims completion,
a free-text match, an exit code without a receipt, and any condition over a non-sink
node — ADR 0006 already refuses a `graph_output` that does not source from a sink,
because a value read from the middle of a graph could report finished while work
behind it was still deciding.

**Reaching the bound without green is a failure, not a quiet stop.** Exactly one
terminal receipt at the iteration node names the node, the declared bound and the
last round's disposition. No hang, no retry, no reinterpretation as succeeded.

### Refusal vocabulary

Three parse- and binding-level tokens are minted by this construct, because
document validity is its own:

| Token | Refused when |
| --- | --- |
| `unbounded_iteration` | `iterate` carries no `maximum_rounds`, or a value that is not a canonical positive integer in ADR 0008's range |
| `iteration_green_condition_unprovable` | `until` names an output the child does not declare as a `graph_output`, or under a differing schema revision — including a revision the declaring node itself contradicts |
| `iteration_carry_unbound` | a `carry` without `seed`, declared twice, or whose names miss the child's declared boundary, or whose schema revisions disagree |

**A fourth token is deliberately not minted, and this paragraph is the amendment
that removed it.** An earlier revision of this record named
`iteration_on_non_subworkflow_node` for `iterate` appearing on an `agent`,
`deterministic`, `wait` or `action` node. Building the document surface showed the
refusal was already owned: `_VOCABULARY_FIELDS` is *derived* from the declared
models, so a field belonging to one kind and written on another is refused as
`refused_field`, naming the node and the field — exactly as `budget`,
`retry`, `available_context` and `description` already are. A bespoke token would
have refused one shape two ways and made `iterate` the odd field out. The rule the
record wanted is unchanged and enforced; only the second name for it is gone.

A carry's seed is held to the rules an input is held to — the same owner, not a
second one — so a seed reading an unordered node, an undeclared output or an
undeclared graph input is refused under the tokens that already name those
failures. For the same reason a graph input read only by a seed counts as read.

`maximum_rounds` has no default and no central cap. An absent bound is refused
before storage, in the same spirit ADR 0008 applies to budgets: an absent field is
not silently substituted. An unbounded loop must not be reachable by omission, and a
default is exactly that accident.

**The durable failure token is borrowed, not minted.** #16 owns the durable name for
the exhausted-bound disposition, and this record only states the need — the same
boundary ADR 0008 drew for itself when it wrote "This record mints neither token".

**No fifth token for the executability case.** A bound runtime capability revision
that does not attest bounded iteration refuses the whole run through ADR 0006's
existing `UNAVAILABLE` path, naming the node and the capability. The capability is
`subworkflow_execution`, extended to attest bounded iteration and the maximum proven
rounds — not a second declarer, since ADR 0006 binds "one capability has exactly one
declarer".

## Delivery boundaries

1. **This record:** the author surface, the denomination, the green definition and
   the refusal vocabulary. No runtime, no format change yet, no API change.
2. **Document surface:** `iterate` parses as a closed model, the three tokens bite,
   and the preview names each iteration node with its bound and its green condition.
   No durable write, no provider, no run.
3. **Binding and executability:** the `until` and `carry` schema revisions resolve
   against the child, the capability attestation is extended, and a missing
   attestation refuses the whole run.
4. **Durable rounds:** the `(parent_node_execution_id, round_ordinal)` binding, the
   per-round evaluation and the one terminal receipt. **Not claimable** until two
   owners exist: the V3 Join/Ready/Scheduler owner ADR 0008
   names and explicitly declares that slice unclaimable without, and
   [#16](https://github.com/FlexOr2/atelier-2/issues/16) (the durable failure
   token). [#57](https://github.com/FlexOr2/atelier-2/issues/57)'s instance
   evaluator left this list when it landed and began gating every V3 success, and
   the record family's writer this list first waited on
   ([#63](https://github.com/FlexOr2/atelier-2/issues/63)'s tables) left it with
   [#295](https://github.com/FlexOr2/atelier-2/issues/295): the start persists
   request and package, the terminal write keeps artifact and receipt. This
   boundary carries no size estimate: naming one before those two exist would be
   invention.

## Required proof before implementation

- A document declaring `iterate` without `maximum_rounds` is refused by name before
  it can be stored, published or started; so is a non-canonical, zero or negative
  value.
- A `until` naming an output the child does not declare, or declaring a differing
  schema revision, is refused by name — a loop whose exit condition can never become
  true must not be publishable.
- A `carry` is bound on both sides by name and schema revision, with a seed for the
  first round, or refused by name.
- `iterate` on any other node kind is refused as a parse error, not ignored.
- The preview names every iteration node with its bound and its green condition
  before any start.
- A run whose bound capability revision does not attest bounded iteration is refused
  as a whole, naming the node and the capability, with no partial execution.
- Round K+1 reads round K's declared result, every round is its own child run with
  its own node execution identities, and no landed hash vector changes.
- Reaching the bound without green produces exactly one terminal receipt naming the
  node, the bound and the last round's disposition — no retry, no hang, no
  succeeded reinterpretation.
- Green is never reached on an agent's own claim: a round whose declared output does
  not satisfy its pinned schema revision does not end the loop.

## Consequences

- The workshop's most common real cycle becomes expressible without a cyclic graph,
  and the catalog stops needing an authored unrolled chain to describe it honestly.
- The model economy of #8 gets its home: a builder and a reviewer of different
  strength per round, bounded by a unit that cannot become a cross-meter sum.
- Attempt, round and run keep three separate owners; no second attempt model and no
  second budget owner appear.
- The cycle checker keeps its unconditional refusal, because the loop lives at the
  child-run boundary rather than in a document edge.
- A published document can express a loop the runtime cannot yet run, and the
  preview marks it — the staged-execution cost ADR 0006 already chose.
- Bounded iteration cannot ship before a durable artifact row exists to evaluate,
  which makes #63's open writer a visible dependency rather than a surprise.

## Out of scope and stop conditions

This record does not decide the scheduler, the durable failure token, the V3 store
cutover, the instance evaluator, retry policy, or the admission cap. It changes
neither `node-execution-id/v1`, nor ADR 0002's identity rule, nor the agent attempt
ordinal, nor any V1/V2 wire or hash contract.

Stop implementation on: a bound denominated in provider-reported values or any
cross-meter sum; a default or central cap for `maximum_rounds`; a green condition
that reads an agent's claim; an attempt ordinal widened beyond 1 and 2; a `carry`
expressed as a document edge that weakens the cycle refusal; a sixth node kind; a
second owner for child-run execution or for schema evaluation; or any durable write
before #63.

## Supersedes

None.
