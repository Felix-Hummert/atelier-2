# Requirements

Audience: operator deciding intent, and engineer or agent binding approved work.
This index owns the rules; [`revisions.toml`](revisions.toml) owns active byte
and source-watermark bindings.

Trace is one-way: `Vision → Requirement → Acceptance → executed test`.

Each edge is written once; generated views never become a second owner.

## Information owners

- [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1) owns the editable
  top-level human requirement. Other issues hold subject decisions and change
  candidates.
- Only an approval-backed active registry tip is approved normative *what*.
  Frozen legacy documents are transitional input, never approved truth.
- A journey is a non-binding scenario and names the requirement IDs it
  illustrates.
- [ADR 0012](../decisions/0012-acceptance-trace-format.md) owns the acceptance
  format. `acceptance/*.toml` owns each done sentence and Requirement edge.
- A test claims an acceptance identifier; the run report proves whether that
  test actually ran and passed.
- ADRs own technical decisions. Types, OpenAPI, and schemas own exact technical
  contracts. [PRODUCT](../PRODUCT.md) is only a derived implementation view.

Discussion, drafts, open questions, and `DECISION_REQUIRED` stay on the owning
issue. A pull request may carry a frozen candidate; it cannot approve intent.

## Strict document contract

A migrated numbered document has exactly this shape:

```markdown
# <title>

## Intent

<nonempty, short intent>

## Rules

### REQ-<AREA>-<nn>: <one nonempty, atomic, testable, solution-independent sentence>
Quelle: OPERATOR|DESK — <source pointer>

## Non-goals

<optional, but nonempty when present>
```

Rule identifiers are unique and never reused. A semantic change gets a new
identifier. `OPERATOR` means the source is the operator's own exact sentence;
every interpretation or engineering consequence is `DESK`. Review verifies the
citation's meaning. The gate verifies its shape and source presence.

Status, open questions, proof lists, journey lists, implementation reports, run
dossiers, history, and technical design are not strict requirement fields.

## Approval and revision lifecycle

1. Draft and resolve the candidate on its issue and pull request.
2. Freeze the exact UTF-8 Markdown bytes and compute their SHA-256 digest.
3. The operator approves only this exact one-line comment, with no added newline:
   `APPROVE REQUIREMENT REVISION NNNN sha256:<content-digest>`.
4. Record the document, path, content digest, approval comment ID, digest of the
   exact approval-comment bytes, and predecessor in `revisions.toml`. The
   approved Markdown bytes do not change in that step.
5. CI requires one regular numbered file, a complete unbranched predecessor
   line with one tip, and bytes matching that tip. `GENESIS` is first.

Changing approved bytes always creates a successor. Supersession or retirement
is added only when a real revision needs it; no unused registry field is
reserved in advance.

## Source freshness bindings

`revisions.toml` binds an approved requirement revision's exact digest to one
source thread and its last observed object. Bindings are append-only: a changed
watermark belongs to a new requirement revision, while the prior binding stays
field-identical. The documentation-order gate verifies those bindings without
fetching a source or judging what it means; the pure freshness reader consumes
an in-memory source snapshot. It never parses `Distilled-From` prose.

The `[[legacy]]` entries are temporary metadata for pre-lifecycle
documents. `revisions.toml` alone owns current shelf metadata. Against an exact
VCS base, CI admits no new legacy, keeps every prior registry field identical,
and allows only in-place approval migration or a valid successor. Thus bytes
and a matching digest cannot be re-pinned together. The legacy list only shrinks.

## Trace contract

The requirement owns only its rule ID and provenance. An acceptance sentence
owns the Acceptance→Requirement edge. A journey owns the Journey→Requirement
edge. A test owns the Test→Acceptance claim. Requirements contain no manual
backward lists; coverage and reverse lookup are derived from those owners.

Both repository gates use [`scripts/requirement_contract.py`](../../scripts/requirement_contract.py)
for registry and Markdown parsing. Neither gate calls GitHub.

<!-- documentation-order-gate-bound:start -->
```text
proves: every numbered requirement is a regular in-shelf file whose exact bytes match its sole active tip or frozen legacy pin
proves: with an exact VCS base, legacy pins cannot grow or change, and may only migrate in place to approval-backed history
proves: with an exact VCS base, every existing revision remains field-identical and history grows only by a valid successor
proves: every strict requirement has only title, nonempty Intent, nonempty unique sourced rule sentences, and optional nonempty Non-goals
proves: every approval-backed revision line is predecessor-complete, unbranched, and has one tip on one numbered path
proves: every source binding names one exact approval-backed requirement revision, and with an exact VCS base prior bindings stay field-identical
does not prove: that a cited source or approval comment exists or says what the registry claims - review judges that
does not fetch: GitHub or another live authority
does not judge: source meaning or freshness
does not make: a frozen legacy document an approved revision
```
<!-- documentation-order-gate-bound:end -->

The acceptance gate additionally owns only the executable trace below.

<!-- acceptance-gate-bound:start -->
```text
proves: every declared sentence was proven by a test that ran and passed here
proves: every claim in this repository names a sentence some story declared
proves: every claim was honoured by a passing test in this pipeline's reports
proves: a proposed landing states its sentences by identifier, or why it has none
proves: a sentence that names a requirement names one a document declares
proves: a sentence bound to proof = 'browser' was honoured only by the Playwright report
does not prove: that a test carries its sentence in meaning - review judges that
does not prove: that a stated exemption is honest - review judges that
does not prove: that a bound sentence serves its requirement - review judges that
does not measure: how much of the shelf is bound - it counts and says so
does not prove: that a body edited after this run still says what it said - review sees the edit
does not measure: any ratio, case count, or coverage target
does not duplicate: the requirement grammar or revision checks owned by scripts/requirement_contract.py
```
<!-- acceptance-gate-bound:end -->
