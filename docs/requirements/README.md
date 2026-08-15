# Human requirements

Audience: humans and agents binding work to an exact statement of human need.

## Requirement documents

A requirement document is to product intent what a decision record is to a
technical choice: numbered, in the repository, changed only through a reviewed
pull request. The discussion happens in issues; this directory holds the
distilled result of that discussion.

The division is exact:

- An **issue** is the live thread. It carries the operator's wish, the
  push-back, the adversarial review, the amendment, and the ruling — in that
  messy order, spread over many comments, with earlier comments left standing
  after later ones replace them.
- A **requirement document** is the settled reading of that thread. It states
  what binds today, in one place, with no superseded sentence left to be
  mistaken for a live one.
- **Neither invents.** A requirement document distils; it never adds intent the
  thread does not carry. Where the thread is undecided, the document says so
  under `Open questions` rather than choosing.

### Naming and header

One file per subject, `NNNN-<subject>.md`, numbered in creation order and never
renumbered. The number is the document's identity; a superseded document keeps
its file and its number.

Every document opens with these fields, in this order:

```text
Status:         DRAFT | AGREED | SUPERSEDED
Owner-Issue:    the one issue that owns this subject
Source-Threads: every issue whose discussion this document distils
Distilled-From: the comment ids read to write this revision
```

`DRAFT` means written but not yet ruled by the operator or the engineering
desk; a builder may read it but may not treat it as binding. `AGREED` means
every rule below carries a ruling in its source thread. `SUPERSEDED` names the
document that replaced it and is otherwise left untouched.

### Body

Four sections, in this order, each of which may be empty but not absent:

- `## Intent` — the sentences that say what the human wants. Short, plain, and
  attributable.
- `## Binding rules` — numbered rules a builder must satisfy. Each rule names
  the comment it distils, so a reader can walk back to the argument. A rule
  that replaced an earlier one says which one, so the thread's dead sentence
  cannot be revived by someone who read only the older comment.
- `## Open questions` — what the thread left undecided, and who owns deciding
  it. An open question is honest debt; a silently resolved one is a fabricated
  requirement.
- `## Acceptance` — the acceptance sentences this requirement expects, and
  which of them a story has already declared. A document names a declared
  sentence by its identifier; where no story has declared one yet, it says so
  instead of inventing an identifier.

### Provenance

The distillation rule is what makes the document checkable: **a requirement
document names the comment ids it distils**, in its `Distilled-From` header and
again at each rule it derives. A reader who doubts a rule can open exactly the
comment it came from. A rule with no comment id behind it is an invention and
is removed, not sourced afterwards.

A literal operator sentence — one the thread marks as binding wording, such as
a canonical acceptance scenario — is quoted verbatim in the language it was
written. The distillation around it is English, like the rest of `docs/`.
Translating a literal sentence is reinterpreting it.

### Precedence

1. [GitHub Issue #1](https://github.com/FlexOr2/atelier-2/issues/1) stays the
   editable authority for the top-level human requirement. Where a requirement
   document and Issue #1 disagree, Issue #1 wins and the document is corrected.
2. A requirement document outranks any derived view of intent, including the
   intent statement in [docs/PRODUCT.md](../PRODUCT.md).
3. A requirement document never outranks a landed decision record. Where it
   asks for something an ADR forbids, that contradiction is an open question,
   not a silent override.

### Index

- [0001: Items are prioritised, get their workflow, and run from a queue](0001-queue-und-autonomie.md)
  — AGREED
- [0002: Access is an invitation, and the installation is the team's workshop](0002-teams-und-zugang.md)
  — AGREED
- [0003: One workshop, three views, one language — the graph](0003-ziel-ui.md)
  — AGREED

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
