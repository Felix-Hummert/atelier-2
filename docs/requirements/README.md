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

`AGREED` means the operator approved **this exact document**, and `Approved-By`
names the comment in which that happened. It is not a status the engineering
desk can grant: a reviewed pull request judges whether a distillation is
faithful, and faithfulness is not approval. A document whose `Approved-By` is
`none` is never `AGREED`.

`SUPERSEDED` names the document that replaced it and is otherwise left
untouched.

### Body

Four sections, in this order, each of which may be empty but not absent:

- `## Intent` — the sentences that say what the human wants. Short, plain, and
  attributed.
- `## Rules` — numbered rules a builder is expected to satisfy. Every rule
  carries its **authority grade** and its source:
  - **`OPERATOR`** — the thread records this sentence in the operator's voice: a
    quoted wish, or a decision the thread marks as the operator's own. This is
    the only grade that carries human intent.
  - **`DESK`** — an engineering reading: a sharpening, a consequence of a
    decision record, an observation of the current code, a machine-review
    finding. It records what the desk currently believes is right; it binds
    nothing until an operator rules it. A `DESK` rule that narrows or replaces
    an `OPERATOR` sentence says so at the rule, because that is a proposal to
    the operator and not a settled correction.

  A rule that replaced an earlier one names the one it replaced, so that a dead
  sentence cannot be revived by someone who read only the older comment.
- `## Open questions` — what the thread left undecided, and who owns deciding
  it. An open question is honest debt; a silently resolved one is a fabricated
  requirement.
- `## Acceptance` — the acceptance sentences this requirement expects, and which
  of them a story has already declared. A document names a declared sentence by
  its identifier; where no story has declared one yet, it says so instead of
  inventing an identifier.

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
  `#82 body @ fe6fd31f`. The digest is reproduced with

  ```console
  $ gh api repos/FlexOr2/atelier-2/issues/82 --jq '.body' | sha256sum
  ```

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

Each document carries its own status; this index deliberately does not repeat
it, because a second copy of a status is the next thing to go stale.

## The editable human requirement

The only editable HumanRequirement authority is
[GitHub Issue #1](https://github.com/FlexOr2/atelier-2/issues/1). Change the human
requirement there; do not copy or independently reinterpret its content here.

This directory will own immutable published requirement revisions and their
provenance once that capability exists. A future trace will bind each literal
acceptance sentence to its immutable requirement revision and its proof without
becoming another requirement source. The publication format and trace format
remain undecided until their owning story and decision record define them.

No published requirement revision or requirement trace exists in this
foundation.
