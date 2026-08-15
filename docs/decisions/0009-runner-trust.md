# ADR 0009: One trust boundary separates the coordinating service from every runner

- Status: PROPOSED 2026-08-15 — decision only, nothing implemented
- Date: 2026-08-15
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
- Decision authority: [Issue #21](https://github.com/FlexOr2/atelier-2/issues/21),
  exact body SHA-256 of the served UTF-8 bytes including their trailing newline
  `81cf4b1f4703ad6f7000836037beccf848fea9617c633ccd0b8a32b17dca47cf`
- Depends on: [ADR 0001](0001-durable-runtime.md) (process ownership, attempt
  states), [ADR 0003](0003-http-api.md) (the control surface this record
  authenticates), [ADR 0004](0004-local-cockpit.md) (the local-only boundary this
  record replaces), [ADR 0006](0006-node-vocabulary.md) (attestation vocabulary,
  reused and never duplicated), [ADR 0008](0008-budget-units.md) (the attempt
  deadline this record reuses as a lifetime bound)
- Feeds: [#7](https://github.com/FlexOr2/atelier-2/issues/7) (actor attribution),
  [#9](https://github.com/FlexOr2/atelier-2/issues/9) part 3 (remote attach epic,
  gated on this record), [#60](https://github.com/FlexOr2/atelier-2/issues/60)
  (sandbox probe as attested state)
- Names, never decides, the dependencies owned elsewhere:
  [#16](https://github.com/FlexOr2/atelier-2/issues/16) (durable failure
  vocabulary), [#23](https://github.com/FlexOr2/atelier-2/issues/23)
  (multi-project isolation), [#58](https://github.com/FlexOr2/atelier-2/issues/58)
  (workspace lease)

## Context

Issue #1 wants the cockpit reachable at `hallucinai.de/atelier`. Today the API
has no operator identity: any client that reaches the port may publish, start,
answer, cancel, and reconcile, and the `actor` field on a reconciliation command
is a string the caller asserts about itself. The only control standing between a
stranger and the operator's billed subscription is `atelier2.host`, which refuses
composition when a Claude subscription executor meets a bind that is not a
literal loopback address, with the reason written into the refusal: the billed
boundary stays on this machine until an authenticated boundary exists.

That refusal is correct and it is a placeholder. Nothing owns what happens when
execution moves off this machine — who a runner is, how it proves that, what it
may do with an attempt, and how the terminal channel that carries human
keystrokes into a credential-bearing process is gated. #9 part 3 is blocked on
this record, #7 needs an actor before commands can be attributed, and two
vision-panel lenses marked the gap CRITICAL with no owner.

The operator directive of 2026-08-15 (#1) sharpens the shape: agents are
default-capable and restricted by their definition, and fail-closed staging
stays exactly where the trust concerns are — credentials, billing, sandbox. This
record is that boundary, so strictness here follows the directive rather than
contradicting it.

## Decision

### 1. A runner is the process that owns provider invocation

A **runner** is the process that launches, supervises, and reaps the provider
process for exactly one bound attempt, on exactly one host, together with the
credential material that host holds. Not runners: the durable core and the API
(the **coordinating service**), the cockpit and the conductor (#7) — both API
clients — and the provider CLI child, which is the runner's child. Today's runner
is the supervisor inside the host process: ADR 0001's Unix control endpoint,
watchdog generation, delegated cgroup and exec guard. That is a runner co-located
with the service, not the absence of one, and naming it now is what keeps a
remote runner from becoming a second architecture later.

There is exactly **one** trust boundary in the product: between the coordinating
service and any runner. Everything a runner says is evidence; only the service
writes durable truth. The tier below changes the mechanism that carries the
boundary, never the rules it enforces.

### 2. Two deployment tiers, one rule set

A deployment declares its runner tier.

- `same-host`: service and runner on one machine under one OS user, connected
  over the supervisor's Unix control endpoint. Identity is OS-enforced —
  filesystem permission on the endpoint path plus the descendant relationship
  ADR 0001 already attests through the cgroup. No token is minted, because a
  token that proves less than the OS check is ceremony.
- `remote`: a separate machine or trust domain. Mutual authentication is
  mandatory in both directions. This tier is a named successor epic (#9 part 3);
  this record binds its rules, not its transport.

A `same-host` mechanism is never accepted for a connection that did not arrive
over that local endpoint: the service refuses it (`runner-transport-mismatch`).
Tier is a property of the deployment, not a per-request claim.

### 3. Operator authentication gates every non-loopback bind

The API gains an authenticated **operator principal** before any bind that is
not a literal loopback address. Until that authenticator exists, the composition
refusal in `atelier2.host` generalizes from "a Claude subscription executor is
composed" to the whole API: a non-loopback bind with no composed operator
authenticator refuses at composition (`unauthenticated-remote-bind`), in the same
loud shape as today, never as a warning or a default-open mode.

Named mechanism per tier, single-user V1 (#1):

- loopback: the operator is whoever holds the machine session; no credential,
  and the bind is what carries the boundary.
- remote: one credential per operator client, verified by the service over TLS
  the deployment terminates. The verifier material is a path the host reads at
  composition — never a value in a workflow, prompt, event, receipt, log, or API
  resource (#1's secret rule). Session lifetime is configured per deployment
  against a named need (a stolen cockpit session must not outlive the operator's
  own session) and is live-versioned configuration under #1, not a constant this
  record invents.

Rejected alternatives, because neither yields an actor for #7: a shared secret
carried in a URL, and an address allowlist used as the whole authentication.

### 4. A runner authenticates per runner, and the service authenticates back

Every remote runner holds **its own** credential. A shared fleet secret is
refused as a design: it cannot be revoked for one host and it names no actor.
Enrolment is an explicit operator act with a durable record binding the runner
id, its tier, the reference to its credential verifier, the enrolling actor, and
the manifest attested at enrolment (§7). An unenrolled runner receives no attempt
binding and no attach ticket (`runner-unknown`); a revoked one likewise
(`runner-revoked`).

The service authenticates itself to the runner in the same handshake, and a
runner refuses work from an unauthenticated service. Without that direction,
anything that can reach a runner can spend the operator's subscription.
Revocation removes the enrolment record and stops new bindings; it asserts
nothing about an attempt already in flight (§10).

### 5. What a runner may do, and what it must not

A runner **may**: accept exactly one attempt binding — the attempt whose id and
request hash it was handed after the *service's* own compare-and-set to
`LAUNCH_ARMED` (ADR 0001; the service arms, never the runner); launch, supervise,
cancel, and reap that provider process; resolve the credential its bound auth
profile names by reference (§6); and report observations — process state, exit,
provider frames, usage measurements.

A runner **must not**, each enforced by the service rather than trusted to the
runner:

- mint a verdict. Receipts, dispositions, budget judgements, and the durable
  event sequence are written by the core (ADR 0001, 0006, 0008). A report
  carrying one is refused whole (`runner-report-out-of-scope`).
- publish or alter catalog content — workflow, agent-configuration, auth-profile,
  budget, retry, skill, tool, or capability revisions (`runner-not-authorized`).
- execute an attempt it was not bound (`attempt-binding-unknown`), or anything
  under an attempt id that already reached a terminal state
  (`attempt-binding-terminal`).
- read credential material outside its bound auth profile — another profile's,
  another runner's, or another project's, whose isolation #23 owns.
- widen its own capability set. ADR 0006's rule holds unchanged at this boundary:
  capabilities are attested, never claimed.

### 6. Credentials reach a runner by reference, never by value

The service transmits an auth-profile revision and the credential *reference* its
host resolves locally. It transmits no secret value, ever, in either direction.
This confirms the landed Claude adapter contract, where
`ClaudeSubscriptionSettings.credential_directory` is the credential owner and the
invocation carries paths and switches only. The consequence is the point: a
remote runner holds its own credential material locally, the service never
becomes a secret-distribution channel, and a compromised service does not leak
the operator's subscription. A runner that cannot resolve
its bound reference refuses at run start (`auth-profile-unresolvable`), with no
fallback to another auth mode.

### 7. A runner is attested, and a binding needs a runner that attests it

This record mints no parallel vocabulary. ADR 0006's runtime capability revision
— a manifest produced by build and probe, never authored, never grantable by a
document — becomes **a runner's** attestation. A single-runner deployment is the
case where nothing about 0006 changes.

At enrolment and at every connection a runner presents:

- its runtime capability revision, whose `agent_execution` entries already carry
  executor identity, provider mode, build identity, gate run and evidence
  reference — so auth-mode enforcement needs no new capability name here;
- its **sandbox probe state**: the functional probe #60 defines, executed on that
  host. An executable check is not a sandbox proof, and a probe result from
  another host is not this host's;
- its **version pins**: the exact provider CLI versions its executor revisions
  attest, as ADR 0008's meter revision already pins Claude 2.1.221.

The service stores the presented manifest with the enrolment and compares it at
each connection. A manifest differing from the enrolled one is a new attestation
requiring a new operator enrolment, visible as a diff, never a silent widening
(`runner-attestation-changed`).

**Placement is the half this record adds.** ADR 0006 refuses a capability the
bound runtime capability revision does not attest. Run start now also refuses a
binding that **no connected, enrolled runner** attests
(`no-runner-attests-binding`), naming the node, the binding, and the missing
attestation, before any durable run, binding, attempt, or provider process — the
409 shape #60 already uses. An unplaceable run is refused, never queued in the
hope a runner appears, because queueing it turns fail-closed into a hang. The
same rule carries provider auth modes, as #1 requires: a runner declares the
modes it can *enforce* on its host, and a binding to a mode it does not attest
refuses rather than downgrading.

### 8. The terminal channel is a separately gated, default-off capability

Attach is the one channel that lets a human's keystrokes into a
credential-bearing process, so execution attestation never implies it. It is
gated separately from execution at four points:

- **Default off.** A deployment enables it explicitly, and the runner attests it
  as `attach_channel` — one new runner-scoped entry in ADR 0006's manifest, under
  0006's own rules and produced the same way. It does not restate `mode`, whose
  sole declarer stays the bound agent-configuration revision (#9 Rev. 4).
- **Per-attach step-up.** Enabling the deployment is not consent for one attach.
  Each attach is a distinct operator authorization producing a single-use ticket
  bound to exactly one `(attempt id, runner id, operator actor)`.
- **Short lifetime with a named bound.** A ticket is single-use and expires at
  the earlier of the attach session's end or the bound attempt's
  `attempt_deadline_seconds` (ADR 0008). No new duration constant is minted; the
  attempt's own deadline is the honest ceiling, since a ticket cannot usefully
  outlive the process it attaches to.
- **Audit.** Every attach writes a durable record — actor, attempt, runner,
  ticket, start and end. An attach whose audit record cannot be written does not
  start. The attempt's receipt carries #9's operator-influenced marking as that
  record already requires.

V1 attach stays local (#9 part 2). This record does not open remote attach; it
states what remote attach must present when its epic runs.

### 9. Every command carries a typed, authenticated actor

An actor is a typed identity, not a free string, with exactly three kinds:

- `operator` — an authenticated operator principal (§3);
- `agent` — the conductor (#7) or any client acting under a published
  configuration or policy revision, recorded together with that revision id and
  the operator who enrolled it;
- `runner` — reports only, and never a command that changes catalog or verdict.

Durable command records bind the actor identity and, for an `agent`, the exact
published revision it acted under, so "who started this, under which published
policy" is answerable from the record. `ReconcileActor`'s caller-asserted string
is superseded by that authenticated identity in the same change that lands the
operator authenticator; until then it is a self-declared label and no document,
API description, or cockpit surface may call it attribution.

### 10. Failure semantics: loud, and never a widened blind spot

- Every refusal named here is typed and terminal. None degrades to a weaker auth
  mode, a longer timeout, an unauthenticated retry, or a clamped bound.
- Authentication, enrolment, or attestation that cannot be verified refuses
  before any durable run, binding, attempt, receipt, or provider start.
- **A lost runner connection is not evidence about the provider process.** An
  armed attempt whose runner became unreachable stays `POSSIBLY_RAN` (ADR 0001)
  until an authoritative observation resolves it. The service never replaces,
  replays, or re-places that attempt on another runner. Revocation is subject to
  the same conservatism: it stops new bindings and resolves nothing in flight.
- Remote attempt ownership — lease, heartbeat, fencing — is what would make a
  remote runner's silence resolvable, and it is the remote epic's contract (#9
  part 3). Until it exists, a remote binding is not published as available. The
  seam stays honest: this record makes remote representable and refuses it as
  unavailable rather than pretending it runs.

## Refusals

| Name | Raised when | Boundary |
| --- | --- | --- |
| `unauthenticated-remote-bind` | a non-loopback bind with no composed operator authenticator | host composition |
| `runner-transport-mismatch` | a connection whose transport does not match the declared tier | runner connection |
| `runner-unknown` | a runner with no enrolment record requests work or a ticket | runner connection |
| `runner-revoked` | an enrolment record was removed | runner connection |
| `runner-attestation-changed` | the presented manifest differs from the enrolled one | runner connection |
| `no-runner-attests-binding` | no connected enrolled runner attests the bound capability, executor, provider mode or auth mode | run start |
| `auth-profile-unresolvable` | the bound credential reference does not resolve on the runner's host | run start |
| `attempt-binding-unknown` | a runner acts on an attempt it was not bound | attempt handoff |
| `attempt-binding-terminal` | a runner acts under a terminal attempt id | attempt handoff |
| `runner-report-out-of-scope` | a report carries a disposition, receipt, or catalog mutation | runner report |
| `runner-not-authorized` | a runner attempts a catalog or command operation | service authorization |

Durable failure tokens, where any of these must become one, are minted by #16;
this record borrows that owner rather than opening a second vocabulary.

## Consequences

- The loopback rule stops being a Claude-specific special case and becomes the
  product's general rule: no remote bind without an authenticated operator. ADR
  0004's "safe only on the trusted local boundary" gets its named successor, and
  #7 gets the identity it needs before the conductor issues commands.
- V1 gains no daemon and no new process. The same-host runner is the supervisor
  that exists today; only its name and its obligations become explicit.
- The remote epic is unblocked in rules, not in build: transport, protocol and
  the ownership lease remain to be decided by it.
- Per-runner credentials cost an enrolment ceremony, and the operator must
  enable attach and then authorize each one. Both costs are accepted: a shared
  secret has neither revocation nor attribution, and attach is the only path a
  human reaches into a billed process.

## Required proofs before implementation is accepted

- Composition refuses a non-loopback bind with no operator authenticator, and
  the existing loopback composition still succeeds unchanged.
- An unknown, revoked, or manifest-changed runner receives no attempt binding
  and no attach ticket, and no durable row is written for the refusal path.
- Run start refuses a binding no connected runner attests, naming node, binding
  and missing attestation, with no run, binding, attempt, receipt or process.
- A runner report carrying a disposition, receipt, or catalog mutation is
  refused whole and changes nothing durable.
- A full durable and API projection after a fake run contains no credential
  value and no verifier path — the canary shape #58 acceptance 8 already uses.
- Attach without a valid ticket refuses; a ticket is accepted exactly once; a
  failed audit write prevents the attach; an attach past the bound attempt's
  deadline refuses.
- A runner that disappears while an attempt is armed leaves that attempt
  `POSSIBLY_RAN`, and no second runner ever receives it.
- Two enrolled runners with different attested manifests place only the bindings
  each attests, proving placement is per runner and not per deployment.

## Out of scope and stop conditions

This record does not decide: transport, wire protocol or framing for the runner
boundary; the remote epic's scope and its attempt-ownership lease, heartbeat and
fencing contract (#9 part 3); multi-project or multi-tenant isolation (#23); the
storage backend for operator credentials and the cockpit login surface; the
provider-side sandbox mechanism (#60); durable failure token names (#16); rate
limiting and quota.

Stop implementation on: a shared runner secret; a runner writing a receipt,
disposition, or catalog revision; a credential value crossing the service in
either direction; an attach path without a per-attach ticket and an audit
record; an unplaceable run that is queued instead of refused; a remote binding
published as available before the ownership contract exists; or an actor field
described as attribution while it is still caller-asserted.

## Supersedes

None.
