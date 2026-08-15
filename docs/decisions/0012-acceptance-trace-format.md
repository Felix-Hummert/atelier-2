# ADR 0012: Acceptance sentences are declared in the repository and proven by the run reports

- Status: ACCEPTED 2026-08-15 — the store and the claim syntax are implemented;
  the report evidence lands with the remaining heads of
  [#94](https://github.com/FlexOr2/atelier-2/issues/94)

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
`@pytest.mark.proves("<id>")` in Python, `proves(<id>)` inside the vitest title.
A claim carried only by a comment does not exist, because the run never prints
it.

**The evidence.** What proves a claim was honoured is the pipeline's own run
report for the job that ran the test — the pytest and vitest reports the
verification jobs emit — and nothing else. The gate derives nothing from
workflow text, and a required report it cannot read is a refusal, never a
smaller proof surface.

## Consequences

- A claim in a file no runner collects is named red rather than staying
  invisible: the source carries the claim and no report carries its result.
- A sentence whose only honest proof is an end-to-end flow cannot be claimed
  until that runner's report joins the required set.
- The gate proves linkage and existence. Whether a test carries its sentence in
  meaning stays review judgment — the same bound `docs/requirements/README.md`
  states and the gate prints.
- `docs/requirements/README.md` points at this record for these three decisions
  and does not restate them as a second truth.

## Supersedes

None.
