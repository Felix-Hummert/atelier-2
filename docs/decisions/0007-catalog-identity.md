# ADR 0007: Named lineages own catalog identity above hash-true revisions

- Status: PROPOSED — review closed, submitted for acceptance, not accepted, not
  implemented
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

### 1. A lineage has a stable id, a projected name, and append-only event histories

A **lineage** is a first-class object over the immutable hash-identified revisions of ADR
0001/0002: a derived id, a kind, an append-only alias history, an append-only retirement
history, and an ordered dense membership with `(lineage, revision_number)` and
`(lineage, revision_hash)` both unique, `revision_number` assigned by the store as
`max + 1`. **Nothing about a lineage is ever updated in place**: every mutable fact below
is the latest entry of an attributed, append-only event history, and reading it is a
projection. Revisions stay what ADR 0002 made them — exact bytes, SHA-256 identity,
immutable, never re-parsed.

The id is derived, not minted, so two stores converge instead of forking:
`catalog-lineage/v1` over `atelier2.contracts.hashing.frame` with the preimage
`(kind token, founding revision hash)`. The founding revision is what a lineage *is*;
renaming keeps the id. Consequence, accepted: identical founding bytes published twice
yield one lineage, which is the convergence import needs.

**The `kind` token set is closed**, because a token enters that preimage: `workflow`,
`schema`, `deterministic_operation`, `adapter_operation`, `context_source`,
`read_operation`, `profile`, `skill`, `tool`, `policy`, `budget_policy`, `retry_policy`,
`cancellation_policy` — ADR 0006's registry list — plus this record's `scorecard_policy`,
`selection_policy` and `admission_policy`; `auth_profile` is deliberately absent, and
adding a token is an amendment. This closes the set of **registries**, not the kinds inside
one: ADR 0006 leaves context-source, read-operation and schema kinds open and this record
keeps them so.

**What enters a hashed preimage is the stable lineage id; the mutable display name
never does.** A persisted reference is `{ref: <lineage id>, revision: <revision hash>}`
— both sides immutable, so a rename changes no document, no bound run, no receipt.
That follows the existing model rather than breaking it: `auth-profile-revision/v1`
already frames a stable id into a hash. The first draft's claim that *no name* enters
a hashed preimage was false for V3 bytes and is withdrawn.

The **display name** is a mutable label owned by authoring and the cockpit, and it moves
by appending, never by editing: each rename writes one attributed **alias-activation
event** `(lineage, name, actor, activated_at)` and touches no existing row. The current
name is the latest event, every earlier one stays readable, so an old name stays
resolvable at authoring time and resolves *labeled* with the current one: the operator
learns the entry was renamed instead of getting a silent hit or a silent miss. There is
no `to` column to close, which is what makes the history append-only in fact rather than
only in wording.

**Retirement is the same shape**: one **retirement event** `(lineage, state token, actor,
activated_at)` per change, the current state being the latest event's token. `retired` is
today's only token, so a lineage is retired exactly when an event exists; a reinstatement
would be a second token and a further event, never a deleted row.

A name is never reassigned to another lineage of the same kind, retired or not, **and a
name of exactly 64 lowercase hexadecimal characters is refused outright**. That one
syntactic rule is what disambiguates `resolve_name(kind, display_name | lineage_id,
position)`: a 64-hex input is a lineage id and can be nothing else, every other input is a
display name. A discriminated parameter was the alternative and is rejected — it lets a
caller mislabel an input and receive a wrong hit where the syntactic rule can only refuse.

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
  one published immutable revision of kind `admission_policy` names, per kind, whether an
  authored reference of that kind must bind an admitted member. It is made current exactly
  as decision 6's selection revision is — one entry in the append-only, attributed
  `catalog_policy_activations` — so the policy is live and versioned rather than a constant,
  and #6's "never a throwaway object" is that policy, not a hidden rule inside publication.

**The policy chooses the caller's port; it never bends the port's rule.**
`resolve_reference` requires admitted membership *always* — every kind, every deployment,
no exception a policy can grant. A kind the policy leaves permissive does not get a laxer
`resolve_reference`; its references carry no `ref` at all and bind through the lineage-free
`resolve`. So `ref` means one thing everywhere and no caller invents the rule, and a
revision with no lineage is honestly **published, not admitted** — the state of every
historical V7 revision, which `resolve` returns forever and only `resolve_reference`
refuses, because only there does a `ref` claim a membership.

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

That preimage deliberately carries no source and no evidence, so **convergence is proven by
the whole record and never by the id alone**: an entry whose derived id the store already
holds is accepted only when *every* field is equal, and otherwise refused naming the first
that differs. A same-id entry disagreeing about its source or its #24 evidence is a real
disagreement between two stores, and it is surfaced instead of absorbed.

The scorecard is **computed on read** under one published **scorecard policy revision** —
made current through the same `catalog_policy_activations` history as every other policy —
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
by a mutable display name — plus one manifest whose fields are fixed below. Every policy
revision — selection, scorecard, admission — travels as an ordinary revision of its own
kind, the activation entries pointing at it; no current name, retirement or active policy
is exported beside its history, because a current value is a projection and only the
history is a fact.

**The export is one read transaction** over the store root. A run terminating mid-export
can therefore not be in the manifest for one lineage and missing where the same run is
named again: the manifest is the store as of a single point, or the export fails.

The manifest is hashed as `catalog-export-manifest/v1` over
`atelier2.contracts.hashing.frame` under ADR 0006's framing rule ("What binds a node call")
unchanged — no second encoding, each ordered sequence one field carrying its own frame
under its own domain, and an absent optional the zero-length field in its declared
position, so absence never shifts the frame. Every scalar is its UTF-8 text and nothing
else: an id or hash as its 64 lowercase hexadecimal characters, a kind or state as its
literal token, a `revision_number` as its shortest unsigned decimal, a timestamp as RFC
3339 in UTC at second precision. The field order is complete and normative:

- `catalog-export-manifest/v1`: the lineage sequence, then the policy-activation sequence
  — lineages ordered by ascending lineage id, activations by ascending
  `(policy kind, activated_at, revision hash)`.
- `catalog-lineage-entry/v1`: lineage id, kind, founding revision hash, then four sequences
  in this order — aliases and retirements in activation order, members by ascending
  `revision_number`, measurements by ascending measurement id.
- `catalog-alias-entry/v1`: name, actor, activated_at. `catalog-retirement-entry/v1`: state
  token, actor, activated_at. `catalog-member-entry/v1`: revision_number, revision hash.
  `catalog-policy-activation-entry/v1`: policy kind, revision hash, actor, activated_at.
- `catalog-measurement-entry/v1`: measurement id, revision hash, measurement kind, value,
  source token, evidence reference — zero-length exactly when the source carries none.

**Import is explicit, byte-exact, all-or-nothing, refuses rather than merges, and validates
entirely before it writes.** The sequence is fixed:

1. every revision's bytes are re-hashed against the name they arrived under, a mismatch
   refused naming the file;
2. every lineage id is **rederived** from its `(kind, founding revision hash)` and refused
   where it disagrees with the manifest, so an id is recomputed and never asserted;
3. alias names and member revision hashes are checked for uniqueness **across every lineage
   in the manifest and against the store**, so no name and no revision is claimed by two
   lineages of one kind — the cross-lineage half a per-lineage check cannot see;
4. for a lineage id the store already holds, a divergent membership, alias history,
   retirement history, policy activation or measurement is refused naming both sides and
   the first diverging position — the display name is the operator's handle, and silently
   aliasing it is the lie this record exists to prevent;
5. **only then does the first write happen, in one write transaction.**

A refusal at any step leaves the store byte-identical to its pre-import state. An import
the store already holds, whole or as a prefix, converges without a write.

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
  the catalog. **That binding carries an exact revision hash**, never a lineage id and never
  a name: a platform fact pins exactly, as #1 requires of every run configuration, and the
  catalog is not there to finish it. An item binding carrying anything else is not a binding,
  and #24's adapter refuses it rather than resolving it.
- **The catalog owns taxonomy and default, and both are versioned.** They live together
  in one published immutable revision of kind `selection_policy`, mapping work-item types
  to lineage ids and naming at most one default lineage. Changing either publishes a new
  selection revision; the only mutable fact is a projection of `catalog_policy_activations`,
  the append-only attributed history whose latest entry per policy kind makes the selection,
  scorecard and admission revisions current. That is #1's live-and-versioned
  configuration rule applied to a decision which otherwise silently re-ranks the catalog,
  and it retires the first draft's unversioned pointer together with its named debt.
- **The caller resolves the precedence and passes an explicit revision.** Run start never
  guesses and never resolves a name — it binds exactly the revision it was given, as ADR
  0001/0002 already require.

**One deterministic result, whichever layer wins.** Precedence resolution returns exactly
`(revision hash, provenance tier)` — the tier a closed token set `item`, `taxonomy`,
`default`, so a reader always knows which layer answered. The item layer already holds that
hash and it is taken as recorded; the taxonomy and default layers hold a lineage id and
resolve it once through `resolve_name(kind, lineage_id, head)` at that moment, so the head a
run binds is fixed before its run configuration revision is published and can never move
under it. There is no second result shape and no layer that hands the caller a lineage to
finish.

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
  `[a-z][a-z0-9._-]*`; a name of exactly 64 lowercase hexadecimal characters, which would
  be indistinguishable from a lineage id; a name currently or previously held by another
  lineage of the same kind, naming both; bytes owned by another lineage of the same kind,
  naming it; a missing #6 Rev. 2 justification; a retired lineage.
- **Binding:** a reference whose revision is not an admitted member of the lineage its
  `ref` names, naming both — under every admission policy, permissive or not.
- **Selection and policy activation:** a selection revision naming a retired lineage, a
  lineage of a kind other than `workflow`, two entries for one work-item type, or two
  defaults; an activation whose revision is not of the kind it activates.
- **Import:** a revision whose bytes do not re-hash to their name; a lineage id that does
  not rederive from its kind and founding revision hash, naming both; a name or a revision
  hash claimed by two lineages of one kind within the manifest, or against the store; a
  lineage whose membership, alias history, retirement history, policy activation or
  measurement diverges, naming both sides and the first diverging position; a measurement
  entry duplicating an existing derived id in **any** field, naming the first that differs.
  A refused import writes nothing.

## Store dimensions and their migration cost

Named honestly, per #16. **Six durable shapes, every one append-only, not one updated in
place:** `catalog_lineages` (id, kind, founding revision hash — no mutable column at all),
`catalog_lineage_aliases` and `catalog_lineage_retirements` (decision 1's two attributed
event histories), `catalog_lineage_members` (`(lineage, revision_number, revision_hash)`,
both constraints), `catalog_measurements` (the ledger), and `catalog_policy_activations`,
the single owner making the selection, scorecard and admission revisions current. The sixth
shape is the price of removing the last in-place update, and it pays for itself by giving
the scorecard policy the activation owner it otherwise lacked. Nothing retires
`auth_profile_revisions` (decision 5).

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
*catalog* travels only as a complete store root; every mutable fact costs a projection over
an event history rather than a column read; ADR 0006 pays amendment A1 and one added port
operation; and a cosmetic fork can still start a fresh balance, made visible here rather
than prevented.

## Required proofs before acceptance

- Literal vectors over `atelier2.contracts.hashing.frame` pin the lineage id, the
  measurement id and the export manifest hash; the same founding revision in two
  independently built stores yields the identical lineage id, the same bytes under a
  different kind a different one, and each closed `kind` token has its own vector. The
  manifest vector covers a lineage carrying a retirement event and a measurement carrying no
  evidence, so both the normative field order and the zero-length optional in its declared
  position are pinned by bytes rather than by prose.
- Admission into a new lineage records position 1, the next distinct document position 2,
  and re-admitting identical bytes is idempotent without a second row; concurrent
  admissions yield distinct dense positions and lose no revision.
- Renaming **appends one alias event and updates no row**: the previous name still resolves,
  returns the current name beside it, cannot be claimed by another lineage of that kind, and
  every earlier event survives the rename; retiring appends one retirement event and the
  projected state is the latest entry. Renaming changes no revision hash, no lineage id and
  no bound run; advancing a head leaves every published run configuration, run snapshot,
  receipt and composed preview hash unchanged.
- **Ambiguity:** a display name of exactly 64 lowercase hexadecimal characters is refused at
  admission and at rename, and `resolve_name` therefore reads every 64-hex input as a lineage
  id and every other input as a display name — proven with a name and a lineage id that would
  otherwise collide, and with no discriminating parameter available to the caller.
- Publication succeeds with no catalog present and reads back as published-not-admitted;
  `resolve` returns such a revision, `resolve_reference` refuses it naming the lineage the
  `ref` claimed, binds a real member, and refuses a non-member naming both; `resolve_name`
  returns head and an exact position and refuses a retired lineage; and no published run
  configuration or receipt contains a display name.
- `resolve_reference` refuses an unadmitted revision **under every admission policy,
  permissive or not**; a kind the policy leaves permissive binds that same revision through
  `resolve` and its references carry no `ref`; activating a new admission policy revision
  changes which authored references are accepted and no already-bound run.
- A newly authored revision without a nearest-entry justification is refused whether or
  not it founds a lineage, and the justification reaches the publish preview.
- Item binding beats the selection revision's taxonomy, which beats its default, and each
  layer returns **exactly one exact revision hash beside its provenance tier**; the item
  layer's hash is taken as recorded and never resolved through the catalog, an item binding
  naming anything but a revision hash is refused; activating a new selection revision changes
  later resolutions and no earlier bound run, and the activation history names who activated
  each.
- Export then import into an empty store reproduces every revision byte-identically and the
  identical ids, names, alias histories, retirement histories, positions, activations and
  measurements; a second import writes nothing; a divergence is refused naming both sides;
  no export narrower than a store root can be produced.
- **Snapshot:** an export taken while a run terminates yields a manifest consistent as of one
  read transaction — the new measurement is wholly present or wholly absent, never present
  under one lineage and missing where the same run is named again.
- **Refusal:** a measurement entry whose derived id the store already holds is accepted only
  on full-record equality and otherwise refused naming the first differing field — proven
  separately for a differing value, a differing source and a differing #24 evidence
  reference, so provenance cannot be discarded by a converging id.
- **No write on refusal:** a refusal at re-hash, at lineage-id rederivation, at cross-lineage
  name or revision uniqueness, or at divergence leaves the store byte-identical to its
  pre-import state — proven over the whole store root before and after, not only over the
  lineage the refusal names.
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
