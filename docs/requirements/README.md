# Human requirements

Audience: humans and agents binding work to an exact statement of human need.

## Requirement documents

A requirement document is the **derived reading** of one issue thread: the
sentences that are still alive in it, gathered in one place, so that a builder
does not have to reconstruct today's intent from a hundred comments in which a
superseded one stands beside the amendment that replaced it.

It is a view. It is not an authority, and that distinction is the whole
convention:

- An **issue** is the live thread and the source. It carries the operator's
  wish, the push-back, the adversarial review, the amendment, and the ruling —
  in that messy order, with earlier comments left standing after later ones
  replace them.
- A **requirement document** reads that thread to its end. It never creates,
  changes, or extends human intent, and every sentence in it must be walkable
  back to a named object in the thread.
- **The source wins.** A later operator ruling in Issue #1 or in the document's
  `Owner-Issue` binds from the moment it is posted, whether or not this
  directory has caught up. A reviewed pull request may correct a document; no
  pull request can make a requirement.
- **No document invents.** Where the thread is undecided, the document says so
  under `Open questions` instead of choosing. A rule with no source is removed,
  not sourced afterwards.

The section *The editable human requirement* below forbids copying or
independently reinterpreting Issue #1 here. A distillation stays on the right
side of that line exactly as long as every rule names the object it came from
and the source outranks the document. A rule that cannot be walked back is the
independent reinterpretation that section forbids.

A requirement document is also **not** a published requirement revision. The
immutable, machine-bound revisions that section describes do not exist yet;
nothing in this directory is one, no run binds one, and a hand-written reading
never becomes one by being reviewed.

### Naming and header

One file per subject, `NNNN-<subject>.md`, numbered in creation order and never
renumbered. The number is the document's identity; a superseded document keeps
its file and its number.

Every document opens with these fields, in this order:

```text
Status:         DRAFT | AGREED | SUPERSEDED
Owner-Issue:    the one issue that owns this subject
Source-Threads: every issue whose discussion this document distils
Distilled-From: the source objects this revision read — comment ids, and issue
                bodies bound by digest (see Provenance)
Approved-By:    the comment id of the operator approval, or none
```

`DRAFT` is the default and means that no operator approval is bound:
`Approved-By` is `none`. A builder may read a draft and may not treat the
document as having settled anything. Rules graded `OPERATOR` still bind — but
through their source, never through this file.

`AGREED` means the operator approved **this exact document as a reading of its
thread**, and `Approved-By` names the comment in which that happened. It never
means a requirement was settled here.

Approval of *intent* has one destination, and it is not this directory. Issue #5
rules it (comment 5294316639): *„akzeptierte Visionen werden EIGENE
Requirement-Issues; #1 bleibt V1-Vertrag + Index."* A vision the operator accepts
is therefore published as its own requirement issue; a document then reads that
issue and names it as its `Owner-Issue`. Nothing in this directory creates,
accepts, or amends a requirement at any status: an `AGREED` document is a derived
reading whose source still wins where the two disagree, exactly as a `DRAFT` one
is. There is no second route, and an approval that graduates a vision takes #5's.

`AGREED` is not a status the engineering desk can grant: a reviewed pull request
also judges whether a distillation is faithful, and faithfulness is not approval.
A document whose `Approved-By` is `none` is never `AGREED`.

`SUPERSEDED` names the document that replaced it and is otherwise left
untouched.

### Body

Four sections, in this order, each of which may be empty but not absent:

- `## Intent` — the sentences that say what the human wants. Short, plain, and
  attributed.
- `## Rules` — the sentences a builder is expected to satisfy. Existing
  documents 0001–0004 write them as numbered rules. New documents write them
  as `REQ-<bereich>-<nn>` blocks under the sentence template below. Every
  rule carries its **authority grade** and its source:
  - **`OPERATOR`** — the cited object reproduces the operator's **own sentence**,
    verbatim and in the language he wrote it, and that sentence says what the
    rule says. The document repeats the quote at the rule, so the human intent
    is readable without leaving the page. This is the only grade that carries
    human intent.
  - **`DESK`** — everything else: an engineering reading, a sharpening, a
    consequence of a decision record, an observation of the current code, a
    machine-review finding. It records what the desk currently believes is
    right; it binds nothing until an operator rules it.

  The grade is decided by the operator's voice in the object, never by a label
  on it. Every comment in these threads is posted from the operator's account
  and most of them are written by the desk, so the account settles nothing; his
  own sentences are recognisable as his — German, lower case, spoken — and they
  appear as quotations. Four things that look like operator authority and are
  not, all of them `DESK`:

  - an **attribution** the desk wrote — `ERGÄNZUNG (Operator)`,
    `OPERATOR-ENTSCHEIDUNG (bindend)`, `(Operator ausdrücklich)`. It is a claim
    about the object, not a voice inside it, and a desk that could certify its
    own prose as the operator's would be the second authority this convention
    exists to refuse;
  - a **rendering** the thread marks *wörtlich sinngemäß*, which the Provenance
    section already forbids promoting to a transcript;
  - an operator **question**. Quoting „gut oder Push-back?" records what he
    asked; the answer written under it is the desk's;
  - an operator **rejection** that says what he does not want without saying
    what to build. The sentence is quoted; the design answering it is `DESK`.

  Where a quoted operator sentence carries only the core of a rule, the rule is
  graded by that core and names the elaboration around it as desk detail. A
  `DESK` rule that narrows or replaces an `OPERATOR` sentence says so at the
  rule, because that is a proposal to the operator and not a settled correction.

  A rule that replaced an earlier one names the one it replaced, so that a dead
  sentence cannot be revived by someone who read only the older comment.
- `## Open questions` — what the thread left undecided, and who owns deciding
  it. An open question is honest debt; a silently resolved one is a fabricated
  requirement. New questions name an owner. The four existing documents still
  carry questions without one (Issue #163 Phase-1 inventory); that is named
  debt for their migration, not a licence to add more ownerless ones.
- `## Acceptance` — the acceptance sentences this requirement expects, and which
  of them a story has already declared. A document names a declared sentence by
  its identifier; where no story has declared one yet, it says so instead of
  inventing an identifier. Under the sentence template, this section is a
  reading of the `Beweis` fields — see below.

### Sentence template

A new requirement document writes each rule as a stable sentence with its own
identifier. The shape is the one
[Issue #163](https://github.com/FlexOr2/atelier-2/issues/163) published; the
field names stay in that language so a later gate can find them:

```markdown
### REQ-<bereich>-<nn>: <ein kurzer, testbarer, implementierungsfreier Satz>
Status:     DRAFT | AGREED | SUPERSEDED (AGREED nur durch Operator-Kommentar)
Quelle:     <Issue/Kommentar-IDs, Operator-Sätze wörtlich zitiert>
Begründung: <warum es dieses Requirement gibt, 1-3 Sätze>
Journeys:   <Verweise, optional>
Beweis:     <acceptance-Identifier ...> | UNGEBUNDEN (ehrlich benannt)
Offen:      - <Frage> (Eigentümer: <wer>, Ziel: <Runde/Item>)  [nur DRAFT]
```

This sits **under** the document header and the four body sections. It does not
replace them. `## Rules` is the section that contains the blocks. `Quelle`
opens with the authority grade (`OPERATOR` or `DESK`) and then the source
objects. The grade is still decided by the operator's voice in those objects,
by the same four refusals listed under Body.

The identifier is the sentence's identity. `<bereich>` is a short uppercase
token naming the subject, chosen when the document is created and never
renamed. `<nn>` is a two-digit number. An identifier is never reused and never
renumbered. A superseded sentence keeps its identifier and points at its
successor.

`AGREED` on a sentence is not `AGREED` on the document. The document status
still means the operator approved this exact reading of the thread. A sentence
becomes `AGREED` only in an operator comment that names that identifier. The
desk cannot grant either status.

`Beweis` is an identifier that exists in `acceptance/`, or the word
`UNGEBUNDEN`. Nothing in between. A bound identifier is a claim that a story
declared that sentence for this requirement; it is not itself the proof — the
acceptance gate still judges the test. `UNGEBUNDEN` on an `AGREED` sentence is
named debt. Documents 0001–0004 do not yet carry this field; they keep numbered
rules until their migration.

`## Acceptance` is a reading of those `Beweis` fields: it lists the identifiers
already declared and names what is still `UNGEBUNDEN`. It does not bind a
sentence a second time. Where they disagree, `Beweis` is the owner and the
section is wrong.

`Offen` is allowed only while the sentence is `DRAFT`, and every entry names an
owner. An `AGREED` sentence has no open question.

`Journeys` may be empty. `docs/journeys/` does not exist yet; a pointer there
today would be a dead link.

The existing provenance convention is not relaxed: every sentence still names
its source object, in `Distilled-From` and again at `Quelle`. A sentence with
no source is removed, not sourced afterwards.

This change does not add a machine check over the template. The shape is
written so one can be added later without inventing the fields.

### Provenance

The distillation rule is what makes a document checkable: **a requirement
document names the source object behind every rule**, in its `Distilled-From`
header and again at the rule itself. A reader who doubts a rule opens exactly
the object it came from.

Neither kind of GitHub object is immutable — a comment can be edited as easily
as a body — so a citation binds by identity, and additionally by digest wherever
the object is routinely rewritten:

- **A comment id** names one authored act at one moment: `5302769095`.
- **An issue body** is rewritten in place with every revision, so a body
  citation carries the SHA-256 of the body as it was read. `Distilled-From`
  records the digest in full; a rule repeats its first eight characters, as
  `#82 body @ fe6fd31f`. The digest is taken over the body's exact bytes and is
  reproduced with

  ```console
  $ gh api repos/FlexOr2/atelier-2/issues/82 --template '{{.body}}' | sha256sum
  ```

  A Go template writes the field and appends nothing; `gh api … | jq -j '.body'`
  is the two-tool equivalent. Citations taken before this correction used
  `--jq '.body'`, which appends a newline the object does not carry. They stay
  valid under the recipe their own document named and are not rewritten —
  [ADR 0010](../decisions/0010-github-platform-adapter.md) §5 names which landed
  citations carry that form — and each is recomputed only when its document is
  refreshed against its source.

A rule with no source object behind it is an invention, and is removed rather
than sourced afterwards.

A sentence the thread attributes to the operator is quoted **in the language it
was written**; the distillation around it is English, like the rest of `docs/`,
because translating a literal sentence is reinterpreting it. Where the thread
marks its own quotation as a rendering rather than a transcript — these threads
write *wörtlich sinngemäß* — the document repeats that qualifier instead of
promoting the quote to a transcript.

### Freshness

A hand-written view goes stale silently, and nothing in this repository can
notice that a thread has moved on. The convention answers that with a visible
watermark rather than a promise: `Distilled-From` names every source object the
revision read, so a reader can open the thread and see at a glance what arrived
after it. A document is refreshed by rewriting it against its source, never by
editing it into a second opinion.

Until a generator and a freshness check exist, what this directory offers is
honest provenance, not currency. That is the second reason the source outranks
the document.

### Precedence

1. [GitHub Issue #1](https://github.com/FlexOr2/atelier-2/issues/1) stays the
   editable authority for the top-level human requirement. A requirement
   document never outranks it; where they disagree, the document is wrong and is
   corrected.
2. A document's `Owner-Issue` outranks the document for its own subject. A later
   operator ruling there binds immediately, and the document follows.
3. A requirement document outranks a derived view of intent that has no source
   of its own, including the intent statement in
   [docs/PRODUCT.md](../PRODUCT.md) — not because the document is an authority,
   but because it is the closer reading of one.
4. A requirement document never outranks a landed decision record. Where it asks
   for something an ADR forbids, that contradiction is an open question, not a
   silent override.

### Index

- [0001: Items are prioritised, get their workflow, and run from a queue](0001-queue-und-autonomie.md)
- [0002: Access is an invitation, and the installation is the team's workshop](0002-teams-und-zugang.md)
- [0003: One workshop, three views, one language — the graph](0003-ziel-ui.md)
- [0004: Execution happens anywhere, and one trust boundary is what makes that safe](0004-runner-und-remote.md)
- [0005: Named lineages, not hashes, are what the operator picks](0005-katalog-und-benannte-workflows.md)

Each document carries its own status; this index deliberately does not repeat
it, because a second copy of a status is the next thing to go stale.

## The editable human requirement

The only editable HumanRequirement authority is
[GitHub Issue #1](https://github.com/FlexOr2/atelier-2/issues/1). Change the human
requirement there; do not copy or independently reinterpret its content here.

This directory will own immutable published requirement revisions and their
provenance once that capability exists. The publication format remains undecided
until its owning story and decision record define it, and no published
requirement revision exists yet.

Half of the intended trace does exist, and that half was reserved for a story
*and* a decision record together: Issue #94 is the story, and
[ADR 0012](../decisions/0012-acceptance-trace-format.md) is the record. Each
literal acceptance sentence is bound to its proof, and
`.github/workflows/ci.yml` refuses a pipeline where that binding is missing. The
other half — binding the same sentence to an immutable requirement revision —
waits on the publication format above, so a declared sentence names its story,
not a revision.

## Acceptance trace

Where a sentence is stored, how a test claims it, and what counts as evidence
that the claim was honoured are decided in
[ADR 0012](../decisions/0012-acceptance-trace-format.md). This section is how a
story uses those decisions; where the two disagree, the record wins.

A story declares its acceptance sentences in `acceptance/<issue-number>-<slug>.toml`
— `acceptance/94-acceptance-trace-in-ci.toml` is the first one. The file is the
sentence's only versioned home; pull-request prose quotes it and never replaces
it. Every declaration in that directory is read, so a repository in which many
stories declare verifies exactly like one in which a single story does.

An identifier is lowercase words joined by single hyphens and is unique across
every declaration. Unknown keys, a schema version the gate does not read, a
sentence without text, and a repeated identifier are refused rather than ignored.

### What a landing states, and the written exemption

A story declares in the repository, but a repository cannot tell on its own that
a story exists. The landing says so. Every pull request answers the template's
`Literal acceptance sentence(s)` field with either the identifiers in
`acceptance/` this landing proves, or `none` and one written line saying why this
change declares none.

Documentation, cleanup, and pure motion carry no acceptance sentence
legitimately. Writing that down is what makes the absence legitimate: an
exemption stated in one line is a claim a reviewer can weigh, while an absence
nobody wrote down is indistinguishable from a wish quietly dropped. `none`
without a reason is not an exemption, and neither is an empty field.

The gate reads that field from the pull-request body the run's own event
carries, the same way it reads run reports and never workflow text. It consults
no issue tracker and never decides for itself whether a change is a story: the
landing states its position and the gate names a landing that states neither.
Whether a stated exemption is honest stays a review judgment — the same division
of labour the rest of this gate keeps.

A test names the sentence it proves where the test run itself reports it:

- Python: `@pytest.mark.proves("<sentence id>")` on a test function. The marker is
  registered in `pyproject.toml`, so `--strict-markers` refuses a typo, and
  `tests/conftest.py` carries it into the run report pytest writes.
- TypeScript: `proves(<sentence id>)` inside the vitest title, as in
  `it("proves(<sentence id>): shows the failed stream", ...)`. The title is what
  the run prints, so the claim cannot drift out of the reported test the way a
  comment can.

`scripts/check_acceptance.py` runs in the `Acceptance trace` job, after the three
verification jobs, over the run reports they uploaded: the two pytest
`--junitxml` files and the cockpit's vitest JSON. A sentence counts as proven
only where one of those reports carries a test claiming it with the outcome
passed, so a test that was collected without being executed, skipped, filtered
away, or failed proves nothing, and nothing is read out of the workflow's own
text. It fails when a declared sentence has no such proof and when a report
carries a claim no story declares. It refuses outright when the declarations are
empty and when a required report is absent or unreadable, because a report that
cannot be read is a run that cannot be trusted, never a smaller proof surface.

Claims themselves are found by reading every `.py` and `.ts` file in the
repository, bound to no runner and no invocation. A claim in a file nothing
collects is therefore named — it is either a missing test or a line to delete —
rather than staying invisible because the gate only looked where a runner looks.

<!-- acceptance-gate-bound:start -->
```text
proves: every declared sentence was proven by a test that ran and passed here
proves: every claim in this repository names a sentence some story declared
proves: every claim was honoured by a passing test in this pipeline's reports
proves: a proposed landing states its sentences by identifier, or why it has none
does not prove: that a test carries its sentence in meaning - review judges that
does not prove: that a stated exemption is honest - review judges that
does not prove: that a body edited after this run still says what it said - review sees the edit
does not measure: any ratio, case count, or coverage target
```
<!-- acceptance-gate-bound:end -->

Whether the claiming test really carries its sentence stays a review judgment,
the same division of labour the architecture gate keeps: the machine names what
is missing, the reviewer names what is hollow.

### The sentence that was carried open, and how it closed

`acceptance/94-acceptance-trace-in-ci.toml` carried five sentences while a sixth
— *"a story that declares no acceptance sentence is named by verification"* —
stayed named on Issue #94 with nothing proving it, because a gate reading only
`acceptance/` cannot see a story that declared nothing. That sentence is now
declared and proven, and the note recording the debt is gone from the file.

What resolved it was not the metadata the debt assumed it needed. Binding a
landing to a declaration looked as if it required knowing which issue a pull
request closes and whether that issue is a story or cleanup, which is pull-request
metadata and would have made a gate whose whole claim is that it does not guess
begin by guessing. The landing states it instead: the template field above is
answered with identifiers or with a reasoned `none`, and the gate reads that
answer rather than inferring one. Nothing consults the issue tracker.

**DESK, and it outlives the sentence it was written for.** A step that builds the
mechanism of a capability may land while one sentence of that capability is
unproven, provided the sentence is named on the open item and the item does not
close. This is what #94 did, and what the six declared sentences now show it cost:
one further change. As precedent and not as authority: atelier-1's
`work.unproven_acceptance` names only sentences on items that declared any, so
documentation and cleanup are exempt there by carrying none — here they are
exempt by saying so.
