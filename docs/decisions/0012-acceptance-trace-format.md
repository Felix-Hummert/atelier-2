# ADR 0012: Acceptance sentences are declared in the repository and proven by the run reports

- Status: ACCEPTED 2026-08-15 — implemented: the store, the claim syntax, and
  the run-report evidence all land with
  [#94](https://github.com/FlexOr2/atelier-2/issues/94); amended 2026-09-02
  (issue [#1022](https://github.com/FlexOr2/atelier-2/issues/1022)) —
  **built with its amendment**: documents the `requirement` key the store
  already carried and adds the `proof` key gating browser-only sentences

## Context

Acceptance sentences lived only in pull-request prose: the template asked for
them, issue bodies carried the wording, and nothing connected a sentence to the
test that proves it. A landing could narrow the wish and no verification would
notice.

`docs/requirements/README.md` reserved this format for its owning story *and* a
decision record. [Issue #94](https://github.com/FlexOr2/atelier-2/issues/94) is
that story; this is that record. It decides three things and deliberately
nothing else: where a sentence is stored, how a test claims it, and what counts
as evidence that the claim was honoured.

## Decision

**The store.** A story declares its sentences in
`acceptance/<issue-number>-<slug>.toml`, schema version 1, each sentence an
identifier and its literal wording. That file is the sentence's only versioned
home; pull-request prose quotes it and never replaces it. Unknown keys, an
unread schema version, a textless sentence, and a repeated identifier are
refused rather than ignored.

**The claim.** A test claims a sentence in the syntax its own runner reports:
`@pytest.mark.proves("<id>")` in Python, `proves(<id>)` inside a Vitest or
Playwright title.
A claim carried only by a comment does not exist, because the run never prints
it.

**The evidence.** What proves a claim was honoured is the pipeline's own run
report for the job that ran the test — the pytest, Vitest, and Playwright reports the
verification jobs emit — and nothing else. The gate derives nothing from
workflow text, and a required report it cannot read is a refusal, never a
smaller proof surface. The passing report entry must name the same source file
and test that made the claim; another test proving the same sentence cannot
honour it. A repeated source-file, test, and sentence identity is ambiguous and
therefore refused instead of letting one report entry stand for both.

**Amendment 2026-09-02 (issue #1022): the sentence table carries two more
keys, and one gates a value.** "An identifier and its literal wording"
undersold what the code and every declaration TOML already carried: two
further optional keys, one already live and undocumented, one new here.

**`requirement`** binds a sentence to one requirement identifier
`docs/requirements/README.md`'s shelf declares. A sentence naming a
requirement no active document declares is refused; a sentence naming none is
listed rather than refused, because almost nothing is filed yet and refusing
that would stop the workshop to do paperwork.

**`proof`** names the one report kind that may honour the sentence, drawn from
the `ProofKind` enum — today only `browser`. A sentence bound to it is
honoured only by a claim the Playwright report shows passing; a claim from any
other required report — Vitest or pytest — is a gate error naming the
sentence, the report, and the claiming test, even standing beside a valid
Playwright claim for the same sentence. A sentence with no `proof` key is
unchanged: any required report's passing claim honours it, exactly as before
this amendment. The key exists because the gate already told Playwright and
Vitest evidence apart to match claim identity (`reported_in`, see "The
evidence" above); this amendment turns that existing distinction into a rule a
sentence can opt into, closing the gap where a test that never renders a
surface could still claim a sentence about one — the phantom verification
named in [#435](https://github.com/FlexOr2/atelier-2/issues/435).

## Consequences

- A claim in a file no runner collects is named red rather than staying
  invisible: the source carries the claim and no report carries its result.
- A browser claim counts only when the Playwright report names the same test in
  the same file as passed; a unit result cannot stand in for it.
- The gate proves linkage and existence. Whether a test carries its sentence in
  meaning stays review judgment — the same bound `docs/requirements/README.md`
  states and the gate prints.
- `docs/requirements/README.md` points at this record for these three decisions
  and does not restate them as a second truth.

## Supersedes

None.
