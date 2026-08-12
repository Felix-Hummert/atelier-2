# Technical decisions

Audience: humans and agents who need the durable reason for a technical choice.

This directory owns architecture decision records. A record must name its
context, decision, consequences, and any record it supersedes; other documents
may point to that record but must not restate it as a separate truth.

- [ADR 0001: DBOS owns durable execution behind an Atelier adapter](0001-durable-runtime.md)
- [ADR 0002: Exact safe-YAML revisions own V1 graph execution](0002-exact-yaml-graph.md)
- [ADR 0003: The HTTP API projects durable workflow truth](0003-http-api.md)
- [ADR 0004: The local cockpit is a projection and control adapter](0004-local-cockpit.md)

The product stack beyond recorded decisions remains undecided; an ADR is not a
claim that its product slice already exists.
