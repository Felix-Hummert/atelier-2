# ADR 0009: One trust boundary separates the coordinating service from every worker

- Status: PROPOSED 2026-08-15; amended 2026-08-21; disposable #301-A candidate 2026-08-22 — no live Runner availability
- Date: 2026-08-15
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
- Decision authority: [Issue #21](https://github.com/FlexOr2/atelier-2/issues/21),
  intended final served `#21 body @ 3c1f663cd51a1c7aedbeffc39c3f38ee2ed6174d16103ab68d9d811014352ed0`
  — 7,961 UTF-8 bytes, ending in one LF byte. This candidate binds only when that
  exact body is read back; until then the current served body remains the
  authority. The prior rebind and derived-document debt are recorded in
  [#21 comment 5354779824](https://github.com/FlexOr2/atelier-2/issues/21#issuecomment-5354779824).
  The operator-owned architecture ruling is
  [#5 comment 5354196886](https://github.com/FlexOr2/atelier-2/issues/5#issuecomment-5354196886);
  the canonical terminology and owner map are
  [#9 comment 5354522420](https://github.com/FlexOr2/atelier-2/issues/9#issuecomment-5354522420)
  and the amended [#9 body](https://github.com/FlexOr2/atelier-2/issues/9), whose
  rebind is recorded in
  [comment 5354786342](https://github.com/FlexOr2/atelier-2/issues/9#issuecomment-5354786342).
  This record owns #21's trust mandate and **does not close #21**: the local
  carrier decision is bounded below; remote/CI remains its separate stop-gate.
- Depends on: [ADR 0001](0001-durable-runtime.md) (process ownership, attempt
  states), [ADR 0003](0003-http-api.md) (the control surface this record
  authenticates), [ADR 0004](0004-local-cockpit.md) (the local-only boundary this
  record replaces), [ADR 0006](0006-node-vocabulary.md) (attestation vocabulary,
  reused and never duplicated), [ADR 0008](0008-budget-units.md) (the attempt
  deadline this record reuses as a lifetime bound)
- Feeds: [#7](https://github.com/FlexOr2/atelier-2/issues/7) (actor attribution),
  [#9](https://github.com/FlexOr2/atelier-2/issues/9) (operator-facing epic;
  remote execution, Effect Worker and Remote Attach remain separate deliveries),
  [#60](https://github.com/FlexOr2/atelier-2/issues/60) (sandbox probe as
  attested state)
- Names, never decides, the dependencies owned elsewhere:
  [#16](https://github.com/FlexOr2/atelier-2/issues/16) (durable failure
  vocabulary), [#23](https://github.com/FlexOr2/atelier-2/issues/23)
  (multi-project isolation), [#58](https://github.com/FlexOr2/atelier-2/issues/58)
  (workspace lease), [#15](https://github.com/FlexOr2/atelier-2/issues/15)
  (attempt state, fencing, terminal-evidence acceptance and reconciliation),
  [#301](https://github.com/FlexOr2/atelier-2/issues/301) (the deployable Atelier
  Runner, executor adapters and containment), and
  [#312](https://github.com/FlexOr2/atelier-2/issues/312) (separate Serve/Runner
  artifacts, deployment, migration and cutover)

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
keystrokes into a credential-bearing process is gated. #9's remote surfaces are
blocked on this record, #7 needs an actor before commands can be attributed, and two
vision-panel lenses marked the gap CRITICAL with no owner.

The operator directive of 2026-08-15 (#1) sharpens the shape: agents are
default-capable and restricted by their definition, and fail-closed staging
stays exactly where the trust concerns are — credentials, billing, sandbox. This
record is that boundary, so strictness here follows the directive rather than
contradicting it.

The 2026-08-20 ruling resolved a later contradiction in the planned local
carrier. The live watchdog/exec-guard path and the subsequently planned direct-
systemd manager are predecessors, and a one-container Serve-plus-execution
deployment is rejected rather than selectable. Their landed history remains in
the owning issues; this record retains only the deletion fact needed to prevent
any of them from returning as a fallback.

**How the decision-authority digest above is computed.** The canonical rule is
exact: the bytes the API serves as the issue body, hashed as they are, with
nothing appended, re-encoded or normalized. The current body itself ends in one
LF byte. This is stated because this record's first revision got the digest
wrong: a shell pipeline (`gh ... --jq .body`) appended another newline before
hashing and therefore bound bytes GitHub never served. Until ADR 0010's adapter
publishes requirement revisions and computes this digest once instead of a human
pasting it, every record here states the byte count and terminal-byte fact so a
reader can re-derive it rather than trust it.

## Decision

### 1. Core owns truth; workers own one bounded operation

**Atelier Core / Serve** owns scheduling, ready-set computation, attempt and
generation CAS, canonical artifacts, events, dispositions, receipts, retry,
resume, cancel and reconciliation. It is the only writer of product truth.

The **Atelier Runner** is a deployable Agent worker. One Runner invocation
executes exactly one leased `AgentAttempt`, identified by its attempt, request,
generation/invocation and pinned Runner-manifest identity. It hosts the Agent
Executor Adapters for Claude, Claude-tools, Codex, Grok and Grok-tools, and owns
provider CLI and local credential resolution, ephemeral workspace
materialization and containment, bounded collection and terminal evidence. A
provider process is its child, not a Runner; the existing `AgentProcessRunner`
port is a lower, Serve-local predecessor, not this boundary.

An **Effect Worker** is a separate worker role for one prepared `EffectIntent`.
It hosts the existing Effect Adapter contract under an operation-scoped grant
and returns effect evidence; only Core commits an `EffectReceipt` or
`WAITING_RECONCILIATION`. It never shares the Agent Runner's identity,
credential, environment or privilege lane. Wait, Join, Resume and deterministic
or subworkflow scheduling remain in Core and are not workers.

Serve and Runner are separate OCI images and release artifacts under #312.
Serve contains no provider CLI or provider credential value and receives no raw
carrier or OCI lifecycle authority. The Runner writes evidence, never product
truth; native carrier logs or artifacts may transport evidence or provenance
but never become the canonical store.

### 2. Every carrier enforces the same identity boundary; the first local form is decided

A **carrier / host** supplies the execution environment in which a worker runs:
local OCI, a remote machine, GitHub Actions or GitLab CI. It is neither an
Atelier Runner adapter nor a second scheduler or store of record. The selected
path is deployment state; work or evidence arriving by another path is refused
(`runner-transport-mismatch`). For remote/CI, who holds launch, cleanup and
lifecycle authority remains the separate open decision below.

Before Core binds work or accepts evidence, it establishes the exact worker
invocation it authorized. The worker authenticates Core in the same act. The
transport must authenticate both peers, authorize each operation for exactly its
attempt/generation and worker role, and make replay and idempotency explicit. A
reused name, path, job label or carrier identity never substitutes for the
per-invocation identity (`runner-peer-unverified`).

For the first local Agent Runner, rootful Docker Engine/Compose is the carrier.
The host launcher alone owns Runner-container launch, stop and cleanup; Core owns
none of that authority. A Runner owns only its provider child, including start,
TERM→KILL, reap and journal. Each Attempt gets one hardened, non-privileged
Runner container and one internal private network. Core may join the needed
Attempt networks; Runner containers from different Attempts may not reach one
another. The exact proof surface is read-only root, `cap-drop=ALL`,
`no-new-privileges`, an unprivileged user, a PID limit, no published port, no
Docker socket and no Docker, project, home, workspace or general host mount. The
host launcher may inject only exact per-invocation identity material read-only.
The read-only harness-code bind mounts in the disposable witness are witness-only
test plumbing, not a production mount form. It proves only the local carrier
boundary, not provider egress, `LAUNCH_ARMED`, cancellation, crash/host-loss or
remote/CI.

Core and Runner mutually authenticate with X.509. The Runner client leaf carries
one exact URI-SAN binding of Attempt, Request, Generation, Invocation and
Runner-manifest identity; both peers check their expected peer, CA, EKU and the
complete binding before an operation. Server and client EKUs differ. This is an
X.509-SAN contract; no identity-framework literal is part of it. Host-managed
keys are mode 0600 and read-only-mounted only into their matching disposable
container; Core has only its own leaf/key and the CA. Issuance, rotation,
revocation and retention remain external operator CA responsibilities, not an
Atelier PKI.

Rootful Docker daemon, host launcher, host and operator CA are the local TCB.
Remote/CI carrier, lifecycle authority and mutual authentication remain **OPEN
on #21**. They stay refused until #15-B proves carrier-neutral crash, cancel and
readback behavior; then #9 owns GitHub first and GitLab parity. Mounting a
Docker/OCI socket, systemd or DBus into Serve, introducing a privileged broker,
or running privileged systemd in a container is not an admissible placeholder.
The first CI proof hosts one Atelier Runner job for one AgentAttempt; it does not
compile the Atelier DAG into native CI jobs. A later Effect Worker remains a
separately authorized job.

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

### 4. Long-lived Runners enrol; ephemeral CI jobs use a CI TrustPolicy

Every long-lived remote Runner holds **its own** credential. A shared fleet
secret is refused: it cannot be revoked for one host and names no actor.
Enrolment is an explicit operator act with a durable record binding the Runner
id, carrier tier, credential-verifier reference, enrolling actor and Runner
attestation (§7). An unenrolled Runner receives no attempt binding or attach
ticket (`runner-unknown`); a revoked one likewise (`runner-revoked`). Core and
Runner mutually authenticate in the same handshake.

An ephemeral CI job is not manually enrolled as a long-lived Runner. Instead,
the operator enrols one narrow **CI TrustPolicy**: pinned OIDC issuer, immutable
repository or project identity, exact workflow or configuration identity, and
protected ref or environment. One unique workflow-run/job assertion may be
exchanged only once for a short-lived credential bound to one attempt,
generation and worker role. A differing claim or replay is refused. This policy
admits the job identity; it does not make CI a scheduler, truth owner or
capability author.

Agent Runner and Effect Worker credentials are role-separated and mutually
unusable. A CI Agent proof therefore grants no Effect operation and carries no
ambient repository-mutation credential; a later Effect Worker proof receives
only its prepared intent and operation-scoped grant.

**Revocation marks the enrolment record revoked; it never deletes it.** A
deleted record makes a revoked runner indistinguishable from one that was never
enrolled, which would collapse `runner-revoked` into `runner-unknown` and erase
the fact an incident needs most: that this runner id was trusted, and when it
stopped being. So enrolment state is durable and three-valued — absent, enrolled,
revoked — carrying the revoking actor and the revocation time. Re-enrolling a
revoked runner id is an explicit operator act with a fresh credential and a fresh
attestation (§7), never a silent return to service. Revocation stops new
bindings; it asserts nothing about an attempt already in flight (§10).

### 5. What an Agent Runner may do, and what no worker may do

An Agent Runner **may** accept exactly one lease bound to immutable attempt,
request, generation/invocation, executor and Runner-manifest identities after
Core's compare-and-set to `LAUNCH_ARMED` (ADR 0001; Core arms, never the Runner).
It may launch, supervise, cancel and reap that provider process, resolve the
credential reference locally (§6), and report bounded observations and terminal
evidence. Identical delivery returns the same invocation; a different binding
conflicts.

No worker may do any of the following; Core enforces each prohibition rather
than trusting the worker:

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

### 6. Provider credentials reach a runner by reference, never by value

Core transmits the auth-profile revision and a logical credential *reference*;
it transmits neither the value nor a Serve-local host path. The Runner resolves
the reference from its own credential source. The current prepared-path seam is
predecessor implementation owned for deletion by #301, not the target contract.
A Runner that cannot resolve the bound reference refuses before provider start
(`auth-profile-unresolvable`) with no fallback to another auth mode. Atelier is
never a secret-distribution channel.

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
  attest, as ADR 0008's meter revision already pins Claude's measured CLI
  versions. A host whose observed version is outside the attested ones has a
  changed attestation, not a usable capability;
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

For an ephemeral CI Runner, the CI TrustPolicy and unique job assertion replace
the long-lived enrolment record, not the attestation. The bound Runner manifest,
executor identity and measured carrier facts still accompany the one-attempt
credential; the assertion cannot author or widen ADR 0006 capabilities.

**Placement is the half this record adds.** ADR 0006 refuses a capability the
bound runtime capability revision does not attest. Run start now also refuses a
binding that **no connected, authorized Runner** attests — authorization is a
long-lived enrolment or an ephemeral CI TrustPolicy exchange —
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

An ephemeral CI TrustPolicy grants no attach capability. V1 attach stays local
(#9 part 2). This record does not open remote attach; it states what remote
attach must present when its epic runs.

### 9. Every command carries a typed, authenticated actor

An actor is a typed identity, not a free string, with exactly three kinds:

- `operator` — an authenticated operator principal (§3);
- `agent` — the conductor (#7) or any client acting under a published
  configuration or policy revision, recorded together with that revision id and
  the operator who enrolled it;
- `worker` — an authenticated Agent Runner or Effect Worker together with its
  exact role; it reports evidence only and never issues a command that changes
  catalog or verdict.

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
- Before `LAUNCH_ARMED`, a lost lease may be assigned again only when
  authoritative no-launch evidence proves that no provider or Effect operation
  began. At or after `LAUNCH_ARMED`, silence is not evidence about the external
  operation: the attempt remains `POSSIBLY_RAN` (ADR 0001), and Core never
  replaces, replays or re-places it. Revocation stops new bindings and resolves
  nothing already in flight.
- The lease, launch fence, terminal-evidence acknowledgement and reconciliation
  protocol belong to #15. Until that protocol and the #21 carrier decision are
  implemented, remote and CI bindings are represented but refused as
  unavailable rather than advertised as working.

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
| `no-runner-attests-binding` | no connected authorized Runner attests the bound capability, executor, provider mode or auth mode | run start |
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
- Serve and Runner ship as separate artifacts. Provider tools and credentials
  leave Serve; raw carrier lifecycle authority never enters it. #312 proves the
  exact artifacts and cutover, rather than this record duplicating that plan.
- The identity invariant is carrier-neutral. #21 decides the first local form:
  rootful Docker Engine/Compose, host-launcher container lifecycle, per-Attempt
  hardened containers and private networks, and X.509 mutual authentication.
  Rootful Docker, host launcher, host and operator CA remain the local TCB.
  Remote/CI stays an explicit later decision rather than inheriting this form.
- Long-lived Runner credentials cost an enrolment ceremony. Ephemeral CI jobs
  instead cost one narrow TrustPolicy and one short-lived, one-attempt credential
  per unique job. Neither path accepts a shared fleet secret.
- Agent Runner and Effect Worker are separate privilege lanes. CI may carry
  either one as a job, but never turns the Atelier DAG, artifacts or receipts
  into CI-owned truth.
- The phased implementation and deletion ledger live on #15, #301 and #312:
  `#15-A → #301-A → #15-B → #301-B → #312 → Deletion`. This ADR owns the
  invariant and links the plan rather than copying it.
- The static one-network form in #301-A is disposable test composition only.
  #312 owns dynamic per-Attempt network creation, drift refusal and cutover; no
  local live installation changes before that owner reaches its own gate.

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
- Where remote/CI needs §2's identity invariant, a runner whose per-invocation
  identity is not established is refused before any attempt binding, and so is a
  service that does not authenticate back — proven against the mechanism of that
  remote/CI tier.
- The bounded current local live-host witness records the two expected Core/Runner
  peers authorizing, exact expected client URI-SAN binding, same-CA wrong-URI
  identity refusal before an operation, wrong-EKU TLS refusal, cross-Attempt
  network probes unreachable, and removal of one Attempt's Runner while Core and
  the other Runner keep running. It does not execute a wrong-CA or wrong-server-
  identity case. Its `result.md` SHA-256 is
  `9c4d962b2bb1dfb3c1dc152979998b4c5297e102d8fedc8416e4c1c787d39da5`
  with successful transcript SHA-256
  `9cc5704b8273c431879695735a38045d8893adcd5ca4f6c015a1f6deadfbac04`,
  manifest SHA-256
  `952eb84623cd20fcbb1dc555a255689f020d7644bd952f1872675c16ac3c73a9`
  and external cleanup proof SHA-256
  `7eaf668be6129fcf78fe46eb25d58e18e14a3b0df14cc0f83cb73edc842eef0f`.
- A disposable #301-A candidate (`scripts/runner_candidate.sh`) on this host
  proved one success Attempt `SUCCEEDED`/`ACKNOWLEDGED` with generation and
  invocation bound, and one cancel Attempt `CANCELLED`/`ACKNOWLEDGED` with
  `replacement=NONE` and `REAPED_AFTER_KILL`. Witness directories
  `/var/tmp/atelier2-301a-runner-witness.6ZYyis` (success Core store SHA-256
  `4519cfd6e06894266a189785dba5134534214d056fb9caf6b9cd4b19d3194035`) and
  `/var/tmp/atelier2-301a-runner-witness.zAP3aP` (cancel Core store SHA-256
  `0100986aaafe4cbc581453fb10fee3feda344cf236ea39654d833f562120c155`). Exact
  labelled Docker objects were empty after `RELEASED`. It does not prove live
  A.1 availability, restart/reconnect, cancel races, replacement `ONE`, a
  wrong-CA live refusal, or packaged cutover. Focused tests cover peer
  EKU/SAN/CA refusal, Landlock identity denial, journal ACK/RELEASE order,
  and the A request-subset/refusal vocabulary.
- A runner identity is not satisfied by a reused name: an identifier that
  outlives the runner it named never binds a later attempt.
- A CI assertion with the wrong issuer, repository/project, workflow/config,
  ref/environment or unique job identity receives no credential or work. The
  same assertion replayed after exchange also receives neither.
- A short-lived credential is usable only for its exact attempt, generation and
  worker role. Agent Runner and Effect Worker credentials are mutually unusable,
  and neither carries ambient repository-mutation authority.
- The inspected Serve artifact contains no provider CLI or credential and has no
  Docker/OCI socket, systemd/DBus or other raw carrier authority. The separately
  identified Runner artifact is the only Agent-execution image.
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
- Before `LAUNCH_ARMED`, reassignment succeeds only with authoritative no-launch
  evidence. At or after `LAUNCH_ARMED`, a disappearing Runner leaves the attempt
  `POSSIBLY_RAN`, and no second Runner ever receives it.
- Two enrolled runners with different attestations place only the bindings each
  attests, proving placement is per runner and not per deployment.
- Deleting or expiring native CI logs and artifacts removes no canonical
  artifact, receipt or reconciliation evidence from Core.

## Out of scope and stop conditions

This record decides only the local rootful Docker form described in §2. It leaves
**OPEN on #21** the first remote/CI carrier, its launch/cleanup authority and
its mutual-authentication mechanism. It also does not decide transport framing;
the environment-requirements vocabulary; multi-project or multi-tenant isolation
(#23); the operator-credential storage backend or cockpit login surface; the
provider-side sandbox mechanism (#60); durable failure token names (#16); rate
limiting or quota. #15 owns lease/fencing/evidence acknowledgement and
reconciliation; #301 owns the Agent worker; #312 owns dynamic per-Attempt
networks, packaging and cutover; #9 owns the operator-facing epic and remote
attach.

Stop implementation on: a shared runner secret; a runner writing a receipt,
disposition, or catalog revision; a provider credential value crossing the
service in either direction; a worker-identity credential written into durable
state, logs or carrier artifacts; an attach path without a per-attach ticket and
an audit record; a ticket value written into durable state, or redeemed by a
read-then-mark sequence instead of an atomic compare-and-consume; a ticket bearer
from anything but a cryptographically secure random source, or below the entropy
floor, or verified by a non-constant-time comparison, or treated as authorized on
its opaque id alone; a worker bound by a reusable name instead of a
per-invocation identity; a shared Agent Runner/Effect Worker credential or
privilege lane; an ephemeral CI job manually enrolled as a long-lived Runner; a
carrier assertion accepted outside its pinned TrustPolicy; raw Docker/OCI,
systemd/DBus or privileged-broker authority entering Serve; a
deployment-authored or probe-derived entry written into ADR 0006's immutable
capability manifest; exposure inferred from the bind address, or a forwarded
header read as identity; a revocation that deletes the enrolment record instead
of marking it; an `agent` actor authorized by a revision id alone; an unplaceable
run that is queued instead of refused; a remote binding published as available
before the ownership contract exists; an actor field described as attribution
while it is still caller-asserted; local #301-A mutating a live installation;
or remote/CI carrier-bound implementation beginning before #21 records its
separate open decision.

## Supersedes

No other ADR. This record's 2026-08-20 amendment supersedes its own watchdog and
direct-systemd target descriptions and records rejection of the one-container
target; the compact context note above is retained only as migration history.
