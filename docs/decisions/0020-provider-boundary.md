# ADR 0020: One Runner-owned session port carries every provider; a permission is authorisation, and the transcript is its projection

- Status: ACCEPTED 2026-09-04 — decision only, no slice implemented
- Date: 2026-09-04
- Decision authority: the operator ruling of 2026-09-04 on proposal
  [#1177](https://github.com/FlexOr2/atelier-2/issues/1177), which owns the
  proposal history. Two independent counter-checks are recorded on that
  proposal — the first rejected the draft, the second accepted it with
  changes — and both sets of changes are carried into this record.
- Depends on: [ADR 0008](0008-budget-units.md) (turn limiter, meter, money
  absent), [ADR 0009](0009-runner-trust.md) (the trust boundary, provider
  containment, credential reference)
- Feeds: [#1178](https://github.com/FlexOr2/atelier-2/issues/1178) (step 0),
  [#1174](https://github.com/FlexOr2/atelier-2/issues/1174) (output-seam schema
  discipline), [#943](https://github.com/FlexOr2/atelier-2/issues/943) and
  [#1099](https://github.com/FlexOr2/atelier-2/issues/1099) (the human terminal
  seat, which stays separate)

## Context

Six live runs in a row failed at the provider boundary, each a different symptom
of the same shape: three provider CLIs driven in headless print mode, their
standard output parsed by Atelier, schema flags that behave differently per
provider, scrub residue left in the candidate, and processes that die silently
(#1165, #1166, #1174, #943). This boundary is a small share of the product's
code and causes most of its outages, because a print-mode call is a one-way
process: a fixed payload goes to standard input, standard input closes, and
output is collected until end of file. A provider that wants to ask a question
mid-turn, or a caller that wants to cancel, has nowhere to speak. Meanwhile
every vendor maintains a structured duplex channel of its own, so a hand-written
parser per CLI buys each vendor's release cadence as a permanent bill.

The operator ruled on 2026-09-04: every model stays freely selectable, including
a privately hosted open model; take the simplest path and do not pay
thousand-fold maintenance because a CLI changed; a different mechanism per
provider is acceptable because an abstraction layer exists; the architecture
must be coherent. Billing runs through subscriptions.

## Decision

### 1. One session port in the Runner owns a provider's lifetime

The Runner owns an `AgentSession` port: `open`, `send(prompt)`, correlated
events (tool called, tool returned, permission requested), `decide(permission)`,
`cancel`, and one terminal result with its meter. Its lifecycle owner is
`runner/session.py`, which already owns candidate lifetime, launch, cancel,
journal and terminal evidence; driver selection is `runner/executors.py`.
`application/execute_agent_attempt.py` stays the Serve-local predecessor path.

Every provider implementation stays a contained child process or an
Atelier-owned bridge process, exactly as ADR 0009 §1 requires: the provider
process is a child of the Runner. An in-process vendor SDK inside the Runner
would be a different trust boundary and is not decided here.

### 2. Three separated artefacts, never one

1. **Live session events** are the driver's contract. Each tool call carries an
   Atelier-owned typed correlation id; provider-side identifiers stay inside the
   driver and never reach a durable record. Without that id, two concurrent
   calls to the same tool cannot be told apart, which is what name matching
   silently lost.
2. **Permission receipts** are the authorisation ledger. A receipt is bound to
   the attempt, the policy revision, the correlation id of the call, and the
   effect: requested effect, offered scope, granted effect, authority. It
   carries enums, hashes and typed values, never raw provider arguments; a value
   that cannot be represented is refused, never truncated. This ledger does not
   replace `AgentReceiptV2`, which stays the receipt of an execution's outcome.
3. **The transcript** is a readable, redacted projection over the same
   correlation id — `attempt-transcript/v3`, with the v1 and v2 readers
   retained. Unknown provider output stays a bounded
   `UnrecognisedProviderOutput` step, and redaction stays solely in transcript
   construction. No session-opened step is added: provider, model and executor
   revision have owners in configuration and in the receipt, and a display
   derives them.

### 3. A permission is authorisation, decided in the Runner, fail-closed

A transcript step is evidence, not a control. The authorisation is an immutable
typed permission policy, bound into the execution binding before the session
opens — neither `AgentConfigurationRevision` nor `AgentExecutionRequestV2`
carries a policy revision today, and that field is part of this decision. A pure
Runner-local decider answers each request from that policy and refuses anything
it does not recognise. It holds no deadline of its own; the attempt deadline
bounds the session. The driver transports the request and the decision and
decides nothing.

### 4. Transport per provider is the vendor's own maintained structured channel

- **Grok**: the native agent-client-protocol mode, pin raised. It is the first
  vector because it removes the print-mode collapse without a third-party
  package.
- **Claude**: the agent-client-protocol adapter as a contained child process
  under a fixed pin. The vendor Python agent SDK is admissible only as an
  Atelier-owned bridge process, never in-process, and needs a permission hook
  there, because an earlier allow rule bypasses the SDK's own tool callback.
  Both paths remain subject to the vendor's subscription terms.
- **Codex**: the vendor SDK with plan login, with the documented JSON
  non-interactive mode as the fallback; the deprecated MCP-server path is not
  taken.
- **Open and self-hosted models**: an agent-client-protocol agent (Goose or
  OpenCode) in front of an OpenAI-compatible endpoint, behind the same port.

A new provider is not configuration alone. The Runner keeps an exact
`(provider, executor revision)` registry in `runner/executors.py`; every new
vector needs selection code, a pin and attestation, a policy, a meter and a
conformance proof — but no new protocol and no new parser once it speaks the
protocol.

### 5. The output schema is judged at Atelier's own seam

Atelier's own output seam is the authority for every provider (#1174). Provider-
side schema enforcement may later be added as supplementary defence; it never
becomes the authority, because its behaviour differs per vendor and release.

### 6. Budgets, credentials and proof keep their existing owners

Budgets follow ADR 0008 unchanged: every multi-turn executor revision attests a
native turn limiter and an exact turn and token meter, and no money value enters
a receipt, a gate or a display. Credentials follow ADR 0009 §6 unchanged: Core
passes a logical reference, the Runner resolves it locally, and the provider's
credential source is offered read-only, so a writing token refresh fails
visibly.

Proof is per adapter and per pin, before a vector is armed: state-machine tests
for split and coalesced frames, unknown messages, permission and cancel races,
end of file without a terminal result, limit refusals, token refresh and
containment drift; a replay from a real capture; and a canary. One successful
transcript is not proof.

### 7. Each predecessor is deleted with its own proof

When a provider's new vector is proven, that provider's print-mode path is
deleted in the same slice — not collected for a cleanup at the end. The
process watchdog, which ADR 0009 already marks a predecessor retained only for
deletion, serves as the migration carrier until the last vector has moved, and
is then deleted. That is the deletion condition ADR 0009 left open; it does not
change what ADR 0009 decided.

## Consequences

- What falls: the per-CLI stream parsers and schema-flag branches in the Claude
  and Grok subscription adapters, the Codex last-message file path, the scrub
  sweep once measured, and finally the watchdog.
- What stays: the durable core and its truth ownership, receipts, the candidate
  and its verification, and `CANDIDATE_UNCHANGED` as the honest failure of an
  attempt that changed nothing.
- Duplex is new surface inside the Runner and each vendor channel is a pin that
  must be raised deliberately: permission and cancel can race, so that race is
  part of every adapter's proof.

## Named edges, not decided here

- A human or Core decision mid-turn needs its own authenticated duplex port with
  a deadline and a cancel-race contract.
- A writable per-attempt credential copy or a refresh broker waits for a
  measured refresh failure and a fresh operator ruling, per ADR 0009 §6.
- The registry cost of open models — one selection entry, pin and attestation
  per hosted model — is accepted, not abstracted away.
- The human terminal seat (#1099) and a real PTY for a child process (#943) stay
  separate from this boundary.
- Whether the Runner is really the live owner is being measured: a live-usage
  audit checks whether the Runner path executed any real attempt on the live
  instance in the last thirty days. If every live attempt still runs through
  the Serve-local path (`application/execute_agent_attempt.py` and watchdog),
  the placement is re-questioned before step 1, and this record either confirms
  the Runner as the owner — which then goes live first — or is amended to make
  the Serve-local path the owner.

## Order

Step 0 is #1178: the existing provider child lifetime moves behind the
`AgentSession` port for the fake executor only, preserving bytes, cancel,
reconnect and terminal evidence. Then: duplex events with correlation,
permission receipts and policy binding proven against the fake; Grok; Claude;
Codex; an open model; transcript v3; deletion of the watchdog. A provider's live
proof is one real `issue-to-pr` run reaching its review node with that builder.

## Supersedes and amends

No ADR is superseded. This record touches, without changing, ADR 0009 §1
(provider containment, kept), ADR 0009 §6 and its credential amendment (kept,
including the condition on a writable copy), ADR 0009's watchdog predecessor
fact (this record supplies its deletion condition), and ADR 0008's turn limiter
and money-absent rules (kept).

The print-mode invocation design being replaced was never a decision record: it
lives in the subscription adapters' own docstrings and in their executor
revision tokens. Removing it therefore supersedes no record and needs no
amendment anywhere.

## Out of scope and stop conditions

This record does not decide an in-process SDK inside the Runner, a mid-turn
human approval port, a writable credential path, terminal-seat design, or
provider pricing. Stop implementation if a driver starts deciding permissions,
if a transcript step is used as an authorisation, if a provider identifier
reaches a durable record instead of an Atelier correlation id, if provider-side
schema enforcement is treated as the authority, if a vector is armed without its
conformance proof and pin, or if a replaced print-mode path is left alive after
its successor is proven.
