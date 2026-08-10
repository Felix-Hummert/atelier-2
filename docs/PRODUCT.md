# Product intent

Audience: humans and agents deciding what Atelier 2 should become.

This file is the sole owner of product intent, scope, and implementation status.
Update it when those facts change; requirements, technical decisions, and
implementation evidence belong to their own owners.

Atelier 2 is intended to become a lean, independent agentic orchestrator. An
operator will describe work as versioned state machines assembled from
configurable nodes, then start, observe, steer, approve, cancel, and resume runs
through a responsive cockpit.

The intended product will:

- keep projects isolated while one operator manages several of them;
- bind each run to immutable workflow, requirement, and context revisions so a
  restart can continue from confirmed checkpoints without silently changing its
  instructions;
- expose each node's provider, model, capabilities, tools, skills, permissions,
  budget, inputs, outputs, retry behavior, cancellation, and transition;
- give Claude and Codex the same product and capability contract through thin
  provider boundaries;
- leave issues, pull requests, checks, reviews, merges, and history with the
  external development platform, while the core owns only its product concepts;
  and
- show which source, context, workflow, proof, landed object, and deployment a
  visible result represents instead of inventing a second truth.

V1 is intended for one operator, on infrastructure they control, across multiple
isolated projects. GitHub is the first product path behind a replaceable platform
boundary; native CI, review, and squash merges remain authoritative there. A
future platform adapter may support GitLab without changing the core contract.

V1 does not include multi-user SaaS, a public shell, a home-grown replacement
for Git, pull requests, or CI, a package manager, or speculative knowledge-graph,
vector-store, security, or extension architecture.

## Current state

One callable product-core slice now exists. A caller can supply an exact,
nonempty run identifier and immutable workflow-revision bytes; Atelier hashes
and stores those bytes, atomically creates the revision-bound run and enqueues
its DBOS workflow in the same canonical SQLite transaction, and a matching
executor can durably advance that run once from `STARTED` to `COMPLETED` after a
restart. Identical starts return the current run without enqueueing again, while
conflicting run identity or durable revision bytes fail without mutation.

This is only the H1 durable-start boundary accepted by
[ADR 0001](decisions/0001-durable-runtime.md). There is no cockpit, HTTP surface,
effect or reconciliation path, configurable workflow graph, provider or
platform integration, deployment code, or general-purpose workflow engine.
Those product behaviors remain intent rather than implemented claims.
