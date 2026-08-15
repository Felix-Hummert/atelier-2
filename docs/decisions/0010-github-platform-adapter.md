# ADR 0010: One GitHub adapter observes, publishes and reads back; the core stays platform-blind

- Status: PROPOSED 2026-08-15 — decision only, nothing implemented
- Date: 2026-08-15
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1),
  story 4, whose "GitHub landet einen nativen Flow" and provider/secret rules this
  record expresses and never re-decides — body read under decision 5's canonical
  rule, 25337 bytes, no trailing newline,
  `db17a54944498ec4e5f8c456b44771b8e1b9301b9b622d47ca11b1a830cf2b66`
- Decision authority: [Issue #24](https://github.com/FlexOr2/atelier-2/issues/24),
  SHA-256 over the exact served UTF-8 body bytes with nothing appended — 519 bytes,
  no trailing newline —
  `0104406ed3ecbee20caba9be436defd79b0e932e122075012b162dd44ca14087`, which poses
  the open decisions; the auth-method question is ruled by the operator in
  [comment 5302051551](https://github.com/FlexOr2/atelier-2/pull/81#issuecomment-5302051551),
  and the three token-method consequences of decisions 5 and 6 by the panel ruling
  in [#1 comment 5302114585](https://github.com/FlexOr2/atelier-2/issues/1#issuecomment-5302114585)
- Depends on: [ADR 0006](0006-node-vocabulary.md) (the adapter-operation contract,
  the core-derived idempotency key and the `external_effects` attestation this
  record fills in, and which routed every platform specific here),
  [ADR 0009](0009-runner-trust.md) (secrets by reference, the typed actor, the
  loopback rule this record's observation choice follows) — **PROPOSED in
  [PR #78](https://github.com/FlexOr2/atelier-2/pull/78) and not yet on `main`, so
  this record cannot be accepted before it is**,
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

One constraint is decisive for observation: under ADR 0009 §3 a deployment declares
its exposure rather than having it inferred from a bind address, and this one
declares `this-machine` — nothing outside the machine session may reach the API, so
there is no reachable inbound URL today.

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
| How an Atelier action is recognizable | on the platform, only where the operation has a content slot for a marker (decision 5); everywhere else, only from Atelier's own receipts | by the account that acted, on every operation, plus the same marker |
| Multi-user successor | none: the connection is one human's reach | user-to-server tokens per operator under the same App |
| Revoking one connection | rotate the token: everything else that token drives breaks with it | uninstall or suspend that installation alone; other installations are untouched |
| If the held credential leaks | the granting user's reach, until the operator rotates it | **wider, not narrower**: the private key authenticates the App itself, so it can mint an installation token for *every* installation that trusts that App. Recovery is a key rotation across the App, not a local revocation |

**Atelier's own receipts are where actor truth lives, in both methods.** The
authoritative answer to "did Atelier do this, under which run and node" is
Atelier's receipt ledger, which carries the typed actor of ADR 0009 §9 whatever the
platform recorded. Where an operation has a content slot, Atelier additionally
writes a marker so a human reading the platform sees the same thing (decision 5
enumerates the slots, because not every operation has one). The platform's actor
field is therefore corroboration in the App method and, in the token method, no
evidence about agency at all.

**The honest weakening this record names rather than forbids:** in the token method
the platform records the operator for every operation, so on the platform side an
agent's action and the operator's own hand differ only by a marker that any holder
of the account could also write — honest labeling, not proof — and for contentless
operations not even by that. Three consequences follow, and none blocks the method:

- the account-level separation #8's fourth anti-gaming rule and #79's automation
  filter would like exists only in the App method; in the token method those owners
  read Atelier's receipts for what Atelier did, and the content marker as a *claim*
  with its provenance rather than as platform-proven attribution (decision 6);
- **a label is never a write operation Atelier performs for authorization
  purposes.** Labels are an observed input to #79's filter, never an authorization
  Atelier grants itself; no adapter operation writes a label that any authorization
  or automation filter reads. Without this rule the token method has an obvious
  loop — Atelier labels an item as permitted and the platform records the operator
  as having permitted it — and closing that loop is cheaper than detecting it;
- **merge and close are bounded in the operation registry, never as adapter-wide
  powers.** Which state-changing operations exist, what each may transition and
  what its readback proves are declared per adapter-operation revision under ADR
  0006, so "Atelier can merge" is never a general truth about the connection: it is
  true of exactly the published operation revisions the run binds and its
  `external_effects` attestation covers.

That is the tradeoff the operator is choosing, stated where they can see it.

- **Connecting a project is an explicit operator act**, with a durable record
  binding the project, the chosen method, the reference to its credential, the
  repository scope, the connecting actor, and — in the App method — the App
  identity and the installation identity the credential is used against, so an
  incident starts from named identities rather than from a search. The record holds
  those identities and never the credential, and it is a local subset of what the
  App's key reaches, never the inventory of it (decision 3). It is the same shape as ADR
  0009 §4's runner enrolment, and deliberately not a second ceremony. An unconnected project
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
- **The App method's long-lived secret is the private key, and its blast radius is
  the App, not the installation.** Disconnecting or suspending one installation
  stops that connection; it does nothing about a leaked key, which can still mint
  an installation token wherever that App is installed. Recovery is therefore a key
  rotation at the App. Neither method's credential is stored, logged or projected,
  so this is a recovery contract, not a second secret rule.
- **The affected set is asked of the platform, never inferred from Atelier's own
  records.** Two rules make that answerable. Atelier uses a **dedicated App**, one
  the deployment owns and shares with nothing else, so the key's reach and the
  product's reach are the same set by construction. And at rotation the authoritative
  inventory is **the platform's own list of that App's installations**, not the
  connection records: a connection record is a *local subset* — the installations
  Atelier was told to use — and an incident must assume the key reached every
  installation trusting the App, including ones no connection record names. Anything
  else inventories the attacker's target from the victim's notes.
- A bound credential reference that does not resolve refuses at run start
  (`platform-credential-unresolvable`), with no fallback to another auth mode.
- **Raw platform responses do not land durably.** ADR 0006 already has the rule:
  the operation revision declares its typed readback projection and the core
  carries that projection as an opaque hashed payload. The adapter never writes a
  raw provider frame into a receipt, per #1's frame rule.

### 4. Item observation: polling in V1, a webhook as a hint later

**V1 polls.** The reason is not effort: this deployment declares `this-machine`
exposure under ADR 0009 §3, and under that declaration nothing outside the machine
session may reach the API, so there is no address a delivery could reach.
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
  key of its own, so where an operation creates a content-bearing object the adapter
  **carries the effect's request hash in that object's own content**, and `execute`
  is always readback-then-create. The marker is what lets a re-attempt after a crash
  find the first effect instead of creating its twin, and it is the same marker that
  carries decision 2's attribution.

**Where the marker lives, per operation — enumerated, never assumed.** Attribution
and deduplication are properties of an operation, not of the adapter, so each
published operation revision declares which row it is:

| Operation kind | Marker slot | Deduplication | Attribution on the platform |
| --- | --- | --- | --- |
| Create an issue, a pull request, or a comment | the object's own body, which Atelier authors in full | readback by marker | the marker, plus the account in the App method |
| Push a commit Atelier authors | a commit trailer, plus the commit's author and committer identity — both owned by the operation revision, see below | readback by trailer | the trailer and that identity |
| Edit an object Atelier itself created | the same body slot, rewritten whole | the target's content hash | unchanged from its creation |
| Write into a body a human owns — a requirement issue above all | **none: Atelier does not write it** | not applicable | not applicable |
| A contentless change: close, reopen, merge, apply a label, request a review | **none exists** | the target's own state, read back by id | **only Atelier's receipts.** In the token method the platform attributes it to the operator, and this record says so rather than implying otherwise |

Three rules fall out and are binding. **A human-owned body is never mutated to
carry a marker**, because a requirement issue is #1's editable source of truth and
a marker written into it corrupts exactly the bytes the revision hashes. **A
companion comment is never used to mark a contentless change**, because it is a
second effect with its own failure window and would have to be reconciled against
the first. And **an operation whose declared row is "none exists" is honest about
it**: its receipt carries the attribution, no marker is claimed, and no consumer is
told the platform proved something it did not.

**The marker syntax and the commit identity have an owner here, because nothing
else in this repository owns them.** Repository policy binds no trailer key and no
per-invocation commit identity, so a commit operation would otherwise inherit
whatever ambient git configuration the host happens to carry — which is how an
agent's commit ends up wearing a human's name. Therefore: **one marker contract
inside the adapter owns the marker's exact syntax and its trailer key**, shared by
every operation revision so two operations cannot drift into two dialects, and
**each commit operation revision declares the author and committer identity its
commits carry**, per invocation and never read from ambient git configuration.
Both are proven with the operation revision: a commit produced by that operation
carries exactly the declared identity and trailer, and a run with a differently
configured host produces the same two. Should repository-wide authorship policy
later acquire an owner, this record defers to it rather than keeping a second copy.

- **What counts as an authoritative negative is declared per operation, and for a
  create there is none.** The three outcomes of `contracts.effects` are only as
  honest as the read behind them, so:

| Operation kind | Can readback prove `FOUND`? | Can it prove `AUTHORITATIVE_NOT_FOUND`? |
| --- | --- | --- |
| Create a content-bearing object | yes — the marker identifies it | **no.** The platform assigns the identity, so the request has no address to read; a listing is bounded and a search index is eventually consistent. An unmatched scan is `UNKNOWN` |
| A **reversible** state change on an object addressed by id — close, reopen, apply a label, edit, request a review | yes when the intended state is present: for a state-setting effect the intended state *is* the effect | **no by default.** A mismatch is equally explained by "never performed" and by "performed, then reversed or edited by a human", so it is `UNKNOWN` |
| A **monotonic** transition — merge above all | yes — the merged flag and the merge commit | yes. A merge cannot be undone into an unmerged pull request, so not-merged at read time does exclude a merge by this intent, provided the head the intent named is still the one read |

  A reversible operation may reach an authoritative negative only where it declares
  the evidence that proves non-performance: a precondition the platform enforces at
  write time, or an event identity on the target's own timeline that would exist if
  the effect had run. That declaration is part of the operation revision and is
  proven with it; absent one, the row above stands and the negative is `UNKNOWN`. An
  operation revision offering a negative its read cannot support is refused
  (`platform-absence-unprovable`), and an empty search never becomes an absence.
  This is where the third outcome earns its keep: `UNKNOWN` routes to the operator
  reconciliation command `contracts.effects` already owns.
- **The ambiguous retry needs no new state, because a durable one already precedes
  the send.** `EffectIntentState.PREPARED` is written durably before any request
  leaves the adapter, so a crash between send and receipt always leaves a prepared
  intent with its exact request bytes; readback then resolves it to a receipt, an
  authoritative absence, or `UNKNOWN` — and `UNKNOWN` advances to
  `WAITING_RECONCILIATION` rather than being retried blind. **No operation is ever
  re-sent on an unresolved outcome**, whatever its kind: a create because its
  negative is unprovable, a reversible change because a mismatch may mean a human
  undid it and re-sending would overrule them. That is the case the operator
  resolves, and it is the honest price of a platform with no idempotency key.
- **An update is idempotent by content.** A target that already carries the intended
  content hash, or already stands in the intended state, is `FOUND`, not a second
  write.
- **Scope is bound.** An operation addressing an object outside the connected
  project's repository scope refuses (`platform-object-out-of-scope`), whichever
  method holds the credential; multi-project isolation stays #23's.
- **Requirement-revision publication stops being hand-rolled.** The adapter observes
  the requirement issue and publishes an immutable revision from the exact served
  UTF-8 body bytes and their SHA-256 — the same canonical rule the fleet applies by
  hand, computed once by the adapter instead of pasted into prose, and carrying the
  object identity and the read's change marker as provenance. **The canonical rule
  is exact: the bytes the API serves as the body, hashed as they are, with nothing
  appended** — no trailing newline, no re-encoding, no normalization. The issue stays
  the human's: publication is a read, Atelier never writes that body, and the bytes
  it hashes are the bytes a human last wrote. This record decides what is observed,
  what is hashed and what identity it carries; the durable revision store and the
  trace format stay with `docs/requirements/README.md`.

**One recipe, one owner, and the divergence named.** That precision is not pedantry,
it is the reason to mechanize: two operative recipes stand in the landed tree today,
and a hash a human types is a hash nobody re-derives. Ruled here so that no builder
and no distiller has to choose between them:

- **The owner stays `docs/requirements/README.md`.** It owns the issue-body citation
  convention, and this record does not take that ownership. What this record settles
  is the one rule the adapter will mechanize, which is therefore the rule a human
  should already be writing.
- **The canonical form is the exact served bytes**, reproduced with

  ```console
  $ gh api repos/FlexOr2/atelier-2/issues/82 --template '{{.body}}' | sha256sum
  ```

  A Go template writes the field and appends nothing. The recipe
  `docs/requirements/README.md` currently prescribes, `--jq '.body' | sha256sum`,
  digests the body plus the newline `gh`'s raw-string output adds — an identity over
  bytes the object does not contain, and one no reader can re-derive from the object
  alone. The newline cannot be suppressed inside `--jq`, which takes the filter as
  its only argument and rejects a `jq` flag; `gh api … | jq -j '.body'` is the
  equivalent two-tool form.
- **The divergence, exactly.** All three landed requirement citations were computed
  under the README's recipe and carry the appended newline: `#79 body @ 9d781a3c`,
  `#82 body @ fe6fd31f`, `#9 body @ 36800d6e`. Every decision-authority digest was
  computed under the exact rule: ADR 0008's `#26` (`69a3f021…`), this record's `#24`
  (`0104406e…`), and ADR 0009's `#21` (`5c03ceb1…`). The split runs between the two
  directories, not inside either.
- **Landed citations are provenance and are not rewritten.** Each records what its
  author read under the recipe its document named at the time, and each stays valid
  under that recipe. Restating a provenance record to match a later convention
  destroys the only thing it was for.
- **The correction is owed, and it is not made here.**
  `docs/requirements/README.md` corrects its recipe line to the exact form and notes
  that citations predating the correction carry the appended-newline form. That step
  belongs to the requirements-document owner and is routed to
  [#93](https://github.com/FlexOr2/atelier-2/issues/93); this record must not edit
  another owner's convention from inside a decision.

### 6. Readback semantics: what merged, closed and labeled mean

One rule governs all of them, and it is ADR 0009 §5's rule rather than a second
one: **an observed platform fact is evidence** carrying its provenance — the object
identity, the exact observed fields, the read time and the platform's change
marker. Only the core writes a verdict.

- **`merged` is not `closed`.** Merged is the pull request's merged flag together
  with its merge commit. A closed-unmerged pull request is a distinct terminal fact
  and is never read as success. Reading these states is unconditional; *performing*
  a merge or a close is not, and stays bounded to the published operation revisions
  decision 2 names.
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
  with no named need. Labels are read in one direction only: per decision 2 no
  Atelier operation writes one that an authorization or filter reads, so a label
  the filter trusts was always set by someone other than Atelier — and in the token
  method, where the account cannot show that, Atelier's own receipts are what say
  so.
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
| `platform-marker-slot-unavailable` | an operation revision declares a marker slot the object kind it writes does not have | operation binding |
| `platform-absence-unprovable` | an operation declares an authoritative negative its read cannot support — a create, a reversible change with no declared non-performance evidence, or an absence derived from an eventually consistent search | operation binding, and again at readback |
| `platform-actor-unattributable` | an observed action maps to no known actor and carries no Atelier marker | observation |
| `platform-observation-rate-limited` | the platform's limit stops a poll; visible, cursor preserved | observation |

Durable failure tokens, where any of these must become one, are minted by #16;
this record borrows that owner rather than opening a second vocabulary.

## Consequences

- The operator connects a project in the way they choose, and the low-friction path
  is a first-class one rather than a concession. Neither method is a different
  product: the same operations, receipts, refusals and secret rules hold on both.
- An Atelier action stops being recognizable only by a prose signature. Its typed
  actor is in Atelier's receipts under both methods, a content marker adds a
  platform-visible copy wherever an operation has a slot for one, and in the App
  method the account proves it outright. The record says which of the three a
  consumer is looking at instead of flattening them.
- The disowned-verdict failure is therefore mitigated in both methods and closed
  only in the App one — and for a contentless change in the token method it is
  mitigated only in Atelier's own ledger. Naming that honestly is the point: a
  consumer that needs account-level proof — #8's fourth rule, a future multi-user
  path — knows it must ask for the App method rather than discovering the gap later.
- An effect whose outcome no read can settle waits for the operator instead of being
  retried into a duplicate or over a human's correction. That is a real cost of a
  platform without idempotency keys, paid on the reconciliation path the effect
  contract already has, rather than hidden behind a read that cannot prove what it
  is asked to prove.
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
- A crash before the request leaves the adapter still finds the intent durably
  `PREPARED` with its exact request bytes, and an effect whose readback cannot
  settle reaches `WAITING_RECONCILIATION` without a second send.
- A readback that can only search returns `UNKNOWN` and routes to reconciliation;
  no path returns `AUTHORITATIVE_NOT_FOUND` from a search, and no create operation
  declares an authoritative negative at all.
- A reversible change whose target does not stand in the intended state returns
  `UNKNOWN`, not an absence — proven with the human-reversal case: Atelier closes an
  item, a human reopens it, and the readback does not report that Atelier never
  closed it.
- A merge readback proves both outcomes against the head the intent named, and a
  head that moved since is not answered as if it had not.
- A commit an operation produces carries exactly the identity and trailer that
  operation revision declares, on a host whose ambient git configuration says
  something else.
- An unconnected or revoked project performs no operation and yields no
  observation, and no durable row is written on the refusal path.
- Every operation revision declares its marker slot and its authoritative-negative
  row, and one declaring a slot its object does not have is refused at binding.
- A contentless change writes no companion object, and its attribution is readable
  from Atelier's receipts alone.
- No operation writes a body Atelier does not own; a requirement issue's bytes are
  byte-identical before and after a run that observed it.
- No published operation writes a label that any authorization or automation filter
  reads.
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
- A published requirement revision's digest matches the platform's served bytes
  recomputed independently, with no added newline and no normalization.

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
marker-derived actor presented as platform-proven attribution; a content-bearing
object created without its marker, or a marker slot assumed for an operation that
has none; a marker written into a body a human owns; a companion object created to
mark a contentless change; a label written for an authorization or filter to read;
an authoritative negative claimed for a create or for a reversible change without
its declared non-performance evidence, or any absence derived from search; any
effect re-sent on an unresolved outcome; a commit identity read from ambient git
configuration instead of its operation revision; a shared App, or an incident that
inventories only Atelier's own connection records; a stored token or installation
token; a
secret value in a workflow, prompt, context package, event, receipt, log or API
resource; a create without a prior readback; a GitHub identifier outside the
adapter; an agent shell publishing through `gh` instead of an Action node; a
webhook accepted as truth without a readback; a poll interval hardcoded instead of
configured; or a label name minted here.

## Supersedes

None.
