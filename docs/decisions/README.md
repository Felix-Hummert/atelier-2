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
- [ADR 0006: Format V3 is the whole authoring language; capabilities stage execution](0006-node-vocabulary.md) — ACCEPTED, not yet implemented
- [ADR 0007: Named lineages own catalog identity above hash-true revisions](0007-catalog-identity.md) — PROPOSED, not yet implemented
- [ADR 0008: Node budgets separate hard limits from reported thresholds](0008-budget-units.md) — ACCEPTED, not yet implemented
- [ADR 0009: One trust boundary separates the coordinating service from every runner](0009-runner-trust.md) — PROPOSED, not yet implemented
- [ADR 0010: One GitHub adapter observes, publishes and reads back; the core stays platform-blind](0010-github-platform-adapter.md) — PROPOSED, not yet implemented
- [ADR 0011: A project is a store root; the root bounds where a project exists, and destroying it is the only removal](0011-project-isolation.md) — PROPOSED, not yet implemented
- [ADR 0012: Acceptance sentences are declared in the repository and proven by the run reports](0012-acceptance-trace-format.md) — ACCEPTED, implemented

The product stack beyond recorded decisions remains undecided; an ADR is not a
claim that its product slice already exists.
