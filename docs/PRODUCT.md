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

One callable product-core vertical now exists. A caller starts an exact run and
immutable workflow revision, then advances it with one exact prepared effect.
Bootstrap only verifies and returns the run's current state. Advance binds the
request bytes, workflow revision, adapter revision, destination, and external
store identity before enqueueing durable execution. Identical retries return
current durable snapshots; changed identities and a second V1 effect fail
without mutation.

The executor reads the effect back, performs it only after authoritative
absence, and atomically confirms the receipt and run. An unknown outcome becomes
`WAITING_RECONCILIATION` without execution. One accountable operator command may
then confirm a found effect or authorize exactly that request's execution; a
state-version CAS gives competing commands one winner. Receipts preserve exact
request/result bytes and whether confirmation came from adapter readback,
adapter execution, operator observation, or operator-authorized execution. The
first concrete adapter is a persistent loopback SQLite destination stored apart
from Atelier's canonical database. [ADR 0001](decisions/0001-durable-runtime.md)
owns the runtime decision and recovery guarantees.

There is still no cockpit, HTTP surface, configurable workflow graph, provider
or platform integration, deployment code, or general-purpose workflow engine.
The product proves one durable effect vertical; it is not yet remotely usable.
