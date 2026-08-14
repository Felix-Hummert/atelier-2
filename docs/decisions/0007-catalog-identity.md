# ADR 0007: Named lineages own catalog identity above hash-true revisions

- Status: DRAFT — proposed for review, not accepted, not implemented
- Date: 2026-08-14
- Depends on: [ADR 0001](0001-durable-runtime.md),
  [ADR 0002](0002-exact-yaml-graph.md), [ADR 0006](0006-node-vocabulary.md), and
  **ADR 0006 amendment A1** below, which acceptance of this record requires
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1),
  whose portable-declarative-file, live-and-versioned-configuration and
  platform-ownership rules this record expresses and never re-decides
- Answers: [#22](https://github.com/FlexOr2/atelier-2/issues/22)
- Feeds: [#6](https://github.com/FlexOr2/atelier-2/issues/6) (the catalog, its
  publish gate and its precedence), [#8](https://github.com/FlexOr2/atelier-2/issues/8)
  (the scorecard), and ADR 0006's reference binding, whose slice V3-4 builds
  against the port in decision 7
- Names, never decides: [#16](https://github.com/FlexOr2/atelier-2/issues/16) (sole
  schema-version owner), [#24](https://github.com/FlexOr2/atelier-2/issues/24) (platform
  adapter), [#26](https://github.com/FlexOr2/atelier-2/issues/26) (budget)
- Evidence: documentary. Read at `c972f70`: ADRs 0001–0006,
  `src/atelier2/adapters/dbos/schema.py` (`SCHEMA_VERSION = 7`, `workflow_revisions`,
  `auth_profile_revisions`), `src/atelier2/contracts/agents.py`
  (`auth-profile-revision/v1`), `src/atelier2/contracts/hashing.py` (`frame`),
  `src/atelier2/application/publish_workflow_revision.py`, and issues #1, #6, #8, #16,
  #22, #24. No code changed, no gate run; nothing below is implemented.

## Context

`workflow_revisions` is keyed by `revision_hash` and holds document bytes, and that is
the whole of catalog identity today: a revision has no series, no predecessor, no
successor, and no way to be asked for by anything but its hash. #6 wants a catalog of
named, versioned, proven chains that new work is laid back into; #8 wants a balance
aggregated over a revision **lineage** with decay, so re-publishing cannot wash a
record clean. Neither has an object to attach to, and ADR 0006's references are
written `{ref: "<… id>", revision: "<… revision id>"}` while handing "their naming,
lineage and storage" here — so `ref` has no owner and no rule saying the revision
belongs to it.

`auth_profile_revisions` shows both halves. It proves the shape —
`(profile_id, revision_number)` unique over hash-identified immutable revisions — and why
the catalog cannot copy it: `auth-profile-revision/v1` frames `profile_id` into the
revision hash, so that series can never be renamed, while catalog entries must be.
Identity is therefore **layered**, not embedded: a stable derived id below, a mutable
display name above, joined by rows and not by bytes.

## ADR 0006 amendment A1, required for acceptance

ADR 0006's normative reference form already writes an **id** in the `ref` position and
defers naming to #22, so `ref` = lineage id contradicts nothing it decided. Its examples,
however, spell readable tokens (`review_verdict`, `review_panel`) there, which reads as a
display name. One line is added beside its "this record only the reference form" sentence,
and this record is not accepted before it lands:

> `ref` is the stable derived lineage id of ADR 0007. The readable tokens in this
> record's examples are illustrative lineage ids, never display names; a display
> name never appears in document bytes.

## Decision

### 1. A lineage has a stable id, a mutable name, and an immutable alias history

A **lineage** is a first-class object over the immutable hash-identified revisions of ADR
0001/0002: a derived id, a kind, an optional retirement, and an ordered dense membership
with `(lineage, revision_number)` and `(lineage, revision_hash)` both unique,
`revision_number` assigned by the store as `max + 1`. Revisions stay what ADR 0002 made
them — exact bytes, SHA-256 identity, immutable, never re-parsed.

The id is derived, not minted, so two stores converge instead of forking:
`catalog-lineage/v1` over `atelier2.contracts.hashing.frame` with the preimage
`(kind token, founding revision hash)`. The founding revision is what a lineage *is*;
renaming keeps the id. Consequence, accepted: identical founding bytes published twice
yield one lineage, which is the convergence import needs.

**The `kind` token set is closed**, because a token enters that preimage: `workflow`,
`schema`, `deterministic_operation`, `adapter_operation`, `context_source`,
`read_operation`, `profile`, `skill`, `tool`, `policy`, `budget_policy`, `retry_policy`,
`cancellation_policy` — ADR 0006's registry list — plus this record's `scorecard_policy`
and `selection_policy`; `auth_profile` is deliberately absent, and adding a token is an
amendment. This closes the set of **registries**, not the kinds inside one: ADR 0006
leaves context-source, read-operation and schema kinds open and this record keeps them so.

**What enters a hashed preimage is the stable lineage id; the mutable display name
never does.** A persisted reference is `{ref: <lineage id>, revision: <revision hash>}`
— both sides immutable, so a rename changes no document, no bound run, no receipt.
That follows the existing model rather than breaking it: `auth-profile-revision/v1`
already frames a stable id into a hash. The first draft's claim that *no name* enters
a hashed preimage was false for V3 bytes and is withdrawn.

The **display name** is a mutable label owned by authoring and the cockpit. Every name a
lineage held is kept append-only as `(lineage, name, from, to, actor)`, the current name
being the open-ended row, so an old name stays resolvable at authoring time and resolves
*labeled* with the current one: the operator learns the entry was renamed instead of
getting a silent hit or a silent miss. A name is never reassigned to another lineage of
the same kind, retired or not.

Admitting identical bytes into a **second** lineage of one kind is refused, or #8's rule
1 falls to copy-and-rename. A cosmetic edit still founds a new lineage and content
identity cannot see the difference; the defense is visibility, not detection — that
lineage starts at `n = 0`, below #8's minimum-n, and decision 6's gate makes its author
name the nearest existing entry.

### 2. Content publication and catalog admission are two states, two commands

Conflating them contradicted ADR 0006's publishable-before-executable staging.

- **Content publication** writes immutable revision bytes keyed by their hash. It exists
  today (`publish_workflow_revision`), requires no lineage, and per ADR 0006 is permitted
  before the runtime can execute the revision and before the catalog exists at all. It is
  never refused for lack of a catalog.
- **Catalog admission** binds an already-published revision into a lineage at a dense
  position, under decision 6's gate. It is optional until the catalog exists; once it does,
  one published policy per kind says whether admission is required before a reference may
  bind that kind. #6's "never a throwaway object" is that policy, not a hidden rule inside
  publication.

A revision with no lineage is therefore **published, not admitted** — the honest state of
every historical V7 revision. `resolve` returns it forever; only `resolve_reference`
refuses it, because only there does a `ref` claim a membership.

### 3. Measurements are an append-only ledger with cross-store-stable identity

Balance data persists as an append-only ledger, never a mutable aggregate. Each entry
names the lineage and revision the run bound, the measurement kind, its value, and its
source — a closed token set (`run_terminal`, `platform_event`, `operator`), a
`platform_event` entry also carrying #24's stable external reference as evidence. A run's
terminal facts land when it terminates; platform facts (#8's "PR merged", "CI green")
arrive later as their own entries, because a merge happens after a run ends.

**An entry's identity is derived, not store-local**, or export could not converge:
`catalog-measurement/v1` over `frame` with the preimage `(measurement kind token, terminal
run hash)` — ADR 0006's terminal run hash, which covers the ordered event hashes and is
identical in every store holding that run. A store-local run id never enters it, so the
same fact observed twice, or imported from two stores, converges instead of double-counting.

The scorecard is **computed on read** under one published **scorecard policy revision**
naming the decay and the minimum n, and every display names the revision that produced
it, so a changed decay re-reads history instead of rewriting it. Aggregation is over the
lineage, per #8 rule 1. Two exclusions are ledger facts rather than reader filters: an
entry from a run with operator intervention (#9), and a run whose difficulty covariate is
absent, marked `covariate_absent` rather than counted at an invented default.

**Surviving a schema cutover: the declaration is `reset`.** ADR 0001 gives no in-place
migration, so measurements survive only by explicit export and import, and what is not
exported is reset visibly: the scorecard reports `n` from this store and names the
cutover as why the count begins there. No gaming hole, because a cutover resets the whole
catalog at once and cannot be aimed at one entry.

### 4. The store is the catalog, and the only file form is a complete export

**Content truth** — what a revision says — is the exact bytes: republished into any store
they yield the same SHA-256, so a definition travels as a file, as #1 requires.
**Relational truth** — lineage, position, selection, measurement — exists only as store
rows, because a git-file catalog would have to write #8's runtime measurements back on
every run: a second durable writer with none of the store's transactions. **The revision
hash is the join, and neither side may state the other's fact.** At run time nothing is
read from a file; a file enters only through an explicit import command.

**A transport therefore carries exactly one form: the complete catalog of one store
root.** Selective bundles are removed — they were the hole in the anti-gaming claim, since
a bundle chosen to omit one lineage's measurements is exactly the aimed reset decision 3
refuses and no manifest check can tell it from an honest partial store.

An export is one file per revision, holding its exact bytes and named by its hash — never
by a mutable display name — plus one manifest naming, for every lineage in the store: id,
kind, current name, full alias history, retirement, the ordered
`(revision_number, revision_hash)` membership, and every measurement entry. The selection
revision of decision 6 travels as an ordinary revision of kind `selection_policy`, with
the activation history pointing at it.

The manifest is hashed as `catalog-export-manifest/v1` over
`atelier2.contracts.hashing.frame` under ADR 0006's framing rule unchanged — no second
encoding, and each ordered sequence one field carrying its own frame under its own domain
(`catalog-lineage-entry/v1`, `catalog-member-entry/v1`, `catalog-alias-entry/v1`,
`catalog-measurement-entry/v1`).

**Import is explicit, byte-exact, all-or-nothing, and refuses rather than merges.** Every
revision is re-hashed and a mismatch is refused naming the file. For a lineage id the store
already holds, a divergent membership, current name, alias history, retirement or selection
activation is refused naming both sides and the first diverging position — the display name
is the operator's handle, and silently aliasing it is the lie this record exists to prevent.
An import the store already holds, whole or as a prefix, converges without a write.

### 5. Auth profiles are excluded from this generalization

`auth_profile_revisions` is not folded in and keeps its embedded
`(profile_id, revision_number)` shape. Its revision hash is framed over
`auth-profile-revision/v1` with `profile_id` and `revision_number` *in the preimage*, so
retiring those dimensions would change every existing auth-profile revision hash — a
different identity, invalidating every agent-configuration revision referencing one.
`auth_profile` is not a catalog kind, and a profile is not renameable. Any unification needs
its own successor-identity contract — the old-to-new hash mapping, what happens to
referencing agent-configuration and run configuration revisions, and whether history is
reconstructed or reset.

### 6. The publish gate and the full precedence, both versioned

**The gate is #6 Rev. 2 unchanged: every newly authored revision** — not only a founding
one — **names the nearest existing catalog entry and justifies why it does not suffice.**
The justification is recorded with the admission and shown in the existing publish
preview, so the judge of "nothing suitable exists" is never the authoring agent alone.

**The precedence is #6's, all three layers: item binding beats taxonomy beats default.**
It crosses a boundary, so it is split at the boundary:

- **The platform owns the fact** (#24): what type a work item is, and any binding recorded
  on the item itself — the strongest layer, a native platform object per #1, never read by
  the catalog.
- **The catalog owns taxonomy and default, and both are versioned.** They live together
  in one published immutable revision of kind `selection_policy`, mapping work-item types
  to lineage ids and naming at most one default lineage. Changing either publishes a new
  selection revision; the only mutable row is an append-only activation history saying
  which revision is current and who activated it. That is #1's live-and-versioned
  configuration rule applied to a decision which otherwise silently re-ranks the catalog,
  and it retires the first draft's unversioned pointer together with its named debt.
- **The caller resolves the precedence and passes an explicit revision.** Run start never
  guesses and never resolves a name — it binds exactly the revision it was given, as ADR
  0001/0002 already require.

### 7. The resolution port ADR 0006's reference binding builds against

Three operations, and which caller uses which is part of the contract.

- **`resolve(kind, revision_hash)`** — lineage-free lookup, for callers where no `ref`
  claims a membership: run start rebinding a revision its run configuration already pins by
  hash, receipt and history reads, and import verification. It needs no lineage row.
- **`resolve_reference(kind, lineage_id, revision_hash)`** — **the operation reference
  binding calls**, and the membership proof the port previously could not give. It returns
  the revision only when it is an admitted member of that lineage, otherwise refusing
  naming both. Without it `ref` is decoration and two references could disagree about what
  an id means.
- **`resolve_name(kind, display_name | lineage_id, position) -> (lineage_id,
  revision_hash)`** — authoring only: the conductor, the publish preview, the cockpit.
  `position` is closed to `head` (highest `revision_number`) or an exact `revision_number`;
  no floating tag, no range, no "latest stable", since anything that can move under a run
  makes it non-reproducible. It accepts a retired name, returns the current name beside the
  result, and refuses a retired lineage.

**A display name resolves to a lineage id exactly once, at authoring time, before the run
configuration revision is published; after that only ids and hashes exist** — which is
how #1's and ADR 0006's no-silent-rebinding invariant survives a moving head.

### Refusals

- **Admission:** a revision not published; a name outside 1–128 characters of
  `[a-z][a-z0-9._-]*`; a name currently or previously held by another lineage of the same
  kind, naming both; bytes owned by another lineage of the same kind, naming it; a missing
  #6 Rev. 2 justification; a retired lineage.
- **Binding:** a reference whose revision is not an admitted member of the lineage its
  `ref` names, naming both.
- **Selection:** a selection revision naming a retired lineage, a lineage of a kind other
  than `workflow`, two entries for one work-item type, or two defaults.
- **Import:** a revision whose bytes do not re-hash to their name; a lineage whose
  membership, name, alias history, retirement or selection activation diverges, naming both
  sides and the first diverging position; a measurement entry duplicating an existing
  derived id with a different value. A refused import writes nothing.

## Store dimensions and their migration cost

Named honestly, per #16. Five durable shapes, none mutable in place: `catalog_lineages`
(id, kind, founding revision hash, retirement), `catalog_lineage_names` (the alias
history), `catalog_lineage_members` (`(lineage, revision_number, revision_hash)`, both
constraints), `catalog_measurements` (the ledger), and `catalog_selection_activations`.
Nothing retires `auth_profile_revisions` (decision 5).

**The cost is one cutover, and it is a cutover ADR 0006 already requires.** None of these
may enter #16's preserving V7→V8 or V8→V9 phases: a preserving migration cannot invent a
lineage for revisions that never had one, and inventing one per existing revision would
fabricate exactly the founding facts #8 aggregates over. They land only in the
non-preserving store replacement ADR 0006 names for its V3 records; which version that is
remains #16's. The catalog then starts empty, and existing V7 revisions stay
published-not-admitted until admitted — which costs nothing, since identical bytes yield
the identical hash.

## Consequences

Prices, stated once. Renaming costs an alias history and a name never reusable across
lineages of one kind; a re-readable ranking costs computing the scorecard on read; a
*catalog* travels only as a complete store root; ADR 0006 pays amendment A1 and one added
port operation; and a cosmetic fork can still start a fresh balance, made visible here
rather than prevented.

## Required proofs before acceptance

- Literal vectors over `atelier2.contracts.hashing.frame` pin the lineage id, the
  measurement id and the export manifest hash; the same founding revision in two
  independently built stores yields the identical lineage id, the same bytes under a
  different kind a different one, and each closed `kind` token has its own vector.
- Admission into a new lineage records position 1, the next distinct document position 2,
  and re-admitting identical bytes is idempotent without a second row; concurrent
  admissions yield distinct dense positions and lose no revision.
- Renaming changes no revision hash, no lineage id and no bound run; the previous name
  still resolves, returns the current name beside it, and cannot be claimed by another
  lineage of that kind. Advancing a head leaves every published run configuration, run
  snapshot, receipt and composed preview hash unchanged.
- Publication succeeds with no catalog present and reads back as published-not-admitted;
  `resolve` returns such a revision, `resolve_reference` refuses it naming the lineage the
  `ref` claimed, binds a real member, and refuses a non-member naming both; `resolve_name`
  returns head and an exact position and refuses a retired lineage; and no published run
  configuration or receipt contains a display name.
- A newly authored revision without a nearest-entry justification is refused whether or
  not it founds a lineage, and the justification reaches the publish preview.
- Item binding beats the selection revision's taxonomy, which beats its default;
  activating a new selection revision changes later resolutions and no earlier bound run,
  and the activation history names who activated each.
- Export then import into an empty store reproduces every revision byte-identically and the
  identical ids, names, alias histories, positions, activations and measurements; a second
  import writes nothing; a divergence is refused naming both sides with nothing written;
  no export narrower than a store root can be produced.
- A scorecard is identical after a restart, changes when the policy revision changes and
  not otherwise, names it, and excludes intervention and `covariate_absent` entries; a
  platform fact arriving after the run terminated folds in; the same fact observed twice,
  or imported from a second store, does not double-count; a store with no measurements
  reports `n = 0` and names the cutover.
- Every refusal above is proven by its own behavioral case, parametrized over the refusal
  list rather than copied per case.

## Out of scope

The document surface, its bindings and its refusals beyond amendment A1 (ADR 0006); the
scheduler and executor; how a difficulty covariate is derived and which measurements #8
finally names; any successor identity for auth profiles (decision 5); platform addressing,
authorization, event observation and the item-level binding's storage (#24); budget units
(#26); which schema version carries the cutover (#16); project isolation (#23); and any
conversational authoring surface above the catalog (#7).

## Supersedes

None. This record extends [ADR 0002](0002-exact-yaml-graph.md), still the owner of
revision identity, and [ADR 0006](0006-node-vocabulary.md), still the owner of the
document surface and its reference form, which amendment A1 clarifies rather than changes.
