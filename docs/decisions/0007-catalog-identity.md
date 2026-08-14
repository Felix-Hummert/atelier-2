# ADR 0007: Named lineages own catalog identity above hash-true revisions

- Status: DRAFT — proposed for review, not accepted, not implemented
- Date: 2026-08-14
- Depends on: [ADR 0001](0001-durable-runtime.md),
  [ADR 0002](0002-exact-yaml-graph.md), [ADR 0006](0006-node-vocabulary.md)
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1),
  whose "Workflows sind portable deklarative Dateien" and platform-ownership
  rules this record expresses and never re-decides
- Answers: [#22](https://github.com/FlexOr2/atelier-2/issues/22)
- Feeds: [#6](https://github.com/FlexOr2/atelier-2/issues/6) (the catalog and its
  publish gate), [#8](https://github.com/FlexOr2/atelier-2/issues/8) (the
  scorecard, whose balance data this record gives a home), and ADR 0006's
  reference binding, whose slice V3-4 builds against the port in decision 6
- Names, never decides:
  [#16](https://github.com/FlexOr2/atelier-2/issues/16) (sole schema-version
  owner), [#24](https://github.com/FlexOr2/atelier-2/issues/24) (platform
  adapter), [#26](https://github.com/FlexOr2/atelier-2/issues/26) (budget)
- Evidence: documentary. Read at `f2d84f0`: `docs/decisions/0001`–`0006`,
  `src/atelier2/adapters/dbos/schema.py` (`SCHEMA_VERSION = 7`,
  `workflow_revisions`, `auth_profile_revisions`),
  `src/atelier2/application/publish_workflow_revision.py`,
  `src/atelier2/contracts/hashing.py`, and issues #1, #6, #8, #16, #22, #24. No
  code changed and no repository gate was run; nothing below is implemented.

## Context

`workflow_revisions` is keyed by `revision_hash` and holds document bytes. That
is the whole of catalog identity today: a revision has no name, no predecessor,
no successor, and no way to be asked for by anything but its hash. #6 wants a
catalog of named, versioned, proven chains that new work is laid back into; #8
wants a balance aggregated over a revision **lineage** with decay, so that
re-publishing cannot wash a record clean. Neither has an object to attach to,
and ADR 0006's references are already written as `{ref: <name>, revision: <id>}`
— so `ref` is a name with no owner and no rule saying the revision belongs to
it.

One shape already exists and is the right one: `auth_profile_revisions` carries
`(profile_id, revision_number)` unique over a hash-identified immutable
revision. This record generalizes that shape instead of inventing a second one.

## Decision

### 1. A lineage is a named ordered set of revisions, and no revision learns its name

A **lineage** is a first-class object over the immutable hash-identified
revisions of ADR 0001/0002. It carries an id, a mutable human name, a kind, an
optional retirement, and an ordered dense membership: `(lineage,
revision_number)` unique and `(lineage, revision_hash)` unique,
`revision_number` assigned by the store as `max + 1` at publish. Revisions stay
exactly what ADR 0002 made them — exact bytes, SHA-256 identity, immutable,
never re-parsed.

**The catalog name never enters the hashed document.** If it did, renaming would
fork the graph identity and the same authored graph published under two names
would be two different revisions. So identity is layered: bytes below, name
above, joined by the hash. This is also why ADR 0006 needs no amendment — no
field is added to format V3, and its closed refusal list is untouched.

A lineage id is derived, not minted, so two stores converge instead of forking:
`catalog-lineage/v1` over `atelier2.contracts.hashing.frame` with the preimage
`(kind token, founding revision hash)`. Renaming keeps the id; the founding
revision is what a lineage *is*. Consequence, accepted: publishing identical
founding bytes twice yields one lineage, which is the convergence import needs.

Every published revision joins exactly one lineage of its kind, and a publish
that names none is refused: #6's "never a throwaway object" is a store rule or
it is a wish. One kind, one owner — the same model covers workflows and every
registry ADR 0006 references (schema, deterministic and adapter operation,
context source, read operation, profile, skill, tool, policy, budget, retry,
cancellation), so `ref` means the same thing everywhere.

Publishing identical bytes into a **second** lineage of the same kind is
refused, naming the lineage that already owns them — otherwise #8's rule 1 is
defeated by copy-and-rename. Honestly: a cosmetic edit still founds a new
lineage, and content identity cannot see the difference. The defense is
visibility, not detection — the new lineage's scorecard starts at `n = 0`, #8's
minimum-n withholds automatic preference there, and #6 Rev. 2's publish gate
makes the author name the nearest existing entry. Which is why that gate
attaches **here**, to lineage creation: a new revision in an existing lineage is
the catalog working as intended, and only a new lineage grows it.

### 2. The store is the catalog; the file is the export

Split, and the split is not a compromise: the two sides own disjoint facts.

- **Content truth** — what a revision says — is the exact document bytes. They
  are portable in the sense #1 means: republished into any store they yield the
  same SHA-256, so a definition genuinely travels as a file and needs no lineage
  to be understood.
- **Relational truth** — which lineage, which position, which selection, which
  measurement — exists only as store rows. #8's measurements are runtime facts
  produced by runs and platform events; a git-file catalog would have to write
  them back into files on every run, which is a second durable writer next to
  the canonical store with none of its transactions.

**The one-truth rule: the revision hash is the join, and neither side may state
the other's fact.** A file never carries a binding position; an export manifest
(decision 4) is a transport snapshot that becomes true only by being imported.
At run time nothing is read from a file: run start, reference binding and the
scorecard read store rows only. A file enters exclusively through an explicit
import command, which writes rows and refuses conflicts instead of merging them.

This also keeps ADR 0001's rule intact: no runtime migration is invented,
because the catalog holds no second store to migrate.

### 3. Measurements are an append-only ledger; the scorecard is a projection

Balance data persists as an **append-only measurement ledger**, never a mutable
aggregate. Each entry names the run, the lineage and revision that run bound,
the measurement kind, its value, and its source; entries are unique per `(run,
measurement kind)`, so a re-observed platform fact converges rather than
double-counting. A run's own terminal facts are written when the run terminates;
platform-sourced facts (#8's "PR merged", "CI green") arrive later as their own
entries through #24's adapter, because a merge happens after a run ends.

The scorecard is **computed on read** from that ledger, under one published
**scorecard policy revision** naming the decay and the minimum n, and every
displayed scorecard names the policy revision that produced it. A changed decay
therefore re-reads history instead of rewriting it, and it can never silently
re-rank the catalog. Aggregation is over the lineage, per #8 rule 1.

Two exclusions are ledger facts, not filters applied by a reader: an entry from
a run with operator intervention (#9) is marked and excluded, and a run whose
difficulty covariate is absent is marked `covariate_absent` and excluded rather
than counted at an invented default. How the covariate is derived is #8's own
work; this record only refuses to fabricate one.

**Surviving a schema cutover: declared, and the declaration is `reset`.** ADR
0001 gives no in-place migration — a new exact schema version is created in an
empty store and older stores are rejected unmutated. So measurements survive a
cutover only by an explicit operator export and import (decision 4). What is not
exported is **reset, visibly**: the scorecard reports `n` from this store and
names the cutover as the reason the count begins there. It never presents a
continuous history it does not hold, and it never synthesizes a pre-cutover
aggregate. That reset is not a gaming hole, because a cutover resets the whole
catalog at once and cannot be aimed at one entry.

### 4. Export and import carry lineage identity

An **export bundle** is one file per revision holding its exact bytes and named
by its hash — never by its catalog name, which would collide and would put a
mutable label into a filename — plus one manifest, itself hashed, naming for
each lineage: id, name, kind, retirement, the ordered `(revision_number,
revision_hash)` membership, the selection state of decision 5, and the
measurement ledger entries.

**Import is explicit, byte-exact, and refuses rather than merges.** Each
revision is re-hashed on import and a mismatch is refused naming the file. A
lineage id already present whose ordered membership diverges is refused naming
the first diverging position; a name already held by a different lineage id is
refused naming both, because the name is the operator's handle and silently
aliasing it is the lie this record exists to prevent. An import that is a prefix
of what the store holds, or that the store already holds entirely, converges
without a write. A bundle carrying revisions without their measurements imports,
and the scorecard says `n` starts here.

Portability is therefore lossless across stores and honest across cutovers, and
the identity that makes it work is the derived lineage id: the same founding
revision names the same lineage in every store that ever sees it.

### 5. Item-type binding: catalog offers, platform tells, caller decides

The precedence #6 decided — item binding beats taxonomy beats default — crosses
a boundary, so it is split at that boundary rather than owned by one side.

- **The catalog owns the offer.** A lineage declares the work-item types it
  serves, and at most one lineage per type is marked its default. Both are
  lineage-level store rows, not revision content: selection is a mutable pointer
  with one holder, and putting it in a hashed document would make "the default
  for a story" a moving hash that every publish silently re-decides.
- **The platform owns the fact** (#24): what type a work item is, and any
  binding recorded on the item itself. Those are properties of a native platform
  object, and per #1 external platforms own their native objects. The catalog
  never reads a platform object, and the platform never stores what a workflow
  choice means.
- **The caller resolves the precedence and passes an explicit revision.** Run
  start never guesses and never resolves a name — it binds exactly the revision
  it was given, as ADR 0001/0002 already require.

Named debt: the default pointer keeps no history in V1, so who changed a default
and when is not reconstructible. It is an explicit attributed command, and
adding a history is a later decision, not a silent one.

### 6. The resolution port ADR 0006's reference binding builds against

Two lookups, deliberately separated, so that the registry story can be built
without absorbing any decision above.

- **`resolve(kind, revision_hash)`** — the only form reference binding and run
  start ever call. It returns the revision record, or refuses naming the
  reference. It needs no lineage row, no name, no selection and no measurement,
  so decisions 1–5 cannot prejudge it.
- **`resolve_name(kind, name | lineage_id, position) -> revision_hash`** — an
  authoring-time lookup used by the conductor, the publish preview and the
  cockpit. `position` is a closed set: `head` (highest `revision_number`) or an
  exact `revision_number`. No floating tag, no range, no "latest stable" —
  anything that can move under a run would make it non-reproducible.

**A name resolves to a hash exactly once, before the run configuration revision
is published; after that only hashes exist.** No name is ever in a hashed
preimage, so a lineage advancing its head changes no published revision, no
bound run, and no receipt — the same no-silent-rebinding invariant #1 and ADR
0006 already hold.

One contract that binding gains from this record: a reference `{ref: N,
revision: R}` is refused at binding when `R` is not a member of the lineage
named `N`, naming both. Without it `ref` is decoration, and two references could
disagree about what a name means. Reference binding otherwise stays hash-keyed
exactly as ADR 0006 wrote it. `resolve_name` refuses a retired lineage;
`resolve` never does, so every historical run stays readable forever.

### Refusals

Refused at publish: a revision naming no lineage; a name outside 1–128
characters of `[a-z][a-z0-9._-]*`; a name already held by another lineage of the
same kind; bytes already owned by another lineage of the same kind, naming that
lineage; a new lineage without the #6 Rev. 2 justification naming the nearest
existing entry.

Refused at binding: a reference whose revision is not a member of the lineage
its `ref` names, naming both.

Refused at selection: a retired lineage; a lineage as default for an item type
it does not declare; a second default for one item type.

Refused at import: a revision whose bytes do not re-hash to their name; a
lineage whose membership diverges from the stored one, naming the first
diverging position; a name held by a different lineage id, naming both; a
measurement entry duplicating an existing `(run, measurement kind)` with a
different value.

## Store dimensions and their migration cost

Named honestly, per #16. This record demands four durable shapes:

1. `catalog_lineages` — id, kind, name, founding revision hash, retirement.
2. `catalog_lineage_members` — `(lineage, revision_number, revision_hash)` with
   both uniqueness constraints. It generalizes `auth_profile_revisions`'
   embedded `(profile_id, revision_number)`, which retires into it; one lineage
   model, not one per registry.
3. `catalog_measurements` — the append-only ledger of decision 3.
4. `catalog_selections` — item type → lineage, small and the only mutable one.

**The cost is one cutover, and it is a cutover ADR 0006 already requires.** None
of these may enter #16's preserving V7→V8 or V8→V9 phases: a preserving
migration cannot invent a lineage for revisions that never had one, and
inventing one per existing revision would fabricate exactly the founding facts
#8 aggregates over. They land only in the non-preserving store replacement ADR
0006 names for its V3 records — under ADR 0001's rule, a new exact version in an
empty store with older stores rejected unmutated. Which schema version that is
remains #16's to say; this record adds tables to that cutover and does not open
a second one.

Consequence, stated rather than hidden: at the cutover **the catalog starts
empty**, and existing V7 revisions carry no lineage. They enter it by being
published under a name, which costs nothing, because identical bytes yield the
identical hash.

## Consequences

- The catalog becomes askable by name, and #8 gets a subject: a balance
  aggregates over a lineage that a re-publish cannot reset, because the lineage
  is derived from its founding revision rather than declared by its author.
- Portability is real and bounded: a definition travels as bytes anywhere, but a
  *catalog* travels only through an explicit export and import, and a store
  cutover without one is a visible reset rather than a silent gap.
- Ranking history is re-readable rather than rewritable, at the price of
  computing the scorecard on read and carrying the policy revision beside every
  display.
- ADR 0006 is untouched: no format field, no new refusal in the document parser,
  and its reference binding gains exactly one membership check.
- Four durable shapes and one retired embedded shape ride one already-required
  cutover. Nothing here justifies a cutover of its own.
- A cosmetic fork can still start a fresh balance. This record makes that
  visible and cheap to catch; it does not claim to prevent it.

## Required proofs before acceptance

This record is a draft; nothing below exists yet.

- A publish into a new lineage records position 1; the next distinct document
  records position 2; re-publishing identical bytes into the same lineage is
  idempotent and returns the existing position without a second row. Concurrent
  publishes into one lineage yield distinct dense positions and lose no
  revision.
- The derived lineage id is pinned by a literal vector computed over
  `atelier2.contracts.hashing.frame`, and the same founding revision in two
  independently built stores yields the identical id, while the same bytes under
  a different kind yield a different one.
- Renaming a lineage changes no revision hash, no lineage id, and no bound run.
- Every refusal above is proven by its own behavioral case, parametrized over
  the refusal list rather than copied per case.
- Export then import into an empty store reproduces every revision
  byte-identically and the identical lineage ids, names, positions, selections
  and measurements; a second import of the same bundle writes nothing; a
  diverged bundle is refused naming the first diverging position with nothing
  written.
- A scorecard computed from a ledger is identical after a process restart,
  changes when the policy revision changes and not otherwise, names the policy
  revision that produced it, and excludes intervention and `covariate_absent`
  entries; a platform-sourced entry arriving after the run terminated folds into
  it, and the same fact observed twice does not double-count.
- A store carrying only imported revisions and no measurements reports `n = 0`
  and names the cutover, and no automatic preference is applied below the
  minimum n.
- `resolve` binds by hash with no lineage row present at all; `resolve_name`
  returns the head hash, returns an exact position, and refuses a retired
  lineage while `resolve` still returns its revisions.
- A reference whose revision is not a member of the lineage its `ref` names is
  refused at binding naming both, while a correct reference binds unchanged, and
  no published run configuration or receipt contains a name.
- Advancing a lineage's head leaves every published run configuration, run
  snapshot, receipt and composed preview hash unchanged.

## Out of scope

The document surface, its bindings and its refusals (ADR 0006); the scheduler
and executor; how a difficulty covariate is derived and which measurements #8
finally names; platform addressing, authorization, event observation and the
item-level binding's storage (#24); budget units (#26); which schema version
carries the cutover (#16); project isolation (#23, deferred with it); and any
conversational authoring surface above the catalog (#7).

## Supersedes

None. This record extends [ADR 0002](0002-exact-yaml-graph.md), which remains
the owner of revision identity, and [ADR 0006](0006-node-vocabulary.md), which
remains the owner of the document surface and its reference form.
