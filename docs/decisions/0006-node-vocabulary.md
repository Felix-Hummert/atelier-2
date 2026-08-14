# ADR 0006: Format V3 owns node prompts, context edges, and handoffs

- Status: DRAFT — proposed for review, not accepted, not implemented
- Date: 2026-08-14
- Depends on: [ADR 0002](0002-exact-yaml-graph.md), [ADR 0001](0001-durable-runtime.md)
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
- Feeds: [#6](https://github.com/FlexOr2/atelier-2/issues/6) (catalog),
  [#7](https://github.com/FlexOr2/atelier-2/issues/7) (Dirigent)

## Context

An Agent node today carries a `role` and a one-line `job` string, and V1's Agent
carries an exact expected output. Nothing else about the work is expressible: no
authored instruction text, no statement of which earlier result a node may read,
no typed result, and no statement of where a finished result lands. The single
Action node performs one hardcoded effect.

That vocabulary cannot express any chain a real story needs. #6 asks for a
catalog of named, versioned chains — planning, breakdown, implementation, review
loop — and names the missing substrate exactly: node prompts, typed context
edges, and output/handoff adapters, present in no schema and no decision record.
#7's Dirigent authors revisions from a chat; it can only author what the format
can express. Until this vocabulary exists, every catalog entry would be a
renamed toy chain.

Issue #1 has already decided the semantics on 2026-08-11/12: the versioned
workflow document decides per node what context is relevant and how data flows;
the core adds no ambient project context; the canonical node call distinguishes
required context, available context, explicit upstream inputs, named typed
hashed outputs, and provider-neutral instructions/capabilities/policy; a
successor sees only explicitly mapped outputs; a summary does not replace its
sources; an output that violates its declared schema is a terminal node failure,
never a silent artifact; irreversible external mutation runs exclusively through
declared Action nodes with intent, readback, and receipt. #9 Rev. 4 has decided
that execution mode is a capability declaration: headless is mandatory for every
provider, interactive is declared, and a node demanding interactive on a
non-declaring configuration fails at validation, never silently.

This record does not re-decide any of that. It decides the smallest **document
surface** that expresses those decisions, as one new closed format version.

## Decision

Format version `3` is a new closed contract parsed by the existing safe-YAML
adapter, not an edit of V2. Every guarantee of [ADR 0002](0002-exact-yaml-graph.md)
survives unchanged: exact UTF-8 bytes identify the revision by SHA-256, the
strict frozen model is validated before any run or revision is written, unknown
fields and node kinds, duplicate keys, missing references, cycles, unreachable
nodes, multiple documents, BOMs, anchors, aliases, merges, and explicit tags are
refused, and the `start`/`next` edges own execution order while list order means
nothing.

V3 keeps ADR 0002's single-successor chain. Fan-out, fan-in, and the ready-set
scheduler are Story 3 and are not decided here; a data edge that skips over
intermediate nodes is the part of the DAG that this format can already carry.

### The Agent node

```yaml
- id: implement
  type: agent
  role: builder
  mode: headless
  prompt: |
    authored instruction text
  required_context: [...]
  inputs: [...]
  outputs: [...]
  next: review
```

`prompt` replaces `job` and is authored by the operator or by an authoring agent.
It is instruction, never context and never a secret: it is part of the exact
document bytes, so it is inside the revision hash, immutable for a started run,
and visible in the publish preview. Its bound is 16 KiB of UTF-8; an empty or
oversized prompt is refused. Context belongs in `required_context` and `inputs`
so that it is revision-bound, hashed, and provenance-carrying — a prompt that
pastes requirement text instead is legal YAML and a review finding, not a format
error.

`mode` is `headless` (the default when omitted) or `interactive`. Per #9 Rev. 4
the node declares the requirement and the bound agent configuration declares the
capability; they are compared at run start. An `interactive` node either declares
no outputs, or declares every output `confirmed_by: operator`; an interactive
output mapped downstream without that confirmation is refused.

`role` keeps its V2 meaning exactly: the portable document names the logical
role, and the run-start command binds every graph role to one immutable
agent-configuration revision and no others.

### Context edges

Two reference kinds, one binding model. Both are immutable, hashed, and fully
materialized before START; per #1 they differ only in provenance.

```yaml
required_context:
  - name: story
    source: {kind: requirement, selector: story_acceptance}
  - name: graph_contract
    source: {kind: decision_record, selector: "0002"}
inputs:
  - name: candidate
    from: {node: implement, output: candidate}
```

`required_context` names resolver-materialized sources. V3 closes the source
kinds at `requirement` (a section of the bound HumanRequirement revision) and
`decision_record` (one record by number). Both selectors are exact; an unknown
kind is refused at parse, and a source that cannot be materialized and hashed
means the node does not START (#1). Until Atelier owns its resolver (#1 story 5),
the external bootstrap harness materializes exactly this package — #1 already
binds it to that contract.

`inputs` name an exact output of an exact earlier node. A reference must resolve
to a node that is strictly upstream on the chain and to an output that node
declares; anything else is refused at parse. Inputs may skip intermediate nodes,
which is what makes a fix node expressible: it reads the original candidate and
the review findings, and the reviews are not merged into a shared chat.

There is no ambient context. A node sees its prompt, its required context, its
inputs, and its capabilities. Conversation history and agent working memory are
not passed to a successor.

`available_context` — #1's on-demand, receipt-generating grant — is deliberately
**not** in V3. It is meaningless without the privileged resolver and the
`ContextAccessReceipt` that make a grant enforceable, and a declared grant nobody
enforces is a lie in the publish preview. The resolver story owns adding it as
the next format version.

### Outputs

```yaml
outputs:
  - name: findings
    kind: verdict
```

An output is named, typed, and hashed. V3 closes the kinds at three:

- `text` — bounded UTF-8 bytes; the existing V2 output bound applies.
- `verdict` — a closed decision token (`pass` / `fail`) plus bounded text. This
  is the machine-readable shape a review must produce, and it is the type the
  bounded-iteration join in #25 will read. #25 owns that construct; V3 only
  makes its input expressible.
- `workspace_candidate` — the commit and tree the attempt produced in its
  isolated workspace; #1's "Candidate T".

Output bytes that do not satisfy the declared kind are a terminal node failure,
never a silent artifact. A node declaring `workspace_candidate` requires an
executor that declares the isolated-workspace capability; where that is not
provable the node is UNAVAILABLE at run start, fail-closed like #1's auth modes.

### Handoff adapters

Every landing of a result outside the run's own hashed outputs is an effect, so
every handoff is an Action node with a **declared adapter reference** — never
free code, and never a side channel on an Agent node. V3 replaces the hardcoded
Action with:

```yaml
- id: publish_report
  type: action
  adapter: github.issue_comment
  target: {kind: requirement_issue}
  inputs:
    - name: body
      from: {node: behaviour_test, output: report}
  next: null
```

The first adapter registry is closed and holds three entries:

| Adapter | Effect class | Lands |
| --- | --- | --- |
| `artifact.markdown` | internal | the named output as a markdown artifact in the run's durable artifact store |
| `github.issue_comment` | external | one comment on the bound requirement issue |
| `github.pull_request_comment` | external | one comment on the bound pull request |

An unknown adapter id is refused at parse with the id named. A known adapter
that the deployment has not granted refuses the run at start, fail-closed and
named; it is never silently skipped. External adapters carry #1's intent,
readback, and receipt contract and stay idempotent under their `logical_key`, so
a restart cannot duplicate a comment. `target` stays deliberately thin — the
GitHub platform adapter and its full addressing are #24, not this record.

V3 keeps ADR 0002's limit of at most one Action, immediately preceded by exactly
one Agent, because the reconciliation proof covers exactly that. An Action may be
the terminal node.

### Terminal and retired keys

Exactly one V3 node carries `next: null` and is the terminal; the toy
`subworkflow` adder is not part of V3. The terminal hash keeps covering the
ordered event hashes, unchanged.

A V3 document that carries `job`, `output`, `subworkflow`, or `available_context`
is refused with the retired or deferred key named, so an author who copied a V2
example learns what replaced it instead of getting a generic closed-schema error.

### What binds a node call

The revision hash already covers prompt bytes. The agent execution request must
additionally bind, as `agent-execution-request/v3`: the mode, the ordered
materialized required-context hashes, the ordered input output-hashes, and the
declared output names and kinds. A changed context is then a different logical
operation rather than a silent re-run of the same one, and a retry with identical
inputs stays the same operation. The receipt correspondingly binds an ordered
tuple of `(name, kind, hash)` instead of one anonymous output blob
(`agent-receipt/v3`); that receipt change is the largest implementation cost of
this record and is named, not hidden.

## Worked example: implement → review → fix → behaviour-test, on Atelier 2 itself

One real chain for one story of this repository. It is written to double as the
first seed of the #6 catalog. Prompts are illustrative authored text; the shape
is the decision.

```yaml
format_version: 3
start: implement
nodes:
  - id: implement
    type: agent
    role: builder
    prompt: |
      Implement every literal acceptance sentence of the bound story inside your
      workspace. Change nothing outside it: do not push, merge, comment, or
      deploy. Return the candidate you produced and a short summary naming which
      sentence each change serves.
    required_context:
      - name: story
        source: {kind: requirement, selector: story_acceptance}
      - name: graph_contract
        source: {kind: decision_record, selector: "0002"}
    outputs:
      - name: candidate
        kind: workspace_candidate
      - name: summary
        kind: text
    next: review

  - id: review
    type: agent
    role: reviewer
    prompt: |
      Judge the candidate against the acceptance sentences and the named decision
      records. Read only what you were given. Return pass only if every sentence
      has a proof in the candidate; otherwise return fail and name each defect
      with the file and the sentence it violates.
    required_context:
      - name: story
        source: {kind: requirement, selector: story_acceptance}
      - name: graph_contract
        source: {kind: decision_record, selector: "0002"}
    inputs:
      - name: candidate
        from: {node: implement, output: candidate}
      - name: summary
        from: {node: implement, output: summary}
    outputs:
      - name: findings
        kind: verdict
    next: fix

  - id: fix
    type: agent
    role: builder
    prompt: |
      You receive the original candidate and the review findings. If the findings
      are a pass, return the candidate unchanged. Otherwise resolve every named
      defect and return the new candidate. Do not reopen a decision the story
      already made.
    required_context:
      - name: story
        source: {kind: requirement, selector: story_acceptance}
    inputs:
      - name: candidate
        from: {node: implement, output: candidate}
      - name: findings
        from: {node: review, output: findings}
    outputs:
      - name: candidate
        kind: workspace_candidate
    next: behaviour_test

  - id: behaviour_test
    type: agent
    role: tester
    prompt: |
      Run the repository gate against the candidate you were given and report the
      exact commands and their exact counts. Report each acceptance sentence with
      the test that proves it, and name any sentence no test proves.
    required_context:
      - name: story
        source: {kind: requirement, selector: story_acceptance}
    inputs:
      - name: candidate
        from: {node: fix, output: candidate}
    outputs:
      - name: report
        kind: text
    next: publish_report

  - id: publish_report
    type: action
    adapter: github.issue_comment
    target: {kind: requirement_issue}
    inputs:
      - name: body
        from: {node: behaviour_test, output: report}
    next: null
```

Three things this example makes visible.

The `fix` node reads the *original* candidate and the hashed findings, not a
merged conversation — the shape #1's fan-in section requires, already expressible
in a chain.

The `fix` stage runs even when the review passed, and its prompt has to say so.
That is the honest cost of an acyclic chain without #25's bounded
repeat-until-green construct: the loop is expressed as one explicitly unrolled
stage with a declared depth of one. #25 owns replacing it; V3 must not invent a
second loop semantics in the meantime.

Only one landing exists, because V3 keeps the single-Action limit. Publishing
the review findings as a second comment needs the effect contract proven for
several actions per run.

Per #6 Rev. 2, publishing this or any successor revision into the catalog carries
the publish gate: the authoring agent names the nearest existing catalog entry
and why it does not suffice, and the operator sees that justification in the
existing publish preview.

## Explicitly out of scope

This record decides a document surface and its refusals. It decides nothing
about: the engine or executor implementation; the DAG scheduler, ready set, join
conditions, and parallelism (#1 story 3); catalog identity, naming, lineage, and
storage (#22); the bounded iteration construct (#25); the platform adapter's
addressing and authorization (#24); budget semantics (#26); interactive attach,
transcripts, and remote runners (#9 parts 2 and 3); and the privileged context
resolver with its access receipts (#1 story 5).

## Migration

V1 and V2 documents stay valid and keep their meaning. The parser dispatches on
`format_version` into a separate closed model, so V3 adds a version instead of
widening a frozen one, and no stored revision is ever reparsed under a new
meaning. A started run keeps executing under the version it bound. There is no
runtime document migration and none is needed, which matters because
[ADR 0001](0001-durable-runtime.md) knows no runtime migration path.

## Consequences

- A catalog entry can express a real chain: authored prompts, who sees what, and
  where a result lands — the substrate gap #6 names is closed at the document
  level.
- The Agent receipt and the agent execution request grow a version to carry
  named typed outputs and bound context; that is a durable-contract change with
  crash evidence, not a field addition.
- The runtime's terminal handling is bound to the Subworkflow node kind today and
  must move to `next: null` before V3 can execute.
- Every landing keeps exactly one receipt path, because handoffs are Actions.
  The price is that a chain wanting two landings is not yet expressible.
- The format still cannot express branching, loops, several Actions, fan-out, or
  fan-in. V3 is deliberately not a general graph language.
- An author who writes V2 habits into a V3 document gets a refusal naming the
  retired key.

## Required proofs before acceptance

This record is a draft; nothing below exists yet.

- A V3 document parses to a closed frozen model, and each refusal above is
  proven by its own behavioral case: retired key, unknown source kind, unknown
  output kind, unknown adapter id, external adapter without a grant,
  non-upstream input reference, undeclared output reference, duplicate input or
  output name, oversized or empty prompt, interactive output mapped downstream
  without operator confirmation, missing or multiple terminal.
- The worked example above parses, publishes, and round-trips byte-identically.
- V1 and V2 example documents from the existing suites still parse unchanged.
- The request and receipt hash vectors are literal and pinned, and a changed
  context hash produces a different request identity while an identical retry
  does not.

## Open questions for review

These are deliberately unanswered; each changes the surface above.

- **Deferred `available_context`.** Is one format version per capability right,
  or should V3 already carry the surface so #7's Dirigent authors one stable
  shape and the resolver only starts enforcing it later?
- **Inline prompts.** A catalog will want the same reviewer prompt in ten
  chains. Inline text duplicates it in every revision; a referenced prompt
  artifact splits one revision's identity across two hashed objects. V3 chooses
  inline for one document, one hash — the catalog record (#22) may need the
  other answer.
- **`workspace_candidate`.** It is the only output kind today's executor cannot
  produce. Naming it now makes the worked example honest; leaving it out would
  keep the format strictly implementable. Which cost is preferred?
- **Ordering against #25.** Should V3 land before the bounded-iteration
  construct, accepting the unconditional unrolled fix stage as visible debt, or
  wait so the first catalog chain is not born with a wart?
- **Terminal change.** Does anything durable — terminal hash, run projection,
  API, cockpit — depend on the terminal node being a Subworkflow beyond the
  runtime branch named above?
- **`mode` default.** Headless by omission keeps documents quiet; always-explicit
  makes every publish preview state the mode without knowing a default.
- **One Action per run.** A review chain wants a findings comment *and* a report
  comment. What proof does lifting ADR 0002's limit require?
- **Receipt shape.** One receipt binding an ordered `(name, kind, hash)` tuple,
  or one receipt per declared output?

## Supersedes

None. This record extends [ADR 0002](0002-exact-yaml-graph.md), which remains
the owner of the document, graph, identity, and transition contracts for every
format version.
