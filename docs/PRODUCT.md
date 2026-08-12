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

One callable product-core vertical executes an immutable YAML workflow revision
through `Agent → Action → Wait → Subworkflow`. The order of YAML entries is not
execution order; configured edges are. Each confirmed node writes one ordered,
hash-bound event and starts only its configured successor. A submitted Wait
answer resumes the same run, and the terminal event binds the completed history
to one terminal hash. Process restarts resume from durable checkpoints without
duplicating the Action effect.

V1's graph is intentionally narrow: Agent delegates its configured job and exact
output contract through an injected provider-neutral executor and atomically
records a distinct success receipt with its existing event and successor. Action
owns the existing exact effect and reconciliation contract, Wait accepts one exact
integer answer, and the terminal Subworkflow adds two configured integers. The
document is a closed safe-YAML contract; unknown fields, unsafe YAML features,
cycles, unreachable nodes, changed retry identities, and contradictory answers
fail without mutating durable state. [ADR 0002](decisions/0002-exact-yaml-graph.md)
owns that graph contract.

The executor still performs an Action only after authoritative absence. An
unknown outcome becomes `WAITING_RECONCILIATION` with a durable reason; one
accountable command may resolve it, after which initial and reconciled Actions
share the same continuation path. Initial receipt creation commits atomically
with intent confirmation. Reconciliation resolution separately commits its
receipt, intent, command, run, and resolved event. The later `ACTION_COMPLETED`
transition is another crash-safe transaction.
[ADR 0001](decisions/0001-durable-runtime.md) owns the runtime and recovery
boundary.

An HTTP API now projects that durable state under `/atelier/api/v1`. It can
publish and inspect immutable workflow revisions; start, list, and inspect
runs; answer a waiting node; submit an accountable reconciliation; and follow
the closed durable event history as a resumable server-sent event stream. Its
public references are transport identifiers, not new domain identities, and
retries report whether a command was newly accepted or already existed without
duplicating its durable write or wake-up. [ADR 0003](decisions/0003-http-api.md)
owns the API and resume contract.

There is still no cockpit, provider or platform integration, authentication
boundary, or deployment code. The graph and API are a proven durable vertical,
not yet a general-purpose workflow engine or a deployed remote product.
