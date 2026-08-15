# ADR 0007: Named lineages own catalog identity above hash-true revisions

- Status: PROPOSED — round 5, rebased onto `main` `f9ff73c` and answering the Codex
  REVISE of 2026-08-15; not accepted, not implemented
- Date: 2026-08-15
- Depends on: [ADR 0001](0001-durable-runtime.md),
  [ADR 0002](0002-exact-yaml-graph.md), [ADR 0006](0006-node-vocabulary.md)
  (ACCEPTED), and **ADR 0006 amendment A1** below, which acceptance of this record
  requires and which amends an already accepted record
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1),
  whose portable-declarative-file, live-and-versioned-configuration and
  platform-ownership rules this record expresses and never re-decides, and the
  **operator direction of 2026-08-15** on
  [#22](https://github.com/FlexOr2/atelier-2/issues/22#issuecomment-5301973340):
  the operator decides which agents and skills he has and where they come from,
  files in git are the source of truth, atelier-2 imports rather than versioning in
  parallel, and sharing is a future requirement. The landed requirement documents
  this record must not contradict — each `DRAFT`, and never outranking a landed
  decision record — are [0002](../requirements/0002-teams-und-zugang.md) rule 9
  (sharing a library or a workflow is sharing a git source; sources are registered
  globally and selected per project; there is no second sharing channel) and rule 14
  (credentials stay references and are never transported)
- Consistent with, and each on `main`:
  [ADR 0008](0008-budget-units.md) (ACCEPTED), whose recorded meter decision 5's
  consumption entries read; [ADR 0009](0009-runner-trust.md) — merged as
  `1a88dbdd` — which owns the product's one trust boundary, its §6 credentials-by-
  reference rule and its §9 typed actor kinds, and decision 2 states why a definition
  source adds no second boundary; [ADR 0010](0010-github-platform-adapter.md) —
  merged as `87cd5700` — the platform adapter whose stable external reference
  decision 5 cites as evidence; and [ADR 0011](0011-project-isolation.md) — merged
  as `4fa26889` — which **owns where a definition source is scoped** (decision 2
  below defers to it) and whose decision 6 builds on decision 6 below
- Answers: the ADR mandate of
  [#22](https://github.com/FlexOr2/atelier-2/issues/22) — where named lineages live,
  where their definitions come from, and the sync semantics. It **does not close
  #22**, which also carries the operator-visible slice named under *Successor*
  below; the record decides, the slice ships, and the issue closes with the slice.
  Merging this record while calling #22 closed would report an unbuilt picker as
  delivered
- Feeds: [#6](https://github.com/FlexOr2/atelier-2/issues/6) (the catalog, its
  publish gate and its precedence), [#8](https://github.com/FlexOr2/atelier-2/issues/8)
  (the scorecard and the consumption measurements folded into it),
  [#66](https://github.com/FlexOr2/atelier-2/issues/66) (the landed authoring format,
  whose fifth acceptance sentence
  `acceptance/66-agent-as-a-markdown-file.toml` holds back until this record gives the
  definition a durable home — decision 4), and ADR 0006's reference binding, whose
  slice V3-4 builds against the port in decision 9
- Names, never decides: [#16](https://github.com/FlexOr2/atelier-2/issues/16) (sole
  owner of the preserving V7→V8→V9 sequence),
  [#63](https://github.com/FlexOr2/atelier-2/issues/63) (owner of the non-preserving
  V3 cutover these shapes land in, after #16 Phase 2's explicit handoff),
  [#24](https://github.com/FlexOr2/atelier-2/issues/24) and ADR 0010 (platform
  adapter), [#26](https://github.com/FlexOr2/atelier-2/issues/26) (budget),
  [#79](https://github.com/FlexOr2/atelier-2/issues/79) (the queue, a consumer of
  decision 8's precedence and never a second selector),
  [#23](https://github.com/FlexOr2/atelier-2/issues/23) and ADR 0011 (project and
  source scope), [#9](https://github.com/FlexOr2/atelier-2/issues/9) (the library and
  canvas surface), [#38](https://github.com/FlexOr2/atelier-2/issues/38) (a run's own
  purpose and input, which is never a catalog fact)
- Evidence: documentary. Read at `f9ff73c`: ADRs 0001–0006 and 0008–0012,
  `src/atelier2/adapters/dbos/schema.py` (`SCHEMA_VERSION = 7`, `workflow_revisions`,
  `auth_profile_revisions`), `src/atelier2/contracts/agents.py`
  (`auth-profile-revision/v1`), `src/atelier2/contracts/hashing.py` (`frame`),
  `src/atelier2/contracts/workflows_v3.py` (the V3 document's authored `name` and
  optional `description`), `src/atelier2/application/publish_workflow_revision.py`,
  and the authoring format `src/atelier2/contracts/agent_definitions.py` with its
  stated gap, landed with PR #68 as `f9ff73c7` together with
  `acceptance/66-agent-as-a-markdown-file.toml` and
  `tests/domain/test_agent_definitions.py`; issues #1, #6, #8, #16, #22, #23, #24,
  #63, #66, #79. No code changed, no gate run; nothing below is implemented.

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

Three further holes are open, and the operator named the first of them.

**Nothing in the product says where a definition comes from.** A revision enters the
store only by being typed into "Publish YAML", so the operator cannot answer which agents
and skills he has, where they came from, or whether the file he edited is the one that
runs — and sharing one has no mechanism at all.

**Nothing in the store holds what a definition says.** #66's authoring format landed on
`main` with PR #68 as `f9ff73c7`: `src/atelier2/contracts/agent_definitions.py` parses an
agent's `name`, `description`, optional `model`, optional tool declaration and system
prompt out of one markdown file and hashes them as `agent-definition/v1`. But
`agent_configuration_revision_for` can put only model, auth profile, executor and
capability into a published revision, and its own docstring says so: those authored facts
"reach no durable owner through this mapping", so two definitions differing solely in
their prompt publish one revision. `test_todays_catalog_revision_cannot_tell_two_prompts_apart`
pins that as today's truth, and `acceptance/66-agent-as-a-markdown-file.toml` names the
consequence in the landed file itself — #66's fifth sentence, that the definition is
reconstructible from the published revision, is deliberately absent from that gate and
"joins this file in the change that proves it", once #22 gives the definition a durable
home. This record is that home.

**And a lineage has a name with no description beside it.** The landed formats already
carry both: `workflows_v3.py` gives a V3 document an authored one-line `name` and an
optional `description`, and an agent definition requires both in its frontmatter. The
catalog has no rule saying where either lives, so a picker showing "name and description"
has nothing to read them from, and nothing says which label an already-terminated run is
listed under after its lineage is renamed. Decisions 1, 2 and 4 close all three.

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
`selection_policy`, `admission_policy` and `agent_definition`; `auth_profile` is
deliberately absent, and adding a token is an amendment. This closes the set of
**registries**, not the kinds inside one: ADR 0006 leaves context-source, read-operation
and schema kinds open and this record keeps them so. `agent_definition` is added for a
named need and not for symmetry: #66's authored agent file must be admissible into a named
lineage for the operator's library to exist at all, and ADR 0006's `profile`, `skill` and
`tool` are three different things a definition is not.

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

**A name is authored wherever its content is authored**, so that one writer owns it, and
**what decides that is the authoring format, not where the bytes came from.** A kind whose
format declares a name takes the declared one: admitting a member appends the alias event
carrying that name, with the admitting actor, so an authored rename becomes a real
attributed catalog event rather than a silent one, and the catalog offers **no second
rename beside it** — a catalog rename of such a lineage is refused. Both formats on `main`
declare one: `workflows_v3.py` calls its `name` "the one line a picker shows", and an
agent definition's frontmatter requires `name`. A kind whose format declares none — every
V1 and V2 workflow revision — keeps the catalog label, renamed in the cockpit as today.
Origin does not enter this rule: a V3 document typed into "Publish YAML" carries its name
in its bytes exactly as one taken in from a source does, so there is one rule and not a
rule per authoring path. Either way the history is the same shape and the refusals below
are the same, which is why an authored name outside this record's name syntax is refused
**at admission**, naming the file — neither format is narrowed here, and both admit more
than the catalog does. The never-reassigned rule below therefore becomes reachable by an
ordinary file operation: reusing a retired lineage's name in a new file is refused
**naming both**, so the operator renames the file or admits into the existing lineage, and
never discovers later that two histories answer to one name.

**A description is authored the same way, and the catalog stores none.** The operator
contract is a name *and* a description, and both are content: a description is what the
definition says about itself, so decision 4's rule owns it — it lives in the bytes and is
parsed from them, never in a column beside them, and there is no catalog description event
and no cockpit description edit. Where a format declares no description the library shows
**none**, and says so rather than inventing one: an optional `description` absent from a V3
document, and every V1 and V2 revision, whose bytes carry neither field. That absence is a
real thing an operator sees in a picker, and the honest reading is what makes it worth
closing — by authoring a description into the file, which is one edit in the place the
content already lives, and never by the catalog minting a second authority over it.

**A run is listed under the current name, and the alias history is where the rename is
visible.** A run pins a revision hash and no display name ever enters its configuration,
its snapshot or its receipt, so *every* label a run list shows is a projection read at
display time; the only question is which projection, and a stored bound-at-run label would
be a second name authority with no arbiter against the alias history — the failure decision
4 refuses for content and this decision refuses for names. So the label is the latest alias
event of the lineage the bound revision is a member of, and a reader who asks what it was
called then reads the alias history, which is attributed and complete. A run whose revision
belongs to no lineage — every historical V7 revision, and any published-not-admitted one —
is listed by its **revision hash and no name at all**, because there is nothing honest to
show. Accepted and visible price: renaming relabels the operator's own history, and it is
the alias history that keeps the old name from disappearing.

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
lineage starts at `n = 0`, below #8's minimum-n, and decision 8's gate makes its author
name the nearest existing entry.

### 2. Authoring lives in operator-owned sources; the catalog takes content in and never writes back

The operator's two questions — *which* agents and skills he has, and *where they come
from* — have no owner today. This decision gives them one, and it does so by **not**
building a second version control.

A **definition source** is a configured origin of authored content the operator owns:
`(source id, source kind token, location, ref, credential reference, selections)`, where a
**selection** is `(path pattern, kind token)`. The source kind token is closed to **`git`**
in V1; `marketplace` is named as the future token and adding one is an amendment, because
nothing calls it today and a source type without a caller is architecture on speculation. A
private source names its credential **by reference, never as material**, exactly as ADR
0009 §6 requires of every credential in the product.

**Whose source it is — the installation's or one project's — is not decided here.** ADR
0011 decision 1 owns that scope and has decided it: the **register** of definition sources
and libraries is installation-level, the **selection** of registered sources is a project
bundle revision, and the resolution order is one sentence — the project's value wins,
absence in the project inherits the installation's, and an explicit deselection is never
re-inherited. Requirement 0002 rule 9 states the same split in the operator's own words.
This record therefore decides what a source *is* and what the three acts below do, and it
adds no second scope rule: it holds for a source registered once and selected by one
project exactly as for one selected by five. What follows from ADR 0011 decision 4 and is
not restated as a rule of this record: intake copies bytes into the **taking project's**
store, the reference direction is project → source, and a source learns nothing about which
projects took from it.

**A file's kind is configured, never inferred.** The operator says which paths hold agent
definitions, which hold skills and which hold workflow documents; a file matching two
selections is refused naming both rather than resolved by precedence. Guessing a kind from
an extension or from a file's contents would let a repository decide which registry its
content enters, which is exactly the authority a configured source is not given.

**Authoring happens in the file, and atelier-2 never writes a source.** Git already owns
versioning, diff, review and sharing; a parallel history inside the catalog would rebuild
a solved problem without any of that tooling, and it would put a second writer on the one
fact — what a definition says — that decision 6 already assigns to the bytes. The catalog
takes content **in**; it does not edit it, and the cockpit offers no edit-and-commit path
back. Editing a definition means editing its file.

The operator's direction calls this importing. This record says **intake** for it, because
*import* already names decision 6's store-to-store transport, and the two acts have
different inputs and different refusals; they are never the same command.

**Three acts, three recorded facts, and only the first may be automatic.**

- **Scan** reads a configured source at its current position and computes the content hash
  of each selected file. It writes no revision and admits nothing, so it may run on a
  schedule or on demand — reading is the only part of this chain that risks nothing.
- **Intake** publishes one scanned file as an immutable revision (decision 3) and records
  `(revision hash, source id, path, exact source position, actor, taken_at)`. A git source
  is configured with a branch or a ref, but the record pins the **exact commit** read, so
  which upstream state produced a revision stays answerable after the branch has moved.
- **Admission** binds that revision into a lineage under decision 8's gate, unchanged. One
  operator action may perform intake and admission together; they remain two recorded
  facts and two refusals.

**Intake copies bytes.** The store holds a taken-in definition as an ordinary immutable
revision, and a source is never a runtime dependency: nothing is read from a source at run
time, and a deleted or unreachable upstream repository cannot invalidate a receipt, which
is what ADR 0001/0002 reconstruction requires.

**A path is the continuity of a source-owned lineage.** A lineage id derives from its
founding revision (decision 1), so a second revision joins a lineage only by being admitted
into it, and something must say which. For source-owned content that is the path: successive
intakes from the same `(source id, path)` are admitted into the lineage the previous intake
from that path joined. **The declared name establishes nothing**, because a name is a
mutable label and reading identity out of it would reintroduce the rename-forks-identity
failure this record exists to prevent. Consequence, accepted and visible: moving or renaming
a file is a new path with no prior admission, and the catalog offers no lineage for it
rather than guessing — the operator admits it into the existing lineage himself, an ordinary
admission with an ordinary actor.

**Sync semantics: publication is explicit, and drift is visible.** Drift is derived, never a
state anyone sets, and it is two independent readings that mirror this record's own
publication-versus-admission split:

- **source against store** — `in_sync` when the source's current content hash at a selected
  path equals the latest revision intaken from that path, `source_ahead` when it does not,
  `source_absent` when the file is gone or no longer selected. There is no fourth reading and
  no "store ahead", because the catalog never writes a source.
- **store against catalog** — whether that latest intaken revision is `admitted` into its
  lineage or `taken_not_admitted`, which is decision 3's two states read through one path.

A scan's observation of the source may be cached so the library reads without touching the
network, but it is an **observation, not a fact**: it can be dropped and rebuilt by
rescanning, and nothing durable depends on it. `source_absent` removes no revision and
retires no lineage — a revision already taken in is immutable, and retirement stays decision
1's explicit attributed event.

The rejected alternative is **auto-intake on change**, and it is rejected on a bound stated
honestly rather than an exaggerated one. It would *not* rebind a running or an already
published run configuration — those pin hashes, and #1 forbids anything else. Nor is it
rejected for being unattributable: ADR 0009 §9 already has an actor kind for it, an `agent`
holding its own credential and acting under a published policy revision, so a poller could
be made attributable and the argument that no actor exists would be false. What it would do
is put content the operator has never seen one `resolve_name(…, head)` away from his next
authored run — the moment a definition he did not read becomes the head his next authored
binding resolves to. That is the whole of the reason, and it is enough: the operator asked
to decide **which** agents he has, and content arriving without him reading it is exactly
that decision taken from him. Explicit publication costs one operator action per upstream
change; the drift reading is what keeps that action from being forgotten. Should the
operator later want it, the shape is already named — an enrolled `agent` actor under a
published intake-policy revision, made current through `catalog_policy_activations` like
every other policy — and it is an amendment to this record, not a switch inside it, because
nothing calls it today.

**A source is a content trust decision, and it creates no second trust boundary.** ADR
0009's one boundary is service ↔ runner, and a source crosses nothing: it does not
authenticate, does not execute, and writes no durable truth. Everything it offers is inert
bytes until an attributed intake, and inert for execution until admission — so a foreign
repository cannot make anything bindable by pushing, and a foreign definition passes the
same gate, the same admission refusals and the same attribution as one the operator typed
himself. Configuring the source is where the operator decides to trust its content, and it
is his decision to record, not the catalog's to infer.

**Sharing, in V1, is a shared repository.** Sharing an agent, a skill or a whole workflow
means pushing its file to a repository the other operator configures as a source; git is
already the mechanism and nothing is built for it, and requirement 0002 rule 9 says the
same in the operator's words: there is no second sharing channel. Decision 6's
complete-store export is deliberately *not* that channel — it is the catalog's transport
and carries measurements and lineage, and sharing a definition must not hand another
operator a balance — and ADR 0011 decision 6 keeps that split, adding no third form.

**The library is a view, never a silo.** It holds no fact of its own: the display name of a
lineage whose format declares none, the kind, the source and position, the head revision and
the drift are owned here; the name and the description of every kind whose format declares
them are owned by the bytes (decision 4); the scorecard is #8's. It is a view over sources
*and* revisions together, so an entry the operator can see but has not taken in is visible
as such rather than absent. Scanning and drift belong to the source adapter; they are not
operations of decision 9's resolution port, which stays at three.

### 3. Content publication and catalog admission are two states, two commands

Conflating them contradicted ADR 0006's publishable-before-executable staging. Intake
(decision 2) is a caller of publication, never a third state beside it.

- **Content publication** writes immutable revision bytes keyed by their hash. It exists
  today (`publish_workflow_revision`), requires no lineage, and per ADR 0006 is permitted
  before the runtime can execute the revision and before the catalog exists at all. It is
  never refused for lack of a catalog.
- **Catalog admission** binds an already-published revision into a lineage at a dense
  position, under decision 8's gate. It is optional until the catalog exists; once it does,
  one published immutable revision of kind `admission_policy` names, per kind, whether an
  authored reference of that kind must bind an admitted member. It is made current exactly
  as decision 8's selection revision is — one entry in the append-only, attributed
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

### 4. What a definition says lives in its bytes and in nothing beside them

This is the durable home the landed #66 format has no owner for, and it is the same rule
ADR 0002 already applies to a workflow document, extended to every kind the catalog names.

**The authored file's exact bytes are the published revision.** An agent definition, a
skill and a profile enter the store the way a workflow document enters it today: exact
UTF-8 bytes, SHA-256 identity, immutable, never re-parsed for identity. Reconstruction is
therefore reading the bytes by hash and parsing them with the authoring format's own
parser — `parse_agent_definition` for an agent definition, the V3 document parser for a
workflow — so the definition comes back byte-identically, including its name, its
description, its system prompt and its tool declaration. That is #66's fifth acceptance
sentence, the one `acceptance/66-agent-as-a-markdown-file.toml` deliberately withheld until
this record exists; it becomes provable here rather than narrowed there, and it joins that
file in the change that builds it.

**The contract shape, named; the columns are the schema owners'.** One content shape keyed
by revision hash, carrying the exact bytes and the **kind token** of the registry the
revision belongs to. `workflow_revisions(revision_hash, document)` is that shape today for
exactly one kind; carrying the kind is the schema consequence, and which store version
takes it belongs to the owners named under *Store dimensions* below. Nothing here decides a
column.

**Per-attribute columns are refused.** A `name`, `description`, `prompt` or `tools` column
beside the bytes would make the store a second authority over what a definition says, and
the first disagreement between a column and its bytes would have no arbiter. Every field
the library shows — including the name and the description the operator's picker needs — is
parsed from the bytes; anything stored beside them is a projection that can be dropped and
rebuilt, never a fact. The one thing that is *not* parsed from the bytes is the display name
of a lineage whose format declares none, which is decision 1's catalog label and exists
precisely because those bytes say nothing about it.

**The deployment half stays separate, and that is what makes a file portable.** Which
model, which auth profile and which executor a deployment runs an agent under is authored
in no file and stays the agent-configuration revision's, which references the definition
revision by hash. A definition carries no deployment fact, so it can be shared into another
operator's atelier — decision 2's sharing — without carrying this one's credentials.

**This adds no binding path.** An `agent_definition` revision is bound where ADR 0006
already binds a role: by the run-start command, to one exact revision hash. It carries no
`ref` in any document, so it binds through the lineage-free `resolve` of decision 9, and
its lineage exists for the library, the scorecard and the operator's rename — not for
reference resolution.

### 5. Measurements are an append-only ledger with cross-store-stable identity

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

#8's consumption measurements — tokens, duration and cost per run — ride this ledger
unchanged, one entry per kind per terminated run, sourced `run_terminal` from ADR 0008's
recorded meter. **Which kinds exist, and how a subscription's consumption is labeled
rather than priced, stay #8's and ADR 0008's**; this record decides only that they need no
second store and no second identity.

**Surviving a schema cutover: the declaration is `reset`.** ADR 0001 gives no in-place
migration, so measurements survive only by explicit export and import, and what is not
exported is reset visibly: the scorecard reports `n` from this store and names the
cutover as why the count begins there. No gaming hole, because a cutover resets the whole
catalog at once and cannot be aimed at one entry.

### 6. The store is the catalog, and the only file form is a complete export

**Content truth** — what a revision says — is the exact bytes: republished into any store
they yield the same SHA-256, so a definition travels as a file, as #1 requires.
**Relational truth** — lineage, position, selection, measurement — exists only as store
rows, because a git-file catalog would have to write #8's runtime measurements back on
every run: a second durable writer with none of the store's transactions. **The revision
hash is the join, and neither side may state the other's fact.** At run time nothing is
read from a file; a file enters only through an explicit intake or import command.

Decision 2 gives the file side its owner and sharpens this split rather than reopening it:
**git owns authored content and its history, the store owns execution identity and every
relational and runtime fact.** Each has exactly one writer, and the intake record is the
only row that names both.

**A transport therefore carries exactly one form: the complete catalog of one store
root.** Selective bundles are removed — they were the hole in the anti-gaming claim, since
a bundle chosen to omit one lineage's measurements is exactly the aimed reset decision 5
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

- `catalog-export-manifest/v1`: the lineage sequence, then the policy-activation sequence,
  then the source-intake sequence — lineages ordered by ascending lineage id, activations by
  ascending `(policy kind, activated_at, revision hash)`, intakes by ascending
  `(revision hash, source id, path, source position)`.
- `catalog-lineage-entry/v1`: lineage id, kind, founding revision hash, then four sequences
  in this order — aliases and retirements in activation order, members by ascending
  `revision_number`, measurements by ascending measurement id.
- `catalog-alias-entry/v1`: name, actor, activated_at. `catalog-retirement-entry/v1`: state
  token, actor, activated_at. `catalog-member-entry/v1`: revision_number, revision hash.
  `catalog-policy-activation-entry/v1`: policy kind, revision hash, actor, activated_at.
- `catalog-measurement-entry/v1`: measurement id, revision hash, measurement kind, value,
  source token, evidence reference — zero-length exactly when the source carries none.
- `catalog-source-intake-entry/v1`: revision hash, source id, path, source position, actor,
  taken_at. Intake entries are top-level because an intake is about a revision, not about a
  lineage, and a published-not-admitted revision has no lineage to sit under.

**The source configuration itself is never exported.** Its location and its credential
reference are the receiving operator's business in neither direction, and an exported
source id resolves to nothing there. The intake entry travels anyway, as **evidence of
provenance and not as a resolvable pointer**: it answers where a revision came from, which
is the operator's own question, and it makes drift recomputable in the store that has the
source.

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
5. every intake entry is checked to name a revision the manifest carries, refused naming
   both, so provenance can never arrive for bytes the transport did not;
6. **only then does the first write happen, in one write transaction.**

A refusal at any step leaves the store byte-identical to its pre-import state. An import
the store already holds, whole or as a prefix, converges without a write.

### 7. Auth profiles are excluded from this generalization

`auth_profile_revisions` is not folded in and keeps its embedded
`(profile_id, revision_number)` shape. Its revision hash is framed over
`auth-profile-revision/v1` with `profile_id` and `revision_number` *in the preimage*, so
retiring those dimensions would change every existing auth-profile revision hash — a
different identity, invalidating every agent-configuration revision referencing one.
`auth_profile` is not a catalog kind, and a profile is not renameable. Any unification needs
its own successor-identity contract — the old-to-new hash mapping, what happens to
referencing agent-configuration and run configuration revisions, and whether history is
reconstructed or reset.

### 8. The publish gate and the full precedence, both versioned

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
finish. #79's queue assigns a workflow to a work item through exactly this precedence and
adds no second selector: it may author the item binding the platform then owns, and its
readiness and automation rules are its own, but which workflow a type gets is the
selection revision's answer or it is two answers.

### 9. The resolution port ADR 0006's reference binding builds against

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

- **Source configuration:** a source kind token outside the closed set; a selection naming a
  kind token outside decision 1's closed set; a source carrying credential material instead
  of a credential reference (ADR 0009 §6).
- **Scan and intake:** a file matching two selections, naming both; a scanned position that
  no longer resolves in the source, naming it; bytes that changed between the scan and the
  intake, naming both content hashes, because an intake publishes exactly what was scanned
  and never what has since appeared; a path outside every selection.
- **Admission:** a revision not published; a name outside 1–128 characters of
  `[a-z][a-z0-9._-]*`, and where the authoring format declares the name that is the
  **authored** one, refused naming the file; a name of exactly 64 lowercase hexadecimal
  characters, which would be indistinguishable from a lineage id; a name currently or
  previously held by another lineage of the same kind, naming both; bytes owned by another
  lineage of the same kind, naming it; a missing #6 Rev. 2 justification; a retired lineage;
  a catalog rename requested for a lineage whose authoring format declares its name, naming
  the file that owns it; any catalog write of a description, for every kind, since no format
  and no cockpit path may put one beside the bytes.
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
  entry duplicating an existing derived id in **any** field, naming the first that differs;
  an intake entry naming a revision the manifest does not carry, naming both.
  A refused import writes nothing.

## Store dimensions and their migration cost

Named honestly, for the two schema owners below. **Seven durable shapes, every one
append-only, not one updated in
place:** `catalog_lineages` (id, kind, founding revision hash — no mutable column at all),
`catalog_lineage_aliases` and `catalog_lineage_retirements` (decision 1's two attributed
event histories), `catalog_lineage_members` (`(lineage, revision_number, revision_hash)`,
both constraints), `catalog_measurements` (the ledger), `catalog_policy_activations`,
the single owner making the selection, scorecard and admission revisions current, and
`catalog_source_intakes` (decision 2's provenance record, unique on
`(revision hash, source id, path, source position)`). The sixth shape is the price of
removing the last in-place update, and it pays for itself by giving the scorecard policy
the activation owner it otherwise lacked. The seventh is the price of the operator's own
question — without it "where does this come from" has no answer and drift cannot be
computed at all. Nothing retires `auth_profile_revisions` (decision 7).

Two further consequences are named here and decided nowhere else in this record. The
revision-bytes shape gains the **kind token** decision 4 requires, so one content store
serves every registry rather than one table per kind. And a **definition source is
configuration, not catalog data** — it varies by operator and deployment, it carries a
credential reference, and it is therefore #1's configuration surface, never a store shape
and never exported. Which configuration level holds it is ADR 0011 decision 1's, not this
record's: registered at the installation, selected per project.

**The cost is one cutover, and it is a cutover ADR 0006 already requires — and the boundary
between the two schema owners is named rather than left to whoever builds first.** None of
these shapes, and not the kind token above, may enter **#16's preserving V7→V8 or V8→V9
phases**: a preserving migration cannot invent a lineage for revisions that never had one,
and inventing one per existing revision would fabricate exactly the founding facts #8
aggregates over. #16 stays the sole owner of that preserving sequence and hands the next
exact store version on. **They land in #63's non-preserving V3 cutover**, which by its own
Schema-ownership section owns that cutover after #16 Phase 2 and takes the next exact
version by explicit handoff — never two versions writing at once and no in-place
reinterpretation of V9 data as V3. So the routing is: #16 for everything preserving, #63
for the cutover these shapes ride, and neither has a decision left to invent here. The
catalog then starts empty, and existing V7 revisions stay published-not-admitted until
admitted — which costs nothing, since identical bytes yield the identical hash.

## Consequences

Prices, stated once. Renaming costs an alias history and a name never reusable across
lineages of one kind; a re-readable ranking costs computing the scorecard on read; a
*catalog* travels only as a complete store root; every mutable fact costs a projection over
an event history rather than a column read; ADR 0006 pays amendment A1 and one added port
operation; and a cosmetic fork can still start a fresh balance, made visible here rather
than prevented.

The source model has its own prices. **Keeping content up to date costs one operator action
per upstream change** — the drift reading is what makes forgetting visible, not what
removes the action. **Authoring moves out of the cockpit** for anything a source owns:
"Publish YAML" stays the path for entries the operator types, and the catalog rename stays
the path for kinds whose format declares no name, so the product carries two authoring paths
with one rule deciding which applies. **Sharing gains a mechanism and no automation**: a
shared repository is the whole of V1, a marketplace is a source kind nobody has written, and
neither is a channel for measurements. And a definition is only as trustworthy as the source
the operator configured — this record makes the origin visible and attributable, it does not
judge the content.

The name-and-description model has two prices the operator will see. **A rename relabels his
own history**: yesterday's runs are listed under today's name, and the alias history is
where the old one survives — the alternative, a bound-at-run label stored beside the run,
buys a stable list at the cost of a second name authority no reader could arbitrate.
**A pre-V3 entry has no description and never gets one from the catalog**: every V1 and V2
revision's bytes carry neither field, so its picker row shows a name and an empty
description until someone authors the content forward. Nothing is hidden, and nothing is
invented to fill the row.

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
- **The run label follows the rename:** a terminated run bound to a member of a lineage is
  listed under the lineage's current name before and after a rename, the alias history names
  the earlier one with its actor, and no run configuration, snapshot or receipt row changed
  in either state — proven by comparing the whole run record before and after, not only the
  label. A run bound to a published-not-admitted revision is listed by its revision hash with
  no name and no invented one.
- **Name and description come from the bytes:** for each kind whose format declares them —
  a V3 workflow document and an agent definition — the picker's name and description are the
  parsed authored values, changing them in the file and re-admitting changes both, and no
  store row beside the bytes carries either. A V3 document with no `description`, and a V1 or
  V2 revision that can carry neither field, read back as **absent** rather than as an empty
  or defaulted string, and a catalog write of a description is refused for every kind.
- **The name's owner is the format, not the origin:** a V3 document typed into "Publish YAML"
  and one taken in from a source both take the authored name, and a catalog rename of either
  is refused naming the file; a V1 or V2 revision, whose format declares no name, is renamed
  in the cockpit and its alias event carries the operator as actor.
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
- **Sources, scan and drift:** scanning a configured source publishes nothing and admits
  nothing — proven over the whole store root before and after; the source reading is
  `in_sync` after an intake, `source_ahead` once the file changes, and `source_absent` once
  it is removed or deselected, while the catalog reading is `taken_not_admitted` until
  admission and `admitted` after it, each proven independently of the other; a
  `source_absent` file leaves its revision and its lineage untouched; and dropping the
  cached observation and rescanning yields the identical readings.
- **Path continuity:** a second intake from the same `(source id, path)` is admitted into
  the lineage the first joined and becomes its head; the same content at a different path
  founds no lineage by itself and is offered for explicit admission instead; and a file
  whose declared name changed keeps its lineage, while a file that only moved does not
  acquire one silently.
- **Configured kind:** a file matching two selections is refused naming both, and a scanned
  file enters exactly the registry its selection names — never one inferred from its
  extension or its contents.
- **The catalog never writes a source:** a configured source's working tree and its refs
  are byte-identical after a scan, an intake, an admission, a rename attempt and an export.
- **Intake:** an intake publishes exactly the scanned bytes and is refused naming both
  content hashes when the file changed in between; the published revision carries the exact
  source position it was read at, and still resolves after that branch has moved and after
  the source is unreachable; the same file taken from two sources converges to one revision
  carrying two intake records.
- **Nothing a source says is executable by itself:** a revision taken in but not admitted is
  refused by `resolve_reference` and never selected by decision 8's precedence; admission
  requires a named actor and the #6 Rev. 2 justification whether the bytes were authored in
  the cockpit or pushed by a stranger.
- **An authored name reaches the catalog as an event:** admitting a member appends one alias
  event carrying the file's declared name with the admitting actor; and an authored name
  outside the name syntax, or of exactly 64 hexadecimal characters, is refused at admission
  naming the file.
- **Reconstruction (#66):** a published `agent_definition` revision parses back through
  `parse_agent_definition` to the identical definition — name, description, model, tool
  declaration and system prompt — and two definitions differing only in their prompt publish
  two distinct revisions, which is exactly the equality
  `test_todays_catalog_revision_cannot_tell_two_prompts_apart` asserts today. That test's
  own docstring says it "pins the boundary so the day it moves is visible": the day it moves
  is this proof, and #66's fifth sentence is declared in
  `acceptance/66-agent-as-a-markdown-file.toml` in the same change.
- Export then import preserves every intake record; an intake entry for a revision the
  manifest does not carry is refused naming both; and no source configuration leaves the
  store.
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
finally names; any successor identity for auth profiles (decision 7); platform addressing,
authorization, event observation and the item-level binding's storage (#24, ADR 0010);
budget units (#26); the preserving schema sequence (#16) and the exact cutover version
(#63); project and source scope (#23, ADR 0011); the library and canvas surface (#9); a
run's own purpose and input (#38); and any conversational authoring surface above the
catalog (#7).

From decision 2, four things are deliberately not decided. **How a git source is read** —
clone or fetch, protocol, scan cadence, rate limits — is implementation under this record,
not a decision it makes. **The marketplace source type** is named as a future token and
designed nowhere. **The library's layout and interaction** are the cockpit's; this record
fixes only which facts it may show and who owns each. And **the queue's triage, readiness
and automation rules** are #79's, which consumes decision 8's precedence rather than
extending it.

## Successor

A decision record is not an implementable slice, and this one deliberately does not become
one by being longer. The smallest honest successor is **workflow-only and visible**: the
saved-workflow picker and the runs list gain the authored display name, the authored
description, the lineage and the revision, the exact hash moves under Details, and a
revision belonging to no lineage renders as decision 1 says it must — its hash, no name, no
invented description. Its proof is authoring, the rename projection over an already
terminated run, exact revision binding at run start, the API and store behaviour behind
both, the component's own states, and one thin desktop-and-390px journey.

It builds **no** library surface, no graph, no scorecard, no source adapter, no sync and no
export: this record decides those so that the slice after it has an owner to build against,
not so that they ship together. The slice belongs to #22, which is why this record does not
close it, and the decisions it consumes are 1, 3, 4 and 9 — sources (decision 2), the
ledger (5), transport (6) and precedence (8) wait for callers of their own.

## Supersedes

None. This record extends [ADR 0002](0002-exact-yaml-graph.md), still the owner of
revision identity, and [ADR 0006](0006-node-vocabulary.md), still the owner of the
document surface and its reference form, which amendment A1 clarifies rather than changes.
