# ADR 0008: A budget bounds counted work before dispatch; measured usage is recorded, never estimated

- Status: DRAFT — proposed for review, not accepted, not implemented. Revision 2:
  it answers the Codex REVISE of `9184b2b`
  ([#26 comment](https://github.com/FlexOr2/atelier-2/issues/26#issuecomment-5299480673))
  and the finding the first productive atelier-2 run left on this pull request
  ([PR #48 comment](https://github.com/FlexOr2/atelier-2/pull/48#issuecomment-5299246107))
- Date: 2026-08-15
- Depends on: [ADR 0002](0002-exact-yaml-graph.md),
  [ADR 0006](0006-node-vocabulary.md) (ACCEPTED)
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1),
  whose "Parallele DAG-Ausführung" (exhausted budget is a terminal failure
  receipt, never silent hanging; parallelism within the configured parallelism
  **and budget** limit) and "Publish und Start sind getrennte Autoritätsgrenzen"
  (a higher budget needs an explicit grant) sentences this record expresses in
  units and never re-decides
- Answers: [#26](https://github.com/FlexOr2/atelier-2/issues/26)
- Feeds: [ADR 0006](0006-node-vocabulary.md), whose `budget: {ref, revision}` is a
  reference with no unit owner until this record lands;
  [#60](https://github.com/FlexOr2/atelier-2/issues/60), which derives its bound
  tool loop, its terminal exhaustion and its attested absolute ceiling from here;
  and [#31](https://github.com/FlexOr2/atelier-2/pull/31), whose landed
  `--max-turns 1` names itself a stopgap awaiting this record
- Names, never decides: [#22](https://github.com/FlexOr2/atelier-2/issues/22) /
  ADR 0007 (revision lineage identity),
  [#24](https://github.com/FlexOr2/atelier-2/issues/24) (platform adapter),
  [#8](https://github.com/FlexOr2/atelier-2/issues/8) (scorecard balance),
  [#16](https://github.com/FlexOr2/atelier-2/issues/16) (the durable schema
  version and the durable failure vocabulary),
  [#63](https://github.com/FlexOr2/atelier-2/issues/63) (the V3 store cutover and
  `node-receipt/v3`), [#15](https://github.com/FlexOr2/atelier-2/issues/15)
  (attempt lifecycle and cancel convergence), #1 story 3 (scheduler and ready set)
- Evidence: documentary. Read at `f45596c`: `docs/decisions/0001`–`0006`,
  `src/atelier2/contracts/agents.py` (`AuthMode`, `AgentExecutionCapability`,
  `AgentReceiptV2`, `AgentExecutionRequestV2`), `contracts/agent_attempts.py`,
  `contracts/hashing.py` (`frame`, `Sha256Hash`),
  `ports/agent_executions.py` (`AgentExecutorManifestEntry`,
  `AgentProcessInvocation`, `AgentProcessCompletion`),
  `adapters/agent_processes.py`, `adapters/agent_process_watchdog.py`,
  `adapters/claude_subscription.py`, `application/cancel_agent_attempt.py` and
  `tests/integration/test_claude_subscription.py`. Prior art read in Atelier 1:
  `atelier/engines/_contracts.py` (`TokenUsage`) and
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
can honestly observe is narrower: how long a process ran, how many attempts it
opened, how many turns it authorized, and whatever usage the provider itself
reported afterwards.

Three facts bound the design, and all three are measured rather than assumed:

- **No usage is recorded per attempt today.** `AgentReceiptV2` carries identity,
  output bytes and the output hash — no turns, no tokens, no duration. The landed
  Claude executor decodes exactly `type`, `is_error` and `result` from the
  provider envelope; that same envelope carries `num_turns` and a `usage` object,
  both read by the landed containment test and both dropped by the executor. So a
  token or turn measurement is a receipt change, not a read.
- **No clock bounds an attempt today.** The supervisor owns the *mechanism* — a
  cancellation drives a cgroup kill to exactly one terminal disposition — but
  nothing arms it on time: `AgentProcessRunner`'s wait is issued with
  `timeout_seconds=None`. A wall-clock bound is a deadline armed onto a
  cancellation path that already exists and is proven.
- **An executor already declares what it can do, in a typed seam.**
  `AgentExecutorManifestEntry.declared_capabilities` is refused unless every
  member is the typed `AgentExecutionCapability`. That domain is *execution mode*
  and stays exactly `headless | interactive` (ADR 0006, #60). A budget meter is
  not an execution mode, so what this record needs is a **second declaration on
  that same manifest entry**, never a new member of that enum.

Atelier 1 reduced provider reporting to the same honest shape — a
provider-neutral `TokenUsage(input_tokens, output_tokens)`, each optional, and a
session total returning `None` rather than guessing when nothing was measured.
This record generalizes that stance rather than inventing a second one.

## Decision

### Two kinds of quantity, and only one of them may gate

Every budget dimension belongs to exactly one **enforcement class**, and the
class is part of the contract, not a comment:

| class | what it means | dimensions |
| --- | --- | --- |
| `counted_before_dispatch` | the bound is handed to the runtime or into the provider's own argument vector *before* the work exists, and the work cannot exceed it. Nothing is predicted: each is a count of things this runtime itself authorizes — seconds on its own clock, attempts it creates, turns it writes into the invocation | `wall_clock`, `attempts`, `turns` |
| `measured_after_the_fact` | the provider reports it when the work is over, so the earliest moment it can be compared to anything is after the call it describes has already happened | `input_tokens`, `output_tokens` |

Three rules follow, and the rest of this record is their application:

1. **Only a `counted_before_dispatch` dimension gates.** No attempt starts unless
   every such bound is known, attested and enforceable at dispatch.
2. **A `measured_after_the_fact` dimension is never called a ceiling or a
   maximum** — not here, not in a field name, not in a receipt, not in a
   projection. Its field name says *threshold*, and what it governs is the **next**
   admission decision, never the attempt whose receipt carries it.
3. **No estimated or defaulted quantity gates, and none enters a receipt.** That
   is why rule 2 exists instead of a forecast: the alternative to naming a
   post-hoc quantity honestly is inventing a pre-dispatch one.

### A budget is a published revision of bounds, and its binding site is its scope

One record type, `budget-revision/v1`, bound at exactly three sites:

- an **agent node's** `budget` reference bounds **one attempt** of that node;
- a **subworkflow node's** `budget` reference bounds the **child run**, by the
  run rule below (a subworkflow opens no provider attempt of its own);
- the **run configuration revision**'s `run_budget` reference bounds the **whole
  run**.

There is no scope field inside the record: the binding site is the scope, so one
published bound set serves all three, and a retry is bounded per attempt while
the run total still counts every attempt. What differs by site is the
*aggregation*, and that is decided under "Meters" below, not by a field.

### The exact fields

| field | | class | meter | unit |
| --- | --- | --- | --- | --- |
| `budget_id` | mandatory | — | — | the lineage name the reference's `ref` resolves (identity owned by #22 / ADR 0007) |
| `revision_number` | mandatory | — | — | positive signed int64, as `auth_profile_revisions` already numbers a revision |
| `wall_clock_seconds` | mandatory | `counted_before_dispatch` | runtime | integer ≥ 1, machine time bought |
| `maximum_attempts` | mandatory | `counted_before_dispatch` | runtime | integer ≥ 1, provider attempts the runtime may open |
| `maximum_turns` | optional | `counted_before_dispatch` | executor | integer ≥ 1, assistant turns the invocation may authorize |
| `input_token_admission_threshold` | optional | `measured_after_the_fact` | executor | integer ≥ 1, from the provider's own usage report |
| `output_token_admission_threshold` | optional | `measured_after_the_fact` | executor | integer ≥ 1, from the provider's own usage report |

**The two mandatory dimensions are the two the runtime counts by itself**, with
no cooperation from any provider: it owns the clock and the kill, and it owns
the decision to open an attempt. A budget whose every bound rests on provider
goodwill bounds nothing, and `maximum_attempts` is the dimension that bounds the
*number of billed calls* — the quantity a retry policy multiplies.

**Exact boundary of the wall-clock dimension.** One attempt's duration is
measured on the runtime's monotonic clock, from the instant the supervisor arms
the deadline — immediately before the process group is created — to the instant
that group reaches its terminal disposition. It is not the provider's
self-reported duration. The measurement is recorded in milliseconds and the
bound is declared in seconds; the bound is exceeded when the measured
milliseconds exceed `wall_clock_seconds × 1000`. An attempt refused before
launch has no duration and spends nothing, which is the same rule ADR 0006
already states for a `blocked` receipt.

**`maximum_attempts` and the retry policy are different questions.** The retry
policy (ADR 0006's `retry` reference) decides *whether* a failed attempt is
retried; this dimension decides *how many attempts may exist at all*. Where they
disagree the smaller governs, and exhausting this count is a terminal
budget-exhausted receipt, not a retry decision.

**An absent optional dimension means not budgeted — never estimated, never
defaulted.** A publish omitting `maximum_turns` states that this budget bounds no
turn count; it acquires no house default and no consumer may infer one. That is
Atelier 1's `None`-means-unmeasured rule on the bound side. It does **not** mean
*unbounded at the executor*: the attested absolute ceiling below always applies,
and it is not a default because it is neither inferred nor the budget's — it is
the executor revision's own published limit.

**There is no money dimension in `budget-revision/v1`.** Revision 1 of this draft
carried a `cost_ceiling` refused unless auth was `AuthMode.API_KEY`. That was
wrong twice over, and the Codex review is right on both counts: `AuthMode`
states how credentials enter, not that any provider reports currency, and this
record explicitly refuses price tables and token-to-money conversion — so no
caller could measure or enforce the field, and an inert speculative field is
exactly the growth this repository refuses. Money enters as one further
dimension of a later `budget-revision/v2` **when, and only when, a named owner
supplies exact per-call currency measurements** — a provider that reports billed
amounts, or a billing ledger read as a first-class source. Nothing about the
record's shape prevents that addition; nothing about it invites a guess today.

### Meters: what may be compared, and what may be summed

Every measured value carries a **meter identity**:
`(provider_id, executor_revision, meter_name, meter_revision)`. The runtime's own
meter — `atelier2-runtime`, revision 1 — measures `wall_clock` and `attempts`
identically for every provider, and that identity is what makes those two
dimensions **composable**. `turns` and the token dimensions are metered by the
executor, and one provider's turn is not another's: an assistant turn under
`--max-turns` on `claude-subscription/v1` is that executor's definition and no
other's.

So:

- **A run-scope bound in a runtime-metered dimension is a sum.** Run wall-clock is
  the sum of attempt durations; run attempts is the count of attempts opened.
- **A run-scope bound in an executor-metered dimension is applied once per meter
  identity present in the run**, against that meter's own sum. A run mixing two
  executors is bounded in turns for each of them.
- **No cross-meter sum is ever formed.** A value carrying meter A never enters a
  total with meter B, and a consumer asking for a single cross-meter total is
  refused, naming both meter identities. A raw integer sum of unlike meters is
  not provider neutrality; it is a silent unit error.
- **The budget record itself carries no meter identity**, which is what keeps it
  provider-neutral and reusable across bindings. The *measurement* carries the
  identity, and the comparison happens where both are known: at binding, against
  the executor's attestation, and at receipt time, against the recorded meter.

**What an executor attests.** Alongside `declared_capabilities`, an executor
manifest entry declares its **budget meters**: for each dimension it supports,
the meter name, the meter revision, the unit, and the enforcement class it can
honour. It also declares which dimensions it **requires** to be bound. This is a
separate typed declaration on the manifest entry, not a widening of
`AgentExecutionCapability`, whose closed domain stays `headless | interactive`
per ADR 0006 and #60.

### Identity

A budget revision is identified by SHA-256 over the repository's existing
framing, `atelier2.contracts.hashing.frame` — no second encoding:

```text
frame("budget-revision/v1",
      budget_id                        utf-8 bytes,
      revision_number                  >Q,
      wall_clock_seconds               >Q,
      maximum_attempts                 >Q,
      maximum_turns                    >Q or the zero-length field when absent,
      input_token_admission_threshold  >Q or the zero-length field when absent,
      output_token_admission_threshold >Q or the zero-length field when absent)
```

Per ADR 0006's rule, an absent optional value is the zero-length field in its
declared position, so absence never shifts the frame and is never confused with
zero. This hash is the revision id a `{ref, revision}` reference names.

### Binding: every refusal, and where it happens

**A budget publication validates its own bytes and nothing else.** A standalone
budget revision knows no auth profile, no executor and no run it may later be
bound into, so it cannot refuse on their behalf — revision 1's "refused at
publish" sentence claimed an authority the object does not have, and the Codex
review is right to strike it. Compatibility is refused where the parties are
known, and ADR 0006 already owns those two moments:

| refusal | phase (ADR 0006) |
| --- | --- |
| a node's bound exceeds the run's, in any dimension both declare | reference binding — publish preview and run start |
| the selected executor does not attest a meter for a dimension this budget bounds | executability — run start |
| the selected executor requires a dimension this budget leaves absent | executability — run start |
| a bound exceeds the executor revision's attested absolute ceiling | executability — run start |

Every one of these refuses **before a single process exists**: no run, no
attempt, no receipt, no provider effect. Each names the dimension, both values
and both revision ids. Per #1 a higher budget is an authority change — a new
published revision, never an edit — and none of these refusals is ever resolved
by clamping a published number down to a smaller one, because a published bound
that silently means something else is worse than a refusal.

A dimension the run leaves unbudgeted bounds nothing at the run scope, so any
node value passes it — the node's own bound and the attested ceiling still apply.

### Enforcement, dimension by dimension

**1. Wall-clock — by the supervisor, in real time.** The bound is armed as a
deadline at launch authorization and, on expiry, drives the cancellation path
that already exists, so an exhausted attempt reaches exactly one terminal
disposition through the proven route rather than a second one.

**2. Attempts — by the runtime, at the moment it would open one.** The runtime
authorizes every attempt; the attempt that would exceed the count is never
opened, and the node takes its terminal budget-exhausted receipt instead.

**3. Turns — in the provider's own argument vector, or not at all.** The
executor writes the bound into the invocation it prepares — the landed
`--max-turns` is the measured example, and its source already names
`--max-turns 1` a stopgap awaiting this record. An executor that does not attest
a hard turn meter cannot carry a turn bound, and a budget that declares one
against it is **refused**. Revision 1 allowed such a bound to fall back silently
to post-hoc judgement; that is precisely the defect of calling a threshold a
ceiling, and the fallback is deleted. A dimension never changes its enforcement
class at runtime.

**4. Tokens — measured after the fact, and named that way.** No provider offers
this runtime a live token meter, so the token dimensions do not gate the attempt
that consumes them. The receipt records the true measurement, and the threshold
is read by the **next** admission decision — this node's retry, and every node
the scheduler has not yet started. There is no pretend-realtime metering, no
polling estimate and no mid-flight extrapolation.

**5. A bound dimension the provider does not report is a failure, not a zero.**
If a measurement a bound depends on is missing or malformed in the provider's
completion, the attempt ends in exactly **one typed terminal provider-contract
failure**. It is never `None` silently read as "under budget". The durable token
for that failure belongs to #16, whose V8→V9 phase already owns turning a
provider-contract violation into one typed terminal attempt failure; this record
adds no second failure vocabulary.

**6. One normalized usage report, `attempt-usage/v1`.** The executor seam returns,
for every attempt:

| field | | meaning |
| --- | --- | --- |
| `attempt_duration_milliseconds` | mandatory | the runtime's own measurement, boundary as defined above |
| `turns` | optional, with meter identity | provider-reported assistant turns |
| `input_tokens` | optional, with meter identity | provider-reported |
| `output_tokens` | optional, with meter identity | provider-reported |

Optional means *unreported*, which is not zero. The Claude executor fills `turns`
from the `num_turns` its envelope already carries and the token fields from the
same envelope's `usage` object — both are in the answer its landed containment
test reads today, and both are discarded. The exact `usage` field mapping is
measured when that adapter slice lands; a field the adapter cannot map is an
unattested meter, refused at binding, never a guessed zero.

**7. Exhaustion is a typed terminal receipt.** Per #1 an exhausted budget is a
terminal failure receipt, never silent hanging: ADR 0006's `failed` disposition
with a typed reason naming the dimension, the bound, the measured value and the
scope exceeded. No retry follows a budget-exhausted result. A `blocked` receipt
spends no budget, as 0006 already says.

### The absolute ceiling no workflow can raise

An executor revision attests, immutably and per attempt, its own
`wall_clock_seconds` and `maximum_turns`. It is not authorable by any workflow,
it is not a runtime setting and it is not read from the environment: raising it
is a new published executor revision, visible in a diff and reviewable. It
applies whether or not a budget bounds that dimension, and a budget bound above
it is refused rather than clamped.

For the first tool-bearing executor revision — #60's, which defers this value
here — the attested ceiling is **20 turns and 600 seconds per attempt**. The
reasoning, since #60 asks for a number and not a principle: the landed tool-free
print call is bounded at one turn and needs no more, a canary that repairs one
red test needs a loop of a few turns, so twenty is an order of magnitude of
headroom and still a bill an operator can bound on sight; ten minutes of machine
time is far beyond any attempt measured so far and short enough that a wedged
loop dies inside one operator's coffee. A future executor may attest different
numbers —
what it may not do is attest none.

### Aggregation, the scheduler's denominator, and the exposure an operator can read

Run consumption is the aggregation defined under "Meters", dimension by
dimension, over the run's terminal receipts. **Run wall-clock is summed attempt
duration, not elapsed run time**, for two deliberate reasons: parallel work must
not look more expensive than the same work run serially, and a `wait` node
blocked on a human must not spend a budget nothing is buying. An elapsed-time
deadline is a schedule, not a budget, and this record does not own one.

Remaining budget is the run bound minus that aggregate, and it is what #1's
parallelism sentence reads. A ready node is admitted only if remaining allows it:
where the node declares a bound in a dimension the run bounds, remaining must
cover that bound; where it declares none, remaining in that dimension must be
positive. Effective parallelism is therefore the smaller of the configured
parallelism limit and the number of ready nodes that fit — the denominator #1
asks for. When remaining cannot admit a ready node, the ready set does not
silently stop shrinking: that node receives its budget-exhausted terminal
receipt.

**The residue, stated as a number an operator can read before the run starts.**
Because the token dimensions are measured after the fact, work already in flight
when a threshold is crossed is not stopped by it. That exposure is bounded, and
the bound is readable at publish time: at most **`parallelism` attempts** are in
flight, and each of them is already bounded by counted quantities — its turn
bound and its deadline. So the most work that can still happen after a threshold
is crossed is `parallelism × maximum_turns` turns and `parallelism ×
wall_clock_seconds` seconds of machine time. This record deliberately does not
convert that into a token number, because no counted quantity converts to tokens
without an estimate. It bounds the *opportunity*, exactly, rather than predicting
the *consumption*, badly.

### The finding the first productive run left here, and what this record does with it

Atelier 2's first productive work product was an adversarial review of this
draft, carried through the machine itself (receipt `f74d03dd…`, one billed call).
It named claim 5 — post-hoc token and cost judgement — the weakest load-bearing
claim: a ceiling judged after the receipt is *"a ledger, not a gate"*, and claim 2
compounded it by leaving cost governed by nothing at all. Codex's independent
review reached the same place from the other side: a threshold must not be called
a maximum.

**The diagnosis is accepted, and revision 2 is the answer to it.** The prescribed
fix — a pre-dispatch forecast of consumption from recent receipts — is
**refused**, because it is exactly the estimation this record refuses by name. A
forecast that gates is an invented quantity deciding whether real work happens:
its error is paid either in refused legitimate work or in the overshoot it was
meant to stop, and the receipt would then carry a number nobody measured. Claim 3
stands.

The record answers the diagnosis by moving the gate **earlier** rather than
making it speculative:

- Every dimension that gates is now **counted** before dispatch — the deadline,
  the attempt count, the turn count. None of them is estimated, and none of them
  needs the provider's goodwill except the one written into its own argument
  vector, which the executor must attest before it may be used.
- The dimensions that cannot be counted in advance stop being called ceilings.
  They are thresholds, they govern the next admission, and no operator reads them
  as a gate because nothing in the record calls them one.
- "Cost is ungoverned" is answered without pricing anything: what governs cost is
  a bound on the counted quantities that *produce* it — attempts, turns and
  machine time — not a currency figure a subscription never emits.
- The run's live question was that with parallelism N the worst case is N
  in-flight overshoots and the record never stated that bound as a readable
  number. It is stated now, above, and because every in-flight attempt is hard
  bounded in turns and seconds, the exposure is bounded in *size* as well as in
  *count*.

### What #60 derives from this record

- No tool executor starts without a bound turn and time budget: `wall_clock_seconds`
  is mandatory in every budget, and that executor's attestation **requires** the
  turn dimension, so a budget omitting it is refused at run start.
- Exhaustion of either is a terminal failure result with exactly one receipt,
  through the existing cancellation path — never a hang, and no retry after it.
- The small immutable absolute ceiling no workflow can raise is attested in the
  executor revision, and its value is decided above: 20 turns, 600 seconds.
- Until the budget contract lands, that attested ceiling may stand **alone** as
  #60's bridge — visible, immutable, unavailable to workflow configuration, and
  explicitly not `budget-revision/v1`. Its removal trigger is named in the route
  below.

## Consequences

- ADR 0006's `budget` reference resolves to a real published object with units,
  so the V3 vocabulary stops naming an owner that does not exist.
- The executor manifest grows a budget-meter attestation, and the executor seam
  grows one normalized usage report the Claude executor fills from envelope
  fields it already sees and drops. `AgentExecutionCapability` is untouched.
- The node receipt grows measured consumption with its meter identity. That is a
  field addition to the `node-receipt/v3` envelope ADR 0006 introduces and #63
  cuts over to — never a widening of `AgentReceiptV2`.
- A subscription deployment is fully budgetable without a single invented price,
  and without a money field nobody can measure.
- Mandatory wall-clock and attempt bounds make every budgeted node killable and
  countable, which is what turns "exhausted budget is terminal, never hanging"
  into an enforceable rule.
- Token bounds are weaker than an operator may expect. They are named as
  thresholds so that nobody builds on a precision that does not exist, and the
  exposure they leave is bounded by the counted dimensions.

## Implementation route

The route is explicitly staged, because the accepted owner of a workflow-bound
budget is format V3, whose catalog, store and run execution are not landed.
**Format V2 stays frozen: no budget reference enters a V2 workflow and no field
enters `AgentReceiptV2`** — a V2 bridge would be a second contract to delete
later. No slice lands as uncalled foundation; each names the caller that consumes
it.

| # | lands | depends on | first caller | deletes |
| --- | --- | --- | --- | --- |
| 0 | this decision | — | — | — |
| 1 | `budget-revision/v1` contract, catalog and publication | #22 / ADR 0007 identity, the V3 catalog owner | the first V3 workflow that binds a budget | — |
| 2 | executor budget-meter attestation, `attempt-usage/v1`, deadline arming, binding refusals | #15 (cancel convergence, whose path the deadline rides), #16 (the typed terminal provider-contract failure) | the bound executor at run start | `_HEARTBEAT_MAXIMUM_TURNS` in `adapters/claude_subscription.py`; #60's ceiling-only bridge state |
| 3 | receipt consumption fields with meter identity | slice 2, #63's `node-receipt/v3` store cutover | the receipt writer | — |
| 4 | run aggregation and admission | slices 1–3, #1 story 3 | the scheduler | — |

#60's canary needs none of slices 1–4: it runs under the attested absolute
ceiling alone. That bridge state — an executor running with no workflow-bound
budget — ends when slice 2 lands, and the ceiling itself remains as what it is,
the bound no publication may raise.

## Required proofs before acceptance

This record is a draft; nothing below exists yet.

- A budget revision hashes over the exact framed preimage above, and a differing,
  re-ordered or absent-versus-zero dimension yields a different id.
- The attempt duration boundary is exact: an attempt is measured from deadline
  arming to terminal disposition of its process group, in milliseconds, on the
  monotonic clock, and a refused-before-launch attempt records no duration.
- Every `attempt-usage/v1` field has a proven unit, and an unreported optional
  field is distinguishable from a reported zero.
- **Hard and post-hoc are proven separately**: an attempt exceeding its
  `wall_clock_seconds` and one exceeding its `maximum_turns` are stopped, by the
  deadline and by the invocation bound respectively; an attempt whose measured
  tokens exceed the threshold completes and is recorded, and the *next* admission
  refuses on it.
- The attempt that would exceed `maximum_attempts` is never opened, and its node
  takes the terminal budget-exhausted receipt.
- A budget bounding a dimension the selected executor does not attest is refused
  at run start, and an executor requiring a dimension the budget omits is refused
  the same way — in both cases with no run, attempt, receipt or provider effect.
- A bound exceeding the executor's attested absolute ceiling is refused, not
  clamped, and the refusal names both values.
- A bound measurement missing or malformed in the provider's completion produces
  exactly one typed terminal result, and never counts as under budget.
- A node bound exceeding the run bound in any dimension is refused before a
  process is created, and the refusal names the dimension.
- **No cross-meter sum exists**: a run over two executors bounds turns per meter
  identity, and a request for one cross-meter total is refused naming both.
- A run's consumption in the runtime-metered dimensions equals the sum over its
  receipts, and parallel execution does not change that sum.
- Remaining run budget bounds admission: with a run bound that admits one of two
  ready nodes, exactly one starts and the other takes a terminal budget-exhausted
  receipt.
- No money field exists in any published budget revision, and none is added until
  a caller measures currency exactly.

## Out of scope

- **Money, pricing tables and any token-to-money conversion.** No measurement
  owner today. Money returns as a dimension of a later revision when a provider
  or billing ledger supplies exact per-call currency, alongside the platform
  adapter (#24) — never derived in a receipt and never estimated from tokens.
- **Provider rate-limit modeling.** Subscription limits are opaque, so a
  throttled or refused call is a provider failure under the retry policy, not a
  budget dimension.
- **Multi-run, project and monthly quotas.** A budget here bounds one attempt,
  one child run and one run; aggregation across runs is #8's balance question or
  a project-level owner, and neither is decided here.
- **Deterministic node bounds.** ADR 0006 refuses `budget` on `deterministic`;
  its wall-clock belongs to the operation revision, which knows its own cost.
- **The durable failure vocabulary and schema version** (#16), and the V3 receipt
  store cutover (#63). This record names the results it needs; it does not decide
  their tokens or their migration.
- **The scheduler itself** (#1 story 3). This record gives it a denominator and
  an admission rule; it does not decide its ready-set machinery.

## Supersedes

None.
