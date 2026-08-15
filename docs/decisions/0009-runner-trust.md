# ADR 0009: One trust boundary separates the coordinating service from every runner

- Status: PROPOSED 2026-08-15 — decision only, nothing implemented
- Date: 2026-08-15
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
- Decision authority: [Issue #21](https://github.com/FlexOr2/atelier-2/issues/21),
  SHA-256 over the exact served UTF-8 body bytes with nothing appended — 787
  bytes, no trailing newline —
  `5c03ceb1d5f1b85f81ec3acc1f6dea1c72d89817929a772432b9b02fbb74a56b`. This record
  owns the ADR mandate of #21 and **does not close #21**: the issue also carries
  code obligations (H4/M7/M8) whose predecessor is
  [#86](https://github.com/FlexOr2/atelier-2/issues/86), which blocks that code
  and, by its own dependency correction, not this documentation-only record.
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
  (workspace lease), [#86](https://github.com/FlexOr2/atelier-2/issues/86) (the
  graph interpreter's move into the core, predecessor of #21's code),
  [#15](https://github.com/FlexOr2/atelier-2/issues/15) (the same-host runner
  lifecycle itself: the accepted direct-systemd replacement, its cutover, and the
  deletion of the watchdog and exec-guard predecessor)

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

**How the decision-authority digest above is computed.** The canonical rule is
exact: the bytes the API serves as the issue body, hashed as they are, with
nothing appended — no trailing newline, no re-encoding, no normalization. It is
stated here because this record's first revision got it wrong: a shell pipeline
(`gh ... --jq .body`) silently appended jq's newline before hashing, and the
resulting digest bound bytes GitHub never served. Until ADR 0010's adapter
publishes requirement revisions and computes this digest once instead of a human
pasting it, every record here states the byte count and the absence of a trailing
newline beside the digest, so a reader can re-derive it rather than trust it.

## Decision

### 1. A runner is the per-invocation execution scope that owns provider invocation

A **runner** is the bounded **execution scope** that owns the provider
invocation of exactly one bound attempt, on exactly one host, together with the
credential material that host holds — and that is **identified per invocation**,
so the scope begins and ends with that one invocation and no later invocation
inherits its identity. Ownership and identity are the whole definition; the shape
of the thing that carries the scope is not part of it. Not runners: the durable
core and the API (the **coordinating service**), the cockpit and the conductor
(#7) — both API clients — and the provider CLI process, which runs *inside* a
runner and is never one.

**What a runner is stays fixed; what carries it is mid-replacement**, and the
two must not be conflated, because the same-host carrier is being replaced
underneath this record:

- **Predecessor, condemned.** Today the service spawns one watchdog process per
  attempt (`adapters/agent_processes.py`) and reaches it over a Unix control
  endpoint; that watchdog — not the service — launches the provider under the
  exec guard, holds its handle and its cgroup, supervises it and reaps it
  (`adapters/agent_process_watchdog.py`). By the definition above the watchdog is
  the runner, without qualification. This 1,564-line supervisor, watchdog and
  exec-guard lifecycle is **deleted** by #15's Slice 2 cutover under a binding
  ruling. It is named here as the current state, never as the target.
- **Successor, accepted and in build.** #15's direct-systemd replacement splits
  what the predecessor packed into one process across three roles, and **only one
  of them is the runner**:
  - the **systemd manager** is long-lived, shared, and one per host — never one
    per attempt. It installs the transient unit the service requests, enforces
    the unit's declared bounds, kills its control group and reaps the unit. It
    is host infrastructure the deployment already runs, shared by every unit on
    the machine, and it is *not* a runner: it owns no attempt and carries no
    per-attempt identity.
  - the **transient unit invocation** is the runner: one per bound attempt,
    carrying systemd's own per-invocation identity, and its installation *is* the
    launch authorization. A reused unit name is a different runner, because the
    invocation identity — not the name — is what this record binds.
  - inside that invocation, the **collector** is the unit's main process: it
    validates the launch envelope, publishes the durable launch and result
    evidence bound to that exact invocation identity, starts the provider process
    and observes its exit; the **provider process** is the child the invocation
    exists to run. Neither is a runner — the collector is the runner's voice, the
    provider its subject, and both live and die inside the scope.

The predecessor collapses all three roles into one process, which is why a runner
reads there as "a process"; the successor separates them. The definition survives
that cutover precisely because it binds the runner to the scope's ownership and
per-invocation identity rather than to whichever role executes a step — and
because it never claims that one noun performs launch, supervision and reaping in
both worlds. What crosses the trust boundary is identical under either carrier:
one bound attempt in, one host's credential material held locally, evidence out,
and no durable truth. The runner is *co-located* with the service on this tier —
one machine, one OS user — and co-location is neither co-residence in a process
nor shared identity. Binding the trust boundary to that ownership rather than to
the carrier of the day is what keeps this record true across the cutover, and what
keeps a remote runner from becoming a second architecture later.

There is exactly **one** trust boundary in the product: between the coordinating
service and any runner. Everything a runner says is evidence; only the service
writes durable truth. The tier below changes the mechanism that carries the
boundary, never the rules it enforces.

### 2. Two deployment tiers, one rule set

A deployment declares its runner tier.

- `same-host`: service and runner are **separate execution scopes** on one
  machine under one OS user — never the service's own scope. What carries the
  tier is #15's to build and is being replaced: a watchdog process behind a Unix
  control endpoint today, a transient systemd unit and durable evidence after the
  cutover.
- `remote`: a separate machine or trust domain. Mutual authentication is
  mandatory in both directions. This tier is a named successor epic (#9 part 3);
  this record binds its rules, not its transport.

Work and evidence cross only by the path the declared tier owns; anything
arriving by another path is refused (`runner-transport-mismatch`). Tier is a
property of the deployment, not a per-request claim.

**The identity invariant, stated so it outlives any transport.** Because a runner
is never the coordinating service's own execution scope on any tier, every tier
carries the same obligation, and it is written about the boundary rather than
about a connection —
the same-host boundary is losing its connection entirely:

- Before the service binds an attempt to a runner, and before it accepts any
  report as evidence about that attempt, it establishes that the runner is
  **exactly the one it authorized** — identified per invocation, never by a name
  or path that can be reused after the runner it named is gone.
- A runner accepts work only from the coordinating service, which authenticates
  itself in the same act (§4's return direction).

This record decides no transport and no framing (see *Out of scope*), so it names
no mechanism as the decision, and it deliberately names none that would preserve
the condemned one. #15's direct-systemd successor supplies its own unit and
manager identity when it is composed — its per-invocation unit identity is
already the shape the first half asks for — and #9 part 3 supplies the remote
transport's mutual credential. Both are instances of the sentence, not the
sentence. The refusal `runner-peer-unverified` binds to the invariant; the item
that owns the same-host lifecycle at the time (#15) binds the mechanism, and this
record adds no second owner for it.

**What `same-host` is today, stated as it is rather than as it sounds — and
accepted as that.** The predecessor endpoint's protection is a `0700` directory
and a `0600` socket, and its accept path performs no peer check at all
(`adapters/agent_process_watchdog.py`). That authenticates **the OS user**, not
the peer: any process running as that user may connect. ADR 0001's cgroup attests
the *provider child's* descendant relationship — it says nothing about who opened
a control connection. So `same-host` today is a **same-UID trust domain**, and
this record accepts it as one rather than describing a control that does not
exist. The stake is named with the acceptance: anything running as that user
which reaches the endpoint can launch, cancel and read a billed provider process.
Closing that gap in the predecessor is explicitly *not* asked for here — #15
deletes it, and hardening a condemned owner is the waste this record would
otherwise commission.

That acceptance is bounded, and the bound is the point. It holds while the whole
boundary stays on one machine, under one OS user, at exposure `this-machine`
(§3). It **ends** — and the identity invariant above becomes mandatory before any
attempt binding — at the first of: the `remote` tier; a `reachable` exposure; a
second OS user or a second project sharing the host (#23); or a runner the
service did not itself authorize. Past any of those, an unestablished runner is
refused (`runner-peer-unverified`) rather than admitted on the strength of its
UID.

### 3. Operator authentication gates every exposure beyond this machine

The API gains an authenticated **operator principal** before any exposure that
reaches past the machine it runs on. Until that authenticator exists, the
composition refusal in `atelier2.host` generalizes from "a Claude subscription
executor is composed" to the whole API: an exposed deployment with no composed
operator authenticator refuses at composition (`unauthenticated-exposure`), in
the same loud shape as today, never as a warning or a default-open mode.

**Exposure is declared, never inferred from the bind address.** A loopback bind
does not prove local reach: a reverse proxy, an SSH port forward, a container
port publication, or any other fronting layer leaves Atelier bound to `127.0.0.1`
while the world reaches it — and an unauthenticated API behind such a layer is
exactly the hole this section exists to close. So the deployment declares its
exposure explicitly, and the two facts are checked against each other:

- `this-machine`: nothing outside the machine session may reach the API. A
  non-loopback bind contradicts this declaration and refuses at composition.
- `reachable`: something in front of the service may carry remote callers. An
  operator authenticator is **mandatory**, whatever the bind address is.

An undeclared exposure is not a default; composition refuses until the
deployment states one. Where a fronting proxy is the exposure, the operator
declares that trust relationship together with the authentication it terminates —
and forwarded request headers (`X-Forwarded-For`, `Forwarded`, and their kin) are
**never** read as identity and never as evidence about exposure, because a caller
can write them.

Named mechanism per declared exposure, single-user V1 (#1):

- `this-machine`: the operator is whoever holds the machine session; no
  credential, and the declared exposure plus the loopback bind together carry
  the boundary.
- `reachable`: one credential per operator client, verified by the service over
  TLS the deployment terminates. The verifier material is a path the host reads at
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
the runner attestation presented at enrolment (§7). An unenrolled runner receives
no attempt binding and no attach ticket (`runner-unknown`); a revoked one
likewise (`runner-revoked`).

The service authenticates itself to the runner in the same handshake, and a
runner refuses work from an unauthenticated service. Without that direction,
anything that can reach a runner can spend the operator's subscription.

**Revocation marks the enrolment record revoked; it never deletes it.** A
deleted record makes a revoked runner indistinguishable from one that was never
enrolled, which would collapse `runner-revoked` into `runner-unknown` and erase
the fact an incident needs most: that this runner id was trusted, and when it
stopped being. So enrolment state is durable and three-valued — absent, enrolled,
revoked — carrying the revoking actor and the revocation time. Re-enrolling a
revoked runner id is an explicit operator act with a fresh credential and a fresh
attestation (§7), never a silent return to service. Revocation stops new
bindings; it asserts nothing about an attempt already in flight (§10).

### 5. What a runner may do, and what it must not

A runner **may**: accept exactly one attempt binding — the attempt whose id and
request hash it was handed after the *service's* own compare-and-set to
`LAUNCH_ARMED` (ADR 0001; the service arms, never the runner); launch, supervise,
cancel, and reap that provider process — by whichever roles its carrier assigns
those steps to inside the scope (§1); resolve the credential its bound auth
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

This record mints no parallel capability vocabulary, and it does not widen ADR
0006's. That distinction is the whole of this section, because the two kinds of
fact involved have different producers:

- ADR 0006's **runtime capability revision** is immutable and produced by the
  build and adapter layer: "not authored and not editable", no deployment writes
  one and no document grants an entry. What a build can prove belongs here, and
  nothing else may enter.
- A **host-and-deployment fact** — whether this machine's sandbox actually
  confines, which provider binary versions this host has, whether the operator
  enabled attach for this deployment — is produced by a probe on one host or by
  an operator's choice. None of that is a build product, so writing it into
  0006's manifest would make the manifest authorable, which 0006 forbids.

So a runner presents a **typed runner attestation**: a wrapper that *references*
its runtime capability revision by id and never restates its entries, plus the
host-and-deployment facts above, signed by the runner's own credential (§4) and
valid for one host. The capability revision stays exactly what 0006 made it; the
wrapper is where per-host truth lives.

At enrolment and at every connection a runner presents that attestation,
carrying:

- **the referenced runtime capability revision id**, whose `agent_execution`
  entries already carry executor identity, provider mode, build identity, gate
  run and evidence reference — so auth-mode enforcement needs no new capability
  name and no new manifest entry here;
- its **sandbox probe state**: the functional probe #60 defines, executed on that
  host. An executable check is not a sandbox proof, and a probe result from
  another host is not this host's;
- its **observed version pins**: the provider CLI versions actually present on
  this host, checked against the versions its referenced executor revisions
  attest, as ADR 0008's meter revision already pins Claude 2.1.221. A host whose
  observed version differs from the attested one has a changed attestation, not a
  usable capability;
- its **attach channel state**: whether the deployment enabled the terminal
  channel on this runner (§8). This is a wrapper field, not a capability entry —
  a deployment toggle is exactly the authored grant 0006 refuses, and it never
  restates `mode`, whose sole declarer stays the bound agent-configuration
  revision (#9 Rev. 4).

The service stores the presented attestation with the enrolment and compares it
at each connection. An attestation differing from the enrolled one — a different
capability revision id, a different probe result, a different observed version, a
changed attach state — is a new attestation requiring a new operator enrolment,
visible as a diff, never a silent widening (`runner-attestation-changed`).

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
gated separately from execution, and every gate below is load-bearing on its
own:

- **Default off.** A deployment enables it explicitly, and the runner carries
  that state as the `attach_channel` field of its runner attestation (§7) — a
  wrapper field, never an entry in ADR 0006's immutable manifest, because a
  deployment toggle is an authored grant and 0006 admits none. It does not
  restate `mode`, whose sole declarer stays the bound agent-configuration
  revision (#9 Rev. 4).
- **Per-attach step-up.** Enabling the deployment is not consent for one attach.
  Each attach is a distinct operator authorization producing a single-use ticket
  bound to exactly one `(attempt id, runner id, operator actor)`.
- **A ticket is a credential and is handled as one.** Its value crosses to the
  authorized operator client and nowhere else. Durable state holds the ticket's
  **opaque id and a digest of its value** — never the value itself, in any
  record, log, event, receipt or API resource, under the same secret rule §6
  applies to every other credential here.
- **The bearer is unguessable, and the id is not authority.** Storing a digest
  protects a *strong* bearer and publishes a brute-force target for a weak one:
  a sequential counter, a UUID scheme, a timestamp, or any value derivable from
  the attempt, runner or actor identity the record already publishes satisfies
  every other sentence here and is recoverable offline from the durable digest by
  anyone who reads it. So the bearer is drawn from a cryptographically secure
  random source with **at least 256 bits of entropy** — the named need being
  exactly that stored digest, since a guess costs one hash — and from no other
  source. Because the bearer carries full entropy, a single SHA-256 is a
  sufficient digest and no password-derivation function is wanted; that
  sufficiency is a consequence of the entropy floor, so the two rules travel
  together and neither is weakened alone. The opaque id **identifies** a ticket
  and never authorizes one: redemption requires the bearer whose digest matches,
  and presenting the id alone is refused like presenting nothing.
- **Verification is constant-time, and the bearer is confined.** The stored
  digest is compared against the presented bearer's digest with a constant-time
  comparison, because a byte-wise compare over a stored digest leaks it one byte
  at a time. Raw bearer bytes exist only at issue and at redemption: they cross
  once to the authorized operator client, are compared and discarded, and are
  never re-displayed or recoverable afterwards — an operator who loses a ticket
  authorizes a new attach rather than retrieving the old one.
- **Consumed exactly once, atomically.** Redemption is a compare-and-consume
  against durable state: the first redemption wins and marks the ticket spent in
  the same operation that authorizes the attach; a concurrent second redemption
  loses and refuses (`attach-ticket-consumed`). A read-then-mark sequence is not
  acceptable, because that race is exactly one unauthorized attach.
- **Short lifetime with a named bound.** A ticket's lifetime is deployment
  configuration against a named need (a ticket must not outlive the operator's
  presence at the terminal), and it is **capped** at the earlier of the attach
  session's end and the bound attempt's `attempt_deadline_seconds` (ADR 0008).
  The deadline is the ceiling, not the lifetime: an attempt may legitimately be
  allowed to run for hours, and a ticket valid for hours is a standing key. No
  new constant is minted here, because the duration is configured, not decided.
- **Audit.** Every attach writes a durable record — actor, attempt, runner,
  ticket id, start and end. An attach whose audit record cannot be written does
  not start. The attempt's receipt carries #9's operator-influenced marking as
  that record already requires.

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

**An `agent` actor authenticates with a credential, and a revision id is not
one.** The published revision an agent acted under answers *under which policy*;
it can be read by anyone who can read the catalog, so it proves nothing about
*who called*. An `agent` client therefore holds its own credential under §4's
enrolment shape — one credential per client, a durable enrolment record naming
the enrolling operator, the revisions it may act under, and its revocation state,
with the same three-valued lifecycle. The record binds both facts and never
conflates them: the credential establishes the identity, the revision id records
the policy.

**Delegation is bounded and does not chain.** An `agent` actor never exceeds the
authority of the operator who enrolled it: a command the enrolling operator may
not issue is refused for the agent too, and revoking that operator revokes every
agent enrolled under them. An agent cannot enrol another actor, cannot mint a
credential, and cannot act as an `operator`; an agent that presents an operator
actor is refused (`actor-kind-not-permitted`) rather than downgraded. A run
started by an agent that itself starts runs carries the originating actor
unchanged through the chain, so depth never launders identity.

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
| `unauthenticated-exposure` | a deployment declaring `reachable` exposure, or declaring none, with no composed operator authenticator | host composition |
| `exposure-bind-contradiction` | a deployment declaring `this-machine` exposure bound to a non-loopback address | host composition |
| `runner-transport-mismatch` | a connection whose transport does not match the declared tier | runner connection |
| `runner-peer-unverified` | the runner's per-invocation identity is not established, or the service does not authenticate back, where §2's identity invariant is required | runner binding |
| `runner-unknown` | a runner with no enrolment record requests work or a ticket | runner connection |
| `runner-revoked` | the enrolment record is marked revoked | runner connection |
| `runner-attestation-changed` | the presented runner attestation differs from the enrolled one | runner connection |
| `no-runner-attests-binding` | no connected enrolled runner attests the bound capability, executor, provider mode or auth mode | run start |
| `auth-profile-unresolvable` | the bound credential reference does not resolve on the runner's host | run start |
| `attempt-binding-unknown` | a runner acts on an attempt it was not bound | attempt handoff |
| `attempt-binding-terminal` | a runner acts under a terminal attempt id | attempt handoff |
| `attach-ticket-consumed` | a ticket is redeemed a second time, including concurrently | attach |
| `attach-ticket-invalid` | a redemption presents no bearer, an unissued bearer, or a bearer whose digest does not match the named ticket | attach |
| `actor-kind-not-permitted` | an `agent` actor presents an operator actor, or enrols or delegates | service authorization |
| `runner-report-out-of-scope` | a report carries a disposition, receipt, or catalog mutation | runner report |
| `runner-not-authorized` | a runner attempts a catalog or command operation | service authorization |

Durable failure tokens, where any of these must become one, are minted by #16;
this record borrows that owner rather than opening a second vocabulary.

## Consequences

- The loopback rule stops being a Claude-specific special case and becomes the
  product's general rule: no exposure beyond this machine without an
  authenticated operator. ADR 0004's "safe only on the trusted local boundary"
  gets its named successor, and #7 gets the identity it needs before the
  conductor issues commands.
- A deployment must now state its exposure, and stating none refuses. That is a
  new obligation on every deployment including today's local one, and it is the
  price of not inferring reach from a bind address a proxy can front.
- V1 gains no daemon and no new process of this record's making. The `same-host`
  runner is the execution scope that already exists — the watchdog process today,
  the transient unit under the host's own systemd manager after #15's cutover —
  and only its name and its obligations become explicit here. The record does
  not claim a peer control it lacks: it accepts today's same-UID trust domain in
  writing, names what that costs, and states the boundary past which the
  acceptance ends.
- The identity invariant is written without a transport, so it survives the
  same-host cutover instead of pinning the record to the owner #15 deletes — and
  it cannot be quietly declared satisfied by changing nothing. This record
  commissions no hardening of the condemned predecessor; that work would be
  deleted with it.
- The remote epic is unblocked in rules, not in build: transport, protocol and
  the ownership lease remain to be decided by it.
- Per-runner credentials cost an enrolment ceremony, and the operator must
  enable attach and then authorize each one. Both costs are accepted: a shared
  secret has neither revocation nor attribution, and attach is the only path a
  human reaches into a billed process.

## Required proofs before implementation is accepted

- Composition refuses a deployment declaring `reachable` exposure with no
  operator authenticator, refuses one declaring `this-machine` while bound to a
  non-loopback address, refuses one declaring no exposure at all — and the
  existing `this-machine` loopback composition still succeeds unchanged.
- A forwarded header naming another address changes nothing: it is neither read
  as identity nor as exposure, and the same request is authorized identically
  with and without it.
- An unknown, revoked, or attestation-changed runner receives no attempt binding
  and no attach ticket, and no durable row is written for the refusal path.
- A revoked runner and a never-enrolled one produce **different** refusals from
  durable state, and re-enrolling a revoked id requires an explicit operator act.
- Where §2's identity invariant is required, a runner whose per-invocation
  identity is not established is refused before any attempt binding, and so is a
  service that does not authenticate back — proven against whatever carries that
  deployment's tier, not against a mechanism this record names.
- A runner identity is not satisfied by a reused name: an identifier that
  outlives the runner it named never binds a later attempt.
- Run start refuses a binding no connected runner attests, naming node, binding
  and missing attestation, with no run, binding, attempt, receipt or process.
- A runner report carrying a disposition, receipt, or catalog mutation is
  refused whole and changes nothing durable.
- A full durable and API projection after a fake run contains no credential
  value and no verifier path — the canary shape #58 acceptance 8 already uses.
- Attach without a valid ticket refuses; a failed audit write prevents the
  attach; an attach past the bound attempt's deadline refuses; and a ticket
  redeemed **twice concurrently** succeeds exactly once, the loser refusing with
  `attach-ticket-consumed`.
- No ticket value appears in any durable record, log, event, receipt or API
  resource — the same canary shape the credential proof above uses — and it is
  not retrievable after issue by any surface.
- A redemption presenting the ticket's opaque id with no bearer, with an unissued
  bearer, or with another ticket's bearer refuses (`attach-ticket-invalid`) and
  consumes nothing, proving the id is an identifier and not authority.
- An `agent` actor with no credential is refused; one presenting an `operator`
  actor is refused rather than downgraded; and a command its enrolling operator
  may not issue is refused for it too.
- A runner that disappears while an attempt is armed leaves that attempt
  `POSSIBLY_RAN`, and no second runner ever receives it.
- Two enrolled runners with different attestations place only the bindings each
  attests, proving placement is per runner and not per deployment.

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
record; a ticket value written into durable state, or redeemed by a
read-then-mark sequence instead of an atomic compare-and-consume; a ticket bearer
from anything but a cryptographically secure random source, or below the entropy
floor, or verified by a non-constant-time comparison, or treated as authorized on
its opaque id alone; a runner bound by a reusable name instead of a
per-invocation identity, or a same-host identity mechanism built into the
predecessor lifecycle #15 deletes; a
deployment-authored or probe-derived entry written into ADR 0006's immutable
capability manifest; exposure inferred from the bind address, or a forwarded
header read as identity; a revocation that deletes the enrolment record instead
of marking it; an `agent` actor authorized by a revision id alone; an unplaceable
run that is queued instead of refused; a remote binding published as available
before the ownership contract exists; or an actor field described as attribution
while it is still caller-asserted.

## Supersedes

None.
