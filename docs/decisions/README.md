# Technical decisions

Audience: humans and agents who need the durable reason for a technical choice.

This directory owns architecture decision records. A record must name its
context, decision, consequences, and any record it supersedes; other documents
may point to that record but must not restate it as a separate truth.

A record's number is the first not already taken in this directory or claimed by
an open pull request, where a pull request claims a number by the record's file
path in its diff — the landed directory alone cannot show a reservation. A
number is never reused and never renumbered.

- [ADR 0001: DBOS owns durable execution behind an Atelier adapter](0001-durable-runtime.md)
- [ADR 0002: Exact safe-YAML revisions own V1 graph execution](0002-exact-yaml-graph.md)
- [ADR 0003: The HTTP API projects durable workflow truth](0003-http-api.md)
- [ADR 0004: The local cockpit is a projection and control adapter](0004-local-cockpit.md)
- [ADR 0005: CI enforces package boundaries](0005-enforced-package-boundaries.md)
- [ADR 0006: Format V3 is the whole authoring language; capabilities stage execution](0006-node-vocabulary.md)
- [ADR 0007: Named lineages own catalog identity above hash-true revisions](0007-catalog-identity.md)
- [ADR 0008: Node budgets separate hard limits from reported thresholds](0008-budget-units.md)
- [ADR 0009: One trust boundary separates the coordinating service from every worker](0009-runner-trust.md)
- [ADR 0010: One GitHub adapter observes, publishes and reads back; the core stays platform-blind](0010-github-platform-adapter.md)
- [ADR 0011: A project is a store root; the root bounds where a project exists, and destroying it is the only removal](0011-project-isolation.md)
- [ADR 0012: Acceptance sentences are declared in the repository and proven by the run reports](0012-acceptance-trace-format.md)
- [ADR 0013: A bounded `iterate` block repeats a subworkflow until a receipt says green](0013-bounded-iteration.md)
- [ADR 0014: A declared loop repeats a stretch of one graph, and the round is the fourth dimension of a node execution identity](0014-in-graph-rounds.md)
- [ADR 0015: A declared verdict steers the loop's back edge, under a contract this product owns](0015-verdict-steered-continuation.md)
- [ADR 0016: The queue projection owns one item's derived identity and its CAS-guarded admission](0016-queue-projection-identity.md)
- [ADR 0017: An installation-owned Account holds every credential; delegated grants and stored keys are peer auth modes, and the app holds only references](0017-account-credential-model.md)
- [ADR 0018: An imported plugin stays provider-bound and is passed through whole; neutrality lives in the role and its casting](0018-plugin-intake-and-neutral-roles.md)
- [ADR 0019: The workshop is four rooms built from four blocks under one rule against restating; Mockup v8 is the gestalt owner](0019-workshop-target-picture.md)

## How to read a record

A record is a protocol, not a running contract: its text says what was decided
on the date it names, and it is read that way.

What holds today has its own owner, and precedence is decided **per question,
not globally**: [docs/PRODUCT.md](../PRODUCT.md) owns what exists,
`docs/requirements` owns intended behaviour, and `docs/product/` owns landed
detail. On those questions they are right. The record keeps the *why* — the
technical choice it made and the reasoning behind it — until an amendment
changes it.

A decision is therefore never rewritten **silently**. A later ruling that
changes what a record decided lands as a dated amendment, naming its
authority, placed **beside the passage it changes** — at the end of the
record only when the amendment changes the record as a whole — and the
record's `Status`/`Date` header is corrected to point a reader at it (ADR
0011, 0013, 0018 and 0019 already amend this way). AGENTS.md's rule to delete
documentation that no longer matches the code does not reach a record
corrected this way: an amended, dated fact is not stale, it is current, and
staying byte-identical outside its amendment is how a reader trusts every
other date in it.

Where a record only restates a rule another document owns, a link to that
document replaces the restatement (AGENTS.md: "Do not duplicate guidance.
Update the owner."). Removing a restatement is not a technical decision, so it
takes no amendment — only the record's own decisions do.

Each record carries its own status, and this index deliberately does not repeat
it, because a second copy of a status is the next thing to go stale. How much of
a record has been built is implementation status and belongs to
[docs/PRODUCT.md](../PRODUCT.md).

The product stack beyond recorded decisions remains undecided; an ADR is not a
claim that its product slice already exists.
