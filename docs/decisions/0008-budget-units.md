# ADR 0008: A budget bounds machine time, turns and tokens; money only where money flows

- Status: DRAFT — proposed for review, not accepted, not implemented
- Date: 2026-08-14
- Depends on: [ADR 0002](0002-exact-yaml-graph.md), [ADR 0006](0006-node-vocabulary.md)
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1),
  whose "Parallele DAG-Ausführung" (exhausted budget is a terminal failure
  receipt, never silent hanging; parallelism within the configured parallelism
  **and budget** limit) and "Publish und Start sind getrennte Autoritätsgrenzen"
  (a higher budget needs an explicit grant) sentences this record expresses in
  units and never re-decides
- Answers: [#26](https://github.com/FlexOr2/atelier-2/issues/26)
- Feeds: [ADR 0006](0006-node-vocabulary.md), whose `budget: {ref, revision}` is a
  reference with no unit owner until this record lands, and
  [#31](https://github.com/FlexOr2/atelier-2/pull/31), whose `--max-turns 1` is
  named in its own source as a stopgap awaiting this record
- Names, never decides: [#22](https://github.com/FlexOr2/atelier-2/issues/22) /
  [ADR 0007](0007-catalog-identity.md) (revision lineage identity),
  [#24](https://github.com/FlexOr2/atelier-2/issues/24) (platform adapter),
  [#8](https://github.com/FlexOr2/atelier-2/issues/8) (scorecard balance), #1
  story 3 (scheduler and ready set)
- Evidence: documentary. Read at `f2d84f0`: `docs/decisions/0001`–`0007` (0007 as
  PR #45), `src/atelier2/contracts/agents.py` (`AuthMode`, `AgentReceiptV2`,
  `AgentExecutionRequestV2`), `contracts/agent_attempts.py`,
  `contracts/hashing.py` (`frame`, `Sha256Hash`),
  `adapters/agent_processes.py`, `adapters/agent_process_watchdog.py`,
  `application/cancel_agent_attempt.py`, and on PR #31
  `adapters/claude_subscription.py` with `ports/agent_executions.py`. Prior art
  read in Atelier 1: `atelier/engines/_contracts.py` (`TokenUsage`) and
  `atelier/sessions/_engine_usage_totals.py`. No code changed and no repository
  gate was run; nothing below is implemented.

## Context

`budget` is a core object in #1 — the scheduler starts parallel nodes only
within it, and exhausting it must produce a terminal receipt. ADR 0006 pins a
node's budget as a versioned reference and says outright that this record owns
what it means. Nothing owns it today, so every consumer would invent its own
column.

The unit question is not cosmetic, because the deployment bet is a
**subscription**. A subscription call has no price: no dollars flow per call,
the provider's rate limits are opaque and undocumented, and any per-call money
figure would be an estimate the machine then treats as truth. What the runtime
can honestly observe is narrower: how long a process ran, how many turns it was
allowed, and whatever usage the provider itself reported.

Two facts bound the design, and both are measured rather than assumed:

- **No usage is recorded per attempt today.** `AgentReceiptV2` carries identity,
  output bytes and the output hash — no turns, no tokens, no duration. The
  Claude executor decodes exactly `type`, `is_error` and `result` from the
  provider envelope; the `usage` and `modelUsage` fields it measured are seen
  and dropped. So a token ceiling is a receipt change, not a read.
- **No clock bounds an attempt today.** The supervisor owns the *mechanism* — a
  cancellation drives a cgroup kill to exactly one terminal disposition — but
  nothing arms it on time: `AgentProcessRunner`'s wait is issued with
  `timeout_seconds=None`. The wall-clock ceiling is a deadline armed onto a
  cancellation path that already exists and is proven.

Atelier 1 reduced provider reporting to the same honest shape — a
provider-neutral `TokenUsage(input_tokens, output_tokens)`, each optional, and a
session total returning `None` rather than guessing when nothing was measured.
This record generalizes that stance rather than inventing a second one.

## Decision

### A budget is a published revision of ceilings, and its scope is where it is bound

One record type, `budget-revision/v1`, bound at exactly two sites:

- a **node's** `budget` reference (ADR 0006: `agent` and `subworkflow` only)
  bounds **one attempt** of that node;
- the **run configuration revision**'s `run_budget` reference bounds the **whole
  run**, as the sum over its terminal receipts.

There is no scope field inside the record: the binding site is the scope, so one
published ceiling set serves both, and a retry is bounded per attempt while the
run total still counts every attempt.

### The exact fields

| field | | unit |
| --- | --- | --- |
| `budget_id` | mandatory | the lineage name the reference's `ref` resolves (identity owned by #22 / ADR 0007) |
| `revision_number` | mandatory | positive signed int64, as `auth_profile_revisions` already numbers a revision |
| `wall_clock_seconds` | mandatory | integer ≥ 1, machine time bought |
| `maximum_turns` | optional | integer ≥ 1, provider-reported assistant turns |
| `maximum_input_tokens` | optional | integer ≥ 1, from the provider's own usage report |
| `maximum_output_tokens` | optional | integer ≥ 1, from the provider's own usage report |
| `cost_ceiling` | optional | `(amount_micros: integer ≥ 1, currency: ISO 4217 code)` |

**Wall-clock is the only mandatory dimension**, because it is the only one
enforceable with no cooperation from the provider: the runtime owns the clock
and the kill. Every other dimension depends on what a provider declares or
reports, and a budget whose bounds all rest on provider goodwill bounds nothing.

**An absent optional dimension means not budgeted — never estimated, never
defaulted.** A publish omitting `maximum_output_tokens` states that output
tokens are unbounded here; it acquires no house default and no consumer may
infer one. That is Atelier 1's `None`-means-unmeasured rule on the ceiling side.

**`cost_ceiling` is refused unless every auth profile it can bind is
`AuthMode.API_KEY`.** Under `AuthMode.SUBSCRIPTION` no money flows per call, so
a money ceiling could only be an invented price; declaring one is a named
refusal at publish and again at start, not an ignored field. Money is an
*additional* dimension, never a substitute for the three above: an API-key run
still carries the same wall-clock, turn and token ceilings.

### Identity

A budget revision is identified by SHA-256 over the repository's existing
framing, `atelier2.contracts.hashing.frame` — no second encoding:

```text
frame("budget-revision/v1",
      budget_id            utf-8 bytes,
      revision_number      >Q,
      wall_clock_seconds   >Q,
      maximum_turns        >Q or the zero-length field when absent,
      maximum_input_tokens >Q or the zero-length field when absent,
      maximum_output_tokens>Q or the zero-length field when absent,
      cost_ceiling         frame("budget-cost-ceiling/v1",
                                 amount_micros >Q, currency ascii)
                           or the zero-length field when absent)
```

Per ADR 0006's rule, an absent optional value is the zero-length field in its
declared position, so absence never shifts the frame and a nested record is one
field carrying its own framed domain. This hash is the revision id a
`{ref, revision}` reference names.

### Enforcement, and where each dimension is honestly enforceable

**1. Before anything starts.** At publish and again at start, a node's bound
ceiling is compared to the run budget dimension by dimension. Where both declare
a dimension, the node's value must not exceed the run's; a dimension the run
leaves unbudgeted bounds nothing, so any node value passes it. A node demanding
more than the run allows is refused before a single process exists, and the
refusal names the dimension, both revision ids and both values. `cost_ceiling`
against a subscription profile is refused at the same boundary. Per #1 a higher
budget is an authority change: it is a new published revision, not an edit.

**2. Wall-clock — by the supervisor, in real time.** The ceiling is armed as a
deadline at launch authorization and, on expiry, drives the cancellation path
that already exists, so an exhausted attempt reaches exactly one terminal
disposition through the proven route rather than a second one.

**3. Turns — by a provider flag where the executor declares one, otherwise
post-hoc.** The Claude subscription executor's `--max-turns` is the measured
example, and its source already names `--max-turns 1` a stopgap awaiting this
record. An executor declares whether it can carry a turn ceiling; where it
cannot, turns are measured from the receipt like tokens.

**4. Tokens and cost — post-hoc, from receipts.** No provider offers the runtime
a live meter, so **a single attempt may overshoot its token or cost ceiling
before its receipt lands.** That is stated rather than engineered around: no
pretend-realtime metering, no polling estimate, no mid-flight extrapolation. The
overshoot is recorded in the receipt, the ceiling is judged when that receipt is
written, and the consequence falls on what happens next — this node's retry, and
every node the scheduler has not yet started.

**5. Exhaustion is a typed terminal receipt.** Per #1 an exhausted budget is a
terminal failure receipt, never silent hanging: ADR 0006's `failed` disposition
with a typed reason naming the dimension, the ceiling, the measured value and
the scope exceeded. A `blocked` receipt spends no budget, as 0006 already says.

**Consequence, named:** this requires the node receipt to carry the measured
consumption — duration, and each provider-reported dimension as an optional
value distinguishing unreported from zero. That is a field addition to the
`node-receipt/v3` envelope ADR 0006 already introduces, and it lands with that
record's store cutover rather than around it.

### Aggregation, and the scheduler's denominator

Run consumption is the sum over the run's terminal receipts, dimension by
dimension. **Run wall-clock is summed attempt duration, not elapsed run time**,
for two deliberate reasons: parallel work must not look more expensive than the
same work run serially, and a `wait` node blocked on a human must not spend a
budget nothing is buying. An elapsed-time deadline is a schedule, not a budget,
and this record does not own one.

Remaining budget is the run ceiling minus that sum, and it is what #1's
parallelism sentence reads. A ready node is admitted only if remaining allows
it: where the node declares a ceiling in a dimension the run bounds, remaining
must cover that ceiling; where it declares none, remaining in that dimension
must be positive. Effective parallelism is therefore the smaller of the
configured parallelism limit and the number of ready nodes that fit — the
denominator #1 asks for. When remaining cannot admit a ready node, the ready set
does not silently stop shrinking: that node receives its budget-exhausted
terminal receipt.

**The honest residue:** because tokens and cost are post-hoc, a run can exceed
its ceiling by the overshoot of the attempts in flight when it was crossed —
admission is exact, consumption is measured one receipt late. That is the price
of refusing a live meter, and the configured parallelism bounds it.

## Consequences

- ADR 0006's `budget` reference resolves to a real published object with units,
  so the V3 vocabulary stops naming an owner that does not exist.
- The node receipt grows measured consumption, and the executor seam grows a
  provider-neutral usage report the Claude executor can fill from the envelope
  fields it already sees and drops.
- A subscription deployment is fully budgetable without a single invented price;
  an API-key deployment adds money as one more dimension and loses nothing.
- Mandatory wall-clock makes every budgeted node killable on time, which is what
  turns "exhausted budget is terminal, never hanging" into an enforceable rule.
- The post-hoc token ceiling is weaker than an operator may expect, and is
  stated here so no consumer builds on a precision that does not exist.

## Required proofs before acceptance

This record is a draft; nothing below exists yet.

- A budget revision hashes over the exact framed preimage above, and a
  differing, re-ordered or absent-versus-zero dimension yields a different id.
- A node ceiling exceeding the run ceiling in any dimension is refused before a
  process is created, and the refusal names the dimension.
- A `cost_ceiling` bound to a subscription auth profile is refused by name.
- An attempt exceeding its wall-clock ceiling reaches exactly one terminal
  receipt through the existing cancellation path, with the typed reason.
- A receipt whose measured tokens exceed the ceiling is written with its true
  measurement, and the next admission decision refuses on it.
- Remaining run budget bounds admission: with a run ceiling that admits one of
  two ready nodes, exactly one starts and the other takes a terminal
  budget-exhausted receipt.
- A run's consumption equals the sum over its receipts, and parallel execution
  does not change that sum.

## Out of scope

- **Pricing tables and any token-to-money conversion.** No owner today; if ever
  needed it belongs to a published pricing registry revision alongside the
  platform adapter (#24), never to this record and never derived in a receipt.
- **Provider rate-limit modeling.** Subscription limits are opaque, so a
  throttled or refused call is a provider failure under the retry policy, not a
  budget dimension.
- **Multi-run, project and monthly quotas.** A budget here bounds one attempt
  and one run; aggregation across runs is #8's balance question or a
  project-level owner, and neither is decided here.
- **Deterministic node bounds.** ADR 0006 refuses `budget` on `deterministic`;
  its wall-clock belongs to the operation revision, which knows its own cost.
- **The scheduler itself** (#1 story 3). This record gives it a denominator and
  an admission rule; it does not decide its ready-set machinery.

## Supersedes

None.
