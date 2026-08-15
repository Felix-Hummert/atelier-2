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
`AdapterOperationalIdentity` is the exact repository and installation a durable
effect targets, and its `readback`/`execute` are the ones `ports.effects` already
declares. Reusing that owner is what makes GitLab or a board backend a new
adapter and a new operation registry rather than a core change.

Observation opens **one** new port, the only one this record opens: a platform
observation source that takes a durable cursor and returns provider-neutral typed
observed facts plus the next cursor. Its facts name platform-neutral things — an
external item, its state, its labels, its actor, the commit an evidence object
speaks about — so a second platform implements the same port.

### 2. Auth: a GitHub App with one installation, in V1

| | GitHub App | Personal access token |
| --- | --- | --- |
| Identity of an agent action | the App's own actor, distinct from the operator | the operator, indistinguishable from their own hand |
| Scope | per installation, per repository, per permission | the granting user's reach, fine-grained tokens included |
| Credential lifetime | short-lived installation token minted from a private key | long-lived token |
| Setup cost | one operator act: create, install, place the key | paste a token |
| Later webhook identity | delivery identified per installation and signed | none |

**V1 uses a GitHub App with a single installation.** The deciding need is not
scope, it is identity: ADR 0009 §9 requires a typed actor, and an actor is only
readable from the platform if the platform recorded who acted. A PAT structurally
cannot record it — it acts as the operator — which is exactly the failure already
in this repository's history. Simplicity is real and it buys nothing here, because
the one thing V1 needs from GitHub auth is the difference a PAT erases.

- **Installation enrolment is an explicit operator act**, with a durable record
  binding the installation, its repository scope, the reference to its credential,
  and the enrolling actor — the same shape as ADR 0009 §4's runner enrolment, and
  deliberately not a second ceremony. An unenrolled installation performs no
  operation and yields no observation (`platform-installation-unknown`); a revoked
  one likewise (`platform-installation-revoked`).
- **Permissions are requested per named operation**, least privilege: read access
  for the objects observation names, write access only for the objects a published
  Action operation revision creates or updates. A permission with no operation
  behind it is not requested.
- **Multi-user successor**: the same App, with a user-to-server token per operator,
  so each human acts as themselves while agent actions keep the App identity. V1
  builds none of it.
- The operator's own `gh` credential, which the bootstrap harness uses today, is
  **not an Atelier auth mode**. It gets no adapter support and disappears when this
  adapter lands.

### 3. Credentials reach the adapter by reference, never by value

ADR 0009 §6 holds here unchanged; this record only names what the references are.

- The durable secret is the App **private key**. The adapter's host resolves it
  from a credential reference at composition, exactly as
  `ClaudeSubscriptionSettings.credential_directory` already does for the Claude
  adapter. It never appears in a workflow document, prompt, context package,
  event, receipt, log, database row, API resource, crash evidence or test fixture.
- **Installation tokens are derived, never stored.** They are minted in memory,
  refreshed by the client, and persisted nowhere. A durable store of an
  installation token is refused as a design, because it converts a short-lived
  credential into a long-lived one and gives the leak a place to live.
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
  first effect instead of creating its twin.
- **Absence is only authoritative from a strongly consistent read** — a direct read
  of the object, or a listing scoped to the bound repository. The platform's search
  index is eventually consistent, so an empty search is `UNKNOWN`, never
  `AUTHORITATIVE_NOT_FOUND`; an operation offering a search-derived absence is
  refused (`platform-absence-unprovable`). This is where the existing third outcome
  earns its keep: `UNKNOWN` routes to the operator reconciliation command that
  `contracts.effects` already owns.
- **An update is idempotent by content.** A target that already carries the intended
  content hash is `FOUND`, not a second write.
- **Scope is bound.** An operation addressing an object outside the enrolled
  installation's repository scope refuses (`platform-object-out-of-scope`);
  multi-project isolation stays #23's.
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
- **Every observed action carries its actor**, mapped onto ADR 0009 §9's typed
  actor: the App installation is an `agent`, recorded with the published revision it
  acted under; a user is an `operator`. An action whose actor cannot be mapped is
  recorded as unattributed and counted as neither
  (`platform-actor-unattributable`), because #8's fourth rule needs that difference
  and today's record cannot supply it.
- **For #8 the adapter supplies facts, not metrics.** Merged or not, the check
  outcome per head, the review rounds, the platform timestamps between opening and
  merging. It computes no rate, no duration aggregate and no cost; token and cost
  truth stays with the receipts ADR 0008 owns, and the subscription mode reports
  consumption rather than money.

### 7. The client is chosen, not written

A hand-rolled client would own four jobs: signing an App assertion and minting and
refreshing installation tokens, pagination, conditional requests and rate-limit
headers, and webhook signature verification. A maintained client owns them, so
hand-rolling the REST client is refused as a design.

The leading candidate is `githubkit` — typed models generated from GitHub's own
OpenAPI description, App authentication strategies and webhook verification in one
library, on the Pydantic this project already depends on; `PyGithub` is the
alternative. The implementing slice confirms the choice by measuring what the
library deletes and what it makes this project own, and records that measurement.
Whichever is chosen stays inside the adapter under the boundary contract of
decision 1.

## Refusals

| Name | Raised when | Boundary |
| --- | --- | --- |
| `platform-installation-unknown` | an operation or observation names an installation with no enrolment record | adapter composition |
| `platform-installation-revoked` | the enrolment record was removed | adapter composition |
| `platform-credential-unresolvable` | the bound credential reference does not resolve on the adapter's host | run start |
| `platform-object-out-of-scope` | an operation addresses an object outside the enrolled repository scope | operation binding |
| `platform-absence-unprovable` | an operation offers an absence derived from an eventually consistent search | readback |
| `platform-actor-unattributable` | an observed action's actor maps to neither an enrolled installation nor a user | observation |
| `platform-observation-rate-limited` | the platform's limit stops a poll; visible, cursor preserved | observation |

Durable failure tokens, where any of these must become one, are minted by #16;
this record borrows that owner rather than opening a second vocabulary.

## Consequences

- An agent's action becomes distinguishable from the operator's at the source. The
  prose signature stops being the attribution mechanism, #7 gets a readable actor,
  and a disowned verdict becomes visible instead of merged.
- The cost is an operator setup ceremony a token would not need — create the App,
  install it, place the key. Accepted: that identity is the entire reason for the
  choice.
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

- A full durable and API projection after a fake run contains no private key, no
  installation token and no credential path — the canary shape ADR 0009 already
  uses.
- The same Action node re-executed after a crash between send and receipt leaves
  exactly one platform object, found by readback, never a twin.
- A readback that can only search returns `UNKNOWN` and routes to reconciliation;
  no path returns `AUTHORITATIVE_NOT_FOUND` from a search.
- An unenrolled or revoked installation performs no operation and yields no
  observation, and no durable row is written on the refusal path.
- A restart mid-observation resumes from the durable cursor, producing the same
  fact set once, with no gap and no replay.
- A closed-unmerged pull request never reads as merged, and a landing receipt names
  reviewed head, reviewed tree and merge commit.
- A check result observed against another head is refused as evidence for this
  candidate.
- An action by the App installation and one by the operator yield different typed
  actors from the same observation path.
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

Stop implementation on: a personal access token, or any credential that makes an
agent's action wear the operator's identity; a stored installation token; a secret
value in a workflow, prompt, context package, event, receipt, log or API resource;
a create without a prior readback; an absence derived from search; a GitHub
identifier outside the adapter; an agent shell publishing through `gh` instead of
an Action node; a webhook accepted as truth without a readback; a poll interval
hardcoded instead of configured; or a label name minted here.

## Supersedes

None.
