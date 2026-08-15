# ADR 0010: One GitHub adapter observes, publishes and reads back; the core stays platform-blind

- Status: PROPOSED 2026-08-15 — decision only, nothing implemented
- Date: 2026-08-15
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1),
  story 4, whose "GitHub landet einen nativen Flow" and provider/secret rules this
  record expresses and never re-decides
- Decision authority: [Issue #24](https://github.com/FlexOr2/atelier-2/issues/24),
  exact body SHA-256 of the served UTF-8 bytes including their trailing newline
  `67c2e99b04cc00d241430cd636f644016b1c08d4aa3b7b7acb2b9c144f6cea98`
- Depends on: [ADR 0006](0006-node-vocabulary.md) (the adapter-operation contract,
  the core-derived idempotency key and the `external_effects` attestation this
  record fills in, and which routed every platform specific here),
  [ADR 0009](0009-runner-trust.md) (secrets by reference, the typed actor, the
  loopback rule this record's observation choice follows),
  [ADR 0005](0005-enforced-package-boundaries.md) (the boundary gate that keeps
  the client inside the adapter)
- Feeds: [#79](https://github.com/FlexOr2/atelier-2/issues/79) (the queue, whose
  transport this is), [#8](https://github.com/FlexOr2/atelier-2/issues/8)
  (platform events as the hard measurements),
  [#7](https://github.com/FlexOr2/atelier-2/issues/7) (a readable actor)
- Names, never decides, the dependencies owned elsewhere:
  [#79](https://github.com/FlexOr2/atelier-2/issues/79) (queue, priority, ready
  model, automation filter), [#7](https://github.com/FlexOr2/atelier-2/issues/7)
  (conductor assignment), [#23](https://github.com/FlexOr2/atelier-2/issues/23)
  (multi-project isolation), [#16](https://github.com/FlexOr2/atelier-2/issues/16)
  (durable failure vocabulary), [#22](https://github.com/FlexOr2/atelier-2/issues/22)
  (catalog identity), `docs/requirements/README.md` (the requirement revision
  store and trace format)

## Context

Issue #1 story 4 wants one readable chain — requirement, bound head and tree,
pull request, check, review, native squash merge — with no Atelier-built landing
ceremony. Nothing owns how Atelier reaches GitHub: no auth mode, no observation,
no publication path, no readback contract.

The gap already costs. Every one of this repository's issues and every comment on
them, human and agent alike, is authored by `FlexOr2`, the operator's own account,
because the fleet publishes through the operator's `gh` credential and signs its
work in prose. That is not cosmetic: PR #72 was merged on a review verdict the
reviewing instance later disclaimed as not its own, and nothing in the platform
record could have separated the two. #7 needs an actor, #8's fourth anti-gaming
rule needs to tell an operator intervention from an agent's own run, and #79's
automation filter needs to know which items agents may take. All three read the
same missing fact.

Who Atelier acts as, however, is the operator's call rather than an architectural
one, and the operator ruled it on 2026-08-15: they configure the method when they
connect a project. So this record specifies both methods, states plainly what each
one can and cannot prove, and keeps the recognizability the finding above demands
in a place neither method can lose — the content Atelier writes and the receipts
Atelier keeps.

Requirement revisions are hand-rolled the same way: an agent copies an issue body,
computes its SHA-256 and pastes the digest into prose — issue #4's "Requirement
binding" block and this record's own decision-authority line above are both that
convention.

Two owners already exist and must not be duplicated. `contracts.effects` owns the
intent, readback and receipt discipline with its three outcomes, and
`ports.effects` owns the `EffectAdapter` protocol behind them. ADR 0006 owns the
Action node, derives the idempotency key in the core, enumerates proven adapter
operations under `external_effects`, and states outright that no platform
identifier belongs in the core because addressing, authorization and readback are
#24's. Observation has no owner at all.

One constraint is decisive for observation: ADR 0009 refuses any non-loopback bind
until an operator authenticator exists, so this deployment has no reachable
inbound URL today.

## Decision

### 1. One adapter, three surfaces, and a platform-blind core

GitHub enters through exactly one adapter package with three surfaces:
**observation** (read), **publication** (perform an effect), **readback** (prove
what an effect did). No GitHub identifier — repository, issue, pull request,
label, check, review, installation — appears in `contracts`, `ports`,
`application`, `api` or `host`. The client library and every GitHub concept stay
inside the adapter under the same forbidden-import contract shape ADR 0005
already enforces for DBOS and SQLAlchemy; ADR 0005 owns that gate.

Publication and readback open **no new port**. The GitHub adapter is an
`EffectAdapterFactory`: its `EffectDestination` is the platform, its
`AdapterOperationalIdentity` is the exact repository and project connection a
durable effect targets, and its `readback`/`execute` are the ones `ports.effects`
already declares. Reusing that owner is what makes GitLab or a board backend a new
adapter and a new operation registry rather than a core change.

Observation opens **one** new port, the only one this record opens: a platform
observation source that takes a durable cursor and returns provider-neutral typed
observed facts plus the next cursor. Its facts name platform-neutral things — an
external item, its state, its labels, its actor, the commit an evidence object
speaks about — so a second platform implements the same port.

### 2. Auth: the operator chooses the method when a project is connected

**The authentication method is the operator's choice at project-connect time, not
this record's mandate** (operator ruling on #24, 2026-08-15). V1 carries **both**
methods, each fully specified here, and a connected project binds exactly one of
them as a published configuration revision under #1's live-configuration rule.

- **Personal access token — the low-friction path, and V1 must support it.** The
  operator pastes a token when connecting the project and is done. A token scoped
  to the connected repository is the recommended form within this path, because
  narrowing the scope costs the operator nothing at the moment they paste it.
- **GitHub App — the second method, recommended, never forced.** It buys an actor
  identity of its own, per-repository and per-permission scope, short-lived
  installation tokens minted from a private key, and the identity a signed webhook
  delivery and a multi-user successor would need.

| | Personal access token | GitHub App |
| --- | --- | --- |
| Setup | paste a token when connecting the project | create the App, install it, place its key |
| Credential held | the token itself, long-lived until the operator rotates it | a private key, from which short-lived installation tokens are minted |
| Scope | the granting user's reach, narrowable per repository and permission | per installation, per repository, per permission |
| Actor the platform records | the granting user — the operator | the App's own actor, distinct from every human |
| How an Atelier action is recognizable | by the marker Atelier writes into the object's content | by the account that acted, and by the same marker |
| Multi-user successor | none: the connection is one human's reach | user-to-server tokens per operator under the same App |
| Revocation blast radius | rotating the token affects everything that token drives | one installation, revocable alone |

**Whichever method is bound, Atelier marks its own actions.** Every object it
creates or updates carries a machine-readable marker naming Atelier, the run and
the node that produced it — the same marker decision 5 already requires for
idempotency, carrying its attribution alongside — and Atelier's own receipts carry
the typed actor of ADR 0009 §9 regardless of method. So an Atelier action is always
recognizable in content and always attributed in Atelier's own record.

**The honest weakening this record names rather than forbids:** in the token method
the platform itself records the operator as the actor, so the difference between an
agent's action and the operator's own hand lives in the object's content, which any
holder of that account can also write. It is honest labeling, not proof. Two named
consequences follow, and neither blocks the method: the account-level separation
#8's fourth anti-gaming rule and #79's automation filter would like exists only in
the App method, and in the token method those owners read the content marker as a
*claim* with its provenance rather than as platform-proven attribution
(decision 6). That is the tradeoff the operator is choosing, stated where they can
see it.

- **Connecting a project is an explicit operator act**, with a durable record
  binding the project, the chosen method, the reference to its credential, the
  repository scope and the connecting actor — the same shape as ADR 0009 §4's
  runner enrolment, and deliberately not a second ceremony. An unconnected project
  performs no operation and yields no observation
  (`platform-connection-unknown`); a revoked connection likewise
  (`platform-connection-revoked`).
- **Scope is requested per named operation**, least privilege in both methods: read
  access for the objects observation names, write access only for the objects a
  published Action operation revision creates or updates. A permission with no
  operation behind it is not requested, and in the PAT path this is a
  recommendation the connect surface presents rather than a rule the platform can
  enforce for the operator.
- **The multi-user successor exists only on the App path** — user-to-server tokens
  per operator, so each human acts as themselves while agent actions keep the App
  identity. A PAT-connected project is single-operator by construction. V1 builds
  neither.
- **An ambient credential is never an auth method.** The adapter resolves the
  reference its project connection names, and never a token that merely happens to
  be in the environment or in a CLI's configuration — including the `gh` credential
  the bootstrap harness uses today. Connecting is a decision, not an inheritance.

### 3. Credentials reach the adapter by reference, never by value

ADR 0009 §6 holds here unchanged and **identically for both methods**; this record
only names what the references are.

- The durable secret is the connection's credential — the **token** in the PAT
  method, the App **private key** in the App method. The adapter's host resolves it
  from a credential reference at composition, exactly as
  `ClaudeSubscriptionSettings.credential_directory` already does for the Claude
  adapter. Neither ever appears in a workflow document, prompt, context package,
  event, receipt, log, database row, API resource, crash evidence or test fixture.
  The operator pastes a token into the credential channel, never into a project
  record, an issue, or a workflow.
- **Installation tokens are derived, never stored.** In the App method they are
  minted in memory, refreshed by the client, and persisted nowhere. A durable store
  of an installation token is refused as a design, because it converts a
  short-lived credential into a long-lived one and gives the leak a place to live.
- **A long-lived token is the PAT method's accepted cost**, and it is bounded where
  Atelier can bound it: the credential channel holds it, rotation and expiry stay
  the operator's, and revoking it is one operator act on the platform. Atelier
  neither copies it nor extends its life.
- A bound credential reference that does not resolve refuses at run start
  (`platform-credential-unresolvable`), with no fallback to another auth mode.
- **Raw platform responses do not land durably.** ADR 0006 already has the rule:
  the operation revision declares its typed readback projection and the core
  carries that projection as an opaque hashed payload. The adapter never writes a
  raw provider frame into a receipt, per #1's frame rule.

### 4. Item observation: polling in V1, a webhook as a hint later

**V1 polls.** The reason is not effort: ADR 0009 refuses a non-loopback bind until
an operator authenticator exists, so there is no address a delivery could reach.
Webhooks are unreachable today, and a decision record that chose them would be
choosing an architecture the deployment cannot run.

- **The interval is configuration, not a constant this record mints.** It is
  live-versioned per project under #1's configuration rule, and the need that
  bounds it is the queue's ready latency — so #79 sets it with its own acceptance,
  where the need is stated.
- **A durable observation cursor per observed collection** makes restart honest: a
  restart neither re-observes from the beginning nor skips. Observation is
  at-least-once, and an observed fact is identified by the platform object and its
  own revision marker, so a repeated observation is the same fact rather than a
  second one.
- **Conditional reads.** The adapter uses the platform's own change markers —
  entity tags and update cursors — so a poll that learns nothing costs nothing it
  does not have to.
- **Rate limiting is visible, never silent.** Exhaustion degrades observation with
  a named projected reason and loses no cursor
  (`platform-observation-rate-limited`). It never fabricates an absence and never
  quietly stops.
- **The successor is a hint, not a second truth.** When ADR 0009's remote tier and
  authenticated bind exist, a webhook delivery is accepted only after signature
  verification and only as a *hint*: the adapter reads the object back before
  anything durable is written. Truth therefore stays on the one readback path,
  which is also why missed, duplicated or replayed deliveries need no replay
  machinery.
- **Observation starts nothing.** It produces observed facts. Which item becomes
  ready, and what runs, is #79's and the scheduler's.

### 5. Publication: only through an Action node, and always readback-then-create

- **Every create or update on the platform is an Action node** bound to a versioned
  GitHub adapter-operation revision (ADR 0006). An agent shell reaching `gh` is not
  a publication path. This is the line the fleet crosses today, and it is enforced
  by the boundary rather than by instructions, per #1's rule that prompts are not
  controls.
- **The core derives the idempotency key** (ADR 0006: run, node, operation revision
  and intent hash); an author never writes one. GitHub's API offers no idempotency
  key of its own, so the adapter **carries the effect's request hash as a
  machine-readable marker in the object it creates**, and `execute` is always
  readback-then-create. The marker is what lets a re-attempt after a crash find the
  first effect instead of creating its twin. It is the same marker that carries
  decision 2's attribution, so an Atelier-created object is recognizable as
  Atelier's under either auth method, and an object created without it is a defect
  rather than an untracked effect.
- **Absence is only authoritative from a strongly consistent read** — a direct read
  of the object, or a listing scoped to the bound repository. The platform's search
  index is eventually consistent, so an empty search is `UNKNOWN`, never
  `AUTHORITATIVE_NOT_FOUND`; an operation offering a search-derived absence is
  refused (`platform-absence-unprovable`). This is where the existing third outcome
  earns its keep: `UNKNOWN` routes to the operator reconciliation command that
  `contracts.effects` already owns.
- **An update is idempotent by content.** A target that already carries the intended
  content hash is `FOUND`, not a second write.
- **Scope is bound.** An operation addressing an object outside the connected
  project's repository scope refuses (`platform-object-out-of-scope`), whichever
  method holds the credential; multi-project isolation stays #23's.
- **Requirement-revision publication stops being hand-rolled.** The adapter observes
  the requirement issue and publishes an immutable revision from the exact served
  UTF-8 body bytes and their SHA-256 — the same canonical rule the fleet applies by
  hand, computed once by the adapter instead of pasted into prose, and carrying the
  object identity and the read's change marker as provenance. This record decides
  what is observed, what is hashed and what identity it carries; the durable
  revision store and the trace format stay with `docs/requirements/README.md`.

### 6. Readback semantics: what merged, closed and labeled mean

One rule governs all of them, and it is ADR 0009 §5's rule rather than a second
one: **an observed platform fact is evidence** carrying its provenance — the object
identity, the exact observed fields, the read time and the platform's change
marker. Only the core writes a verdict.

- **`merged` is not `closed`.** Merged is the pull request's merged flag together
  with its merge commit. A closed-unmerged pull request is a distinct terminal fact
  and is never read as success.
- **A landing receipt binds both objects.** Native squash merge is authoritative
  (#1), so the reviewed head and tree are not the landed commit. The receipt names
  the reviewed head and tree *and* the resulting merge commit, because #1 requires
  a receipt to say which object was checked and which commit landed — which is
  exactly what the fleet writes by hand in a PR body today.
- **`closed` on an item carries no reason alone.** The observed fact is the pair of
  state and state reason. What that pair means for a queue outcome is #79's.
- **`labeled` is an observed set at a read, and this record mints no label name.**
  The repository carries only the platform's default labels today; the automation
  filter's vocabulary belongs to #79, and inventing one here would be a constant
  with no named need.
- **A check or a review is evidence about exactly one commit.** A check result or an
  approval observed against a different head is not evidence about this candidate,
  because an approval goes stale the moment a new commit is pushed.
- **Every observed action carries its actor, and how strongly it is proven.** In the
  App method the platform's own actor maps onto ADR 0009 §9's typed actor: the
  installation is an `agent`, recorded with the published revision it acted under,
  and a user is an `operator`. In the PAT method the platform records the operator
  for both, so the adapter reads decision 2's content marker as an `agent` **claim**
  and records it as such — a claim with its provenance, never platform-proven
  attribution. The observed fact therefore carries the actor *and* whether the
  account or only the content established it, so #8's fourth rule and #79's filter
  can decide what they trust instead of being told a claim is a proof. An action
  that maps to neither, and carries no marker, is recorded as unattributed and
  counted as neither (`platform-actor-unattributable`).
- **For #8 the adapter supplies facts, not metrics.** Merged or not, the check
  outcome per head, the review rounds, the platform timestamps between opening and
  merging. It computes no rate, no duration aggregate and no cost; token and cost
  truth stays with the receipts ADR 0008 owns, and the subscription mode reports
  consumption rather than money.

### 7. The client is chosen, not written

A hand-rolled client would own four jobs: authenticating both methods — a pasted
token, and an App assertion whose installation tokens must be minted and refreshed
— pagination, conditional requests and rate-limit headers, and webhook signature
verification. A maintained client owns them, so hand-rolling the REST client is
refused as a design.

The leading candidate is `githubkit` — typed models generated from GitHub's own
OpenAPI description, both token and App authentication strategies, and webhook
verification in one library, on the Pydantic this project already depends on;
`PyGithub` is the alternative. Carrying both auth methods behind one client is
itself part of the measurement, since a library that covers only one of them
leaves the other hand-rolled. The implementing slice confirms the choice by measuring what the
library deletes and what it makes this project own, and records that measurement.
Whichever is chosen stays inside the adapter under the boundary contract of
decision 1.

## Refusals

| Name | Raised when | Boundary |
| --- | --- | --- |
| `platform-connection-unknown` | an operation or observation names a project with no connection record | adapter composition |
| `platform-connection-revoked` | the connection record was removed | adapter composition |
| `platform-credential-unresolvable` | the bound credential reference does not resolve on the adapter's host | run start |
| `platform-object-out-of-scope` | an operation addresses an object outside the connected repository scope | operation binding |
| `platform-absence-unprovable` | an operation offers an absence derived from an eventually consistent search | readback |
| `platform-actor-unattributable` | an observed action maps to no known actor and carries no Atelier marker | observation |
| `platform-observation-rate-limited` | the platform's limit stops a poll; visible, cursor preserved | observation |

Durable failure tokens, where any of these must become one, are minted by #16;
this record borrows that owner rather than opening a second vocabulary.

## Consequences

- The operator connects a project in the way they choose, and the low-friction path
  is a first-class one rather than a concession. Neither method is a different
  product: the same operations, receipts, refusals and secret rules hold on both.
- An Atelier action stops being recognizable only by a prose signature. In both
  methods it carries a machine-readable marker and a typed actor in Atelier's own
  receipts; in the App method the account proves it too. The difference between the
  methods is how strong that attribution is, and the record says which one a
  consumer is looking at instead of flattening the two.
- The disowned-verdict failure is therefore mitigated in both methods and closed
  only in the App one. Naming that honestly is the point: a consumer that needs
  account-level proof — #8's fourth rule, a future multi-user path — knows it must
  ask for the App method rather than discovering the gap later.
- V1 gains a poll loop and one new port. It gains no inbound surface, no new
  process and no new trust boundary; ADR 0009's remains the only one.
- The fleet's hand-rolled patterns become machine truth: a claim or verdict comment
  becomes a published effect with a receipt, and a requirement digest becomes an
  observed revision instead of a pasted string.
- #79 can be built on observed facts without inventing a queue-shaped platform
  contract, and #8 gets its hard measurements with provenance rather than
  self-reports.
- Polling bounds how fresh the queue can be, and that bound is configuration
  answering to #79's acceptance rather than a number chosen here.

## Required proofs before implementation is accepted

- A full durable and API projection after a fake run contains no token, no private
  key, no installation token and no credential path — the canary shape ADR 0009
  already uses, run once per auth method.
- Both auth methods perform the same operations, produce the same receipt shape and
  raise the same refusals; no operation is available in one method only.
- The same Action node re-executed after a crash between send and receipt leaves
  exactly one platform object, found by readback, never a twin.
- A readback that can only search returns `UNKNOWN` and routes to reconciliation;
  no path returns `AUTHORITATIVE_NOT_FOUND` from a search.
- An unconnected or revoked project performs no operation and yields no
  observation, and no durable row is written on the refusal path.
- Every object Atelier creates carries its marker, in both methods; an object
  created without one fails the proof.
- A restart mid-observation resumes from the durable cursor, producing the same
  fact set once, with no gap and no replay.
- A closed-unmerged pull request never reads as merged, and a landing receipt names
  reviewed head, reviewed tree and merge commit.
- A check result observed against another head is refused as evidence for this
  candidate.
- In the App method an action by the installation and one by the operator yield
  different typed actors from the same observation path; in the PAT method the same
  path records the marker-derived actor as a claim and never as platform-proven.
- No module outside the GitHub adapter imports the client library or names a GitHub
  concept, proven by the ADR 0005 boundary gate.
- A rate-limit exhaustion is visible in the projection and loses no cursor.

## Out of scope and stop conditions

This record does not decide: the queue, priority, ready model, automation filter
and its label vocabulary (#79); conductor assignment (#7); any non-GitHub tracker,
for which decision 1's port shape is the seam and nothing more; multi-project
isolation (#23); the durable requirement-revision store and trace format
(`docs/requirements/README.md`); durable failure tokens (#16); catalog identity
(#22); cost, pricing and quota (ADR 0008); the remote transport and inbound surface
a webhook needs (ADR 0009 and #9 part 3).

Stop implementation on: an auth method hardcoded instead of chosen per project
connection, or either method built as a second-class path with fewer operations; a
marker-derived actor presented as platform-proven attribution; an object Atelier
creates without its marker; a stored token or installation token; a secret value in
a workflow, prompt, context package, event, receipt, log or API resource; a create
without a prior readback; an absence derived from search; a GitHub identifier
outside the adapter; an agent shell publishing through `gh` instead of an Action
node; a webhook accepted as truth without a readback; a poll interval hardcoded
instead of configured; or a label name minted here.

## Supersedes

None.
