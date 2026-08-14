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

Workflow format V2 adds provider-neutral Agent roles. Before a run starts, every
role is resolved to one immutable, secret-free agent-configuration revision and
authentication-profile revision; the complete matrix is frozen into that run.
Before invoking the exact configured provider/executor, the runtime persists one
ordinal-1 attempt and binds an in-memory invocation to a separately supervised
process generation. Only the live caller whose compare-and-set reaches
`LAUNCH_ARMED` may authorize that generation to launch; the exact cgroup is the
restart-visible cleanup witness, while PIDs, commands, environment, and provider
handles are never durable state. The API can durably request cancellation of the
current exact attempt before signalling it. Its workflow sends `TERM`, waits one
finite grace, escalates to `KILL`, reaps the process, and records the cleanup
disposition. Recovery continues that cleanup from the cgroup witness without
replaying the invocation. One explicit replacement creates a distinct ordinal-2
attempt and workflow only after cleanup; ordinal 3 and automatic provider retry
do not exist. A known reaped unsuccessful child becomes `FAILED`; a success
records the non-secret operational identity and arbitrary output bytes in one
atomic attempt/receipt/event/run transition.

The first real provider now sits behind that durable contract. When the operator
declares a Claude executable, an agent workspace, and a credential directory, the
host composes one Claude subscription executor. It runs the bound model headless
through the CLI's print-JSON envelope, hands the node's job to the process over
standard input rather than its command line, and grants the launched process only
the declared `CLAUDE_CONFIG_DIR` credential boundary and the serving host's
executable search path; nothing else of the server's environment is inherited. A
configuration binding a non-subscription profile to this executor is refused
before any process is prepared. An unsuccessful exit, an unreadable envelope, an
envelope declaring a provider error, a raw frame past the provider-frame bound,
and an answer larger than the durable output bound all fail the attempt instead
of recording invented output. Undeclared, the host composes no V2 provider
factory and behaves exactly as before. Codex remains absent, and the isolated
read, edit, and test tools an agent needs to change its own workspace are not
part of this contract.

That provider frame has its own bound, distinct from the durable output bound,
because the durable answer travels inside a JSON envelope: it is sized so no
answer the durable contract accepts can be refused as a frame, and anything
larger fails the attempt.

The call itself is deliberately the barest one its authentication allows: no
tools, no hooks, no MCP servers, no plugins or skills, no project configuration
discovery, no persisted session, no prompt history, no retries, and a bounded
turn count, so the credentials it is handed answer text and do nothing else.
Every one of those switches was measured against the Claude release this
executor names, so the deployment reads the declared executable's version before
it composes anything and refuses one older than that release rather than trusting
an unmeasured CLI to mean the same thing. That is containment, not isolation —
the process still runs as the serving user — so this executor may only be
declared for a loopback bind. Serving it on a reachable address is refused at
startup, because starting a billed provider is unauthenticated on this API.

What an executor can honestly do is a declaration, not a hope. Each executor
declares its capabilities, an agent configuration carries the capability its node
demands, and starting a run compares the two: a node demanding a capability its
bound executor does not declare is refused while the run is being started, before
any attempt, watchdog or billed process exists. Headless is every provider's
duty; anything above it has to be asked for by name.

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
publish secret-free auth-profile and agent-configuration revisions; publish and
inspect immutable workflow revisions; start, list, and inspect V1 or V2 runs;
answer a waiting node; cancel the current V2 Agent attempt with an optional
single replacement; submit an accountable reconciliation; and follow the
closed durable event history as a resumable server-sent event stream. Existing
V1 JSON and OpenAPI component bytes remain frozen while exact V2 unions expose
the run's safe binding matrix and byte-safe Agent output. Public references are
transport identifiers, not new domain identities, and retries report whether a
command was newly accepted or already existed without duplicating its durable
write or wake-up. [ADR 0003](decisions/0003-http-api.md) owns the API and resume
contract.

A narrow local cockpit can list runs, publish and start a workflow from `/new`,
and project one durable run's bound revision, state, nodes, and resumable event
history. It can answer the exact integer requested by a Wait node and resolve an
unknown Action outcome as either an exact found effect or an accountable,
confirmed absence. Its session-scoped mutation journal preserves exact retry
bytes without becoming a second durable truth. [ADR 0004](decisions/0004-local-cockpit.md)
owns this browser boundary. The cockpit still provides no provider or platform
integration, authentication boundary, public deployment, or general-purpose
workflow editing. The graph, API, and local cockpit are a proven durable
vertical, not yet a general-purpose workflow engine or a deployed remote
product.
