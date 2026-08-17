# Product intent

Audience: humans and agents deciding what Atelier 2 should become.

This file owns implementation status: what Atelier 2 currently is, proven by
what has landed. It no longer owns product intent. Intent is stated by
[GitHub Issue #1](https://github.com/FlexOr2/atelier-2/issues/1) and by the
issue threads that the numbered documents indexed by
[docs/requirements/README.md](requirements/README.md) read; the intent section
below is a derived view of those sources, corrected by rewriting it against them
and never by editing it into a second, independent truth. It is hand-maintained
and nothing checks it, so where it and a source disagree the source is right and
this view is simply stale. Technical decisions and implementation evidence
belong to their own owners.

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
Each configuration also binds a typed requested execution capability. Migrated
configuration revisions retain their original V1 hash and mean `headless`; new
API publications use the capability-aware V2 hash format and name the capability
they request, `headless` or `interactive`. A caller that omits it publishes
`headless`, byte-identically to a publication made before the field could be
sent. Publication binds an executor key, not a capability, so a configuration
may request more than today's executors serve. An executor registry must attest
`headless`, and a run requesting a capability absent from its exact
provider/executor entry is refused before any provider process starts. A
nonterminal run is refused on restart before its factory opens when that
attestation has disappeared.
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
before any process is prepared, and V1 admits only a personal `max` or `pro`
credential, refusing a Team, Enterprise, absent or unreadable one, because an
account outside that set can be handed administrator-managed settings when the
CLI starts. It attests exactly one execution capability,
`headless`, because a print call is the only shape it can serve, so a node
demanding an interactive one is refused before that run exists at all. An
unsuccessful exit, an unreadable envelope, an envelope declaring a provider
error, and an answer larger than the durable output bound all fail the attempt
instead of recording invented output. Undeclared, the host composes no V2
provider factory and behaves exactly as before. When the
operator also declares a Grok executable, workspace, and credential directory,
the host composes one Grok subscription executor beside Claude. It runs the
bound model headless through `grok -p --output-format json`, hands the job
through `--prompt-file` rather than the argument vector, and grants the child
only the serving host's search path plus one disposable invocation-private
`HOME`/`GROK_HOME`. That home receives a private copy of the source
`auth.json`; provider sessions and responses stay there and the entire home is
removed after success, known failure, cancellation, retry refusal, or process
error without touching another invocation. Before launch, `grok inspect`
must report that exact home/configuration as its only configuration source,
all external-compatibility imports disabled, and no ambient trust surface.
The isolated read, edit, and test tools an agent needs to change its own
workspace are not part of this contract.

Codex sits behind the same boundary, declared the same way and composed
alongside Claude rather than instead of it. Its CLI has no prompt-file flag, so
the node's job travels on standard input and never on the command line, and its
durable answer is read from the last-message file the CLI writes rather than
from an event stream this contract has not measured — an answer that never
arrives is a failed attempt, not an invented one. What differs is that a Codex
binding also carries the sandbox policy its attempts run under. That policy is
part of the prepared command, and the deployment proves at startup that the
CLI's own sandbox can actually start on this host, because a sandbox that
cannot start contains nothing and discovering that at the first run costs a
run. The same startup proves the composed profile resolves the credential
directory it was given, loads no user configuration, and configures no MCP
server, so the operator's own Codex trust — including the per-project trust its
configuration records — is never inherited by a served agent.

The raw frame a provider writes has its own bound, distinct from the durable
output bound, because the durable answer travels inside a JSON envelope. The
executor declares that bound on the invocation it prepares, since the frame is a
property of its own wire format, and supervision holds the process to exactly
that declaration: a frame past it is terminated and refused before the executor
ever decodes it, so no answer is recorded. It is sized so no answer the durable
contract accepts can be refused as a frame.

The call itself is deliberately the barest one its authentication allows: no
tools, no hooks, no MCP servers, no plugins or skills, no project configuration
discovery, no session or prompt history surviving the invocation, no retries,
and a bounded turn count, so the credentials it is handed answer text and do
nothing else.
It also asks the CLI to strip provider credentials from every subprocess
environment, as defence in depth against a child this invocation does not
expect to have; that hardening needs bubblewrap on the search path the launched
process receives, so a deployment without it is refused at startup rather than
at the first run.

Every one of those switches was measured against one exact Claude release, so
the deployment reads the declared executable's version before it composes
anything and admits only a release that conformance run covers — not merely a
new enough one, because a later CLI can change a control the containment
depends on, and admitting it costs a rerun of that one-call matrix and a
decision about this executor's operational identity. The same startup attests
that this host carries no administrator-managed policy: policy is outside every
switch above by design and can still start a child beside the credential
directory, so a host carrying one is refused rather than served. Reading the
declared executable's version is itself the first execution of an external
binary, so it is run with no environment at all, in an empty private directory,
under a byte bound on both streams and a deadline that kills the whole probe
session. That is containment, not isolation — the process still runs as the
serving user — so this executor may only be declared for a loopback bind.
Serving it on a reachable address is refused at startup, because starting a
billed provider is unauthenticated on this API. OS-enforced isolation remains
the planned stronger boundary; until it lands, these refusals are what keep the
tool-free premise true.

Workflow format V3 is authored truth, not executable truth. The parser accepts a
format-3 document into its own closed model — the five node kinds with the field
matrix each requires, refuses, or accepts, `depends_on` as the only control edge,
the join rule in its three arities, the input sources a node may read — another
node's output, that node's terminal receipt, a context entry, and the order the
graph itself was started with — the two context-edge kinds, and graph-level inputs
and outputs — and refuses every forbidden form naming the node and the field it
concerns, including each retired V1 or V2 key with its replacement. Unsafe YAML is
refused by name too, before any vocabulary is read: an
anchor, an alias, an explicit tag, a merge key, a duplicate key, a second document,
a document that is not UTF-8 without a byte order mark, and one nested past the
bound that keeps the refusal a refusal instead of an exhausted stack.

Every reference behind that surface now binds. A subworkflow node's declared inputs
and outputs match the graph boundary of the published child revision it names one to
one, by name and schema revision, read against that child's real content, and a chain
nesting deeper than the depth its caller attests is refused before that depth resolves
or reads anything. The order a graph declares is readable by name: a node of any kind
binds it as an input, and a parent's own order reaches a child through that boundary
under the schema revision both levels agreed on, a differing one refused by name.
That boundary is checked from both sides — a node reading an order its graph never
declared, and a declared order no node reads, are each refused naming what they
concern. An input whose source proves no schema revision — a terminal receipt, a
context entry, an authored value — still cannot bind a typed graph input, and
recursion is impossible rather than checked: no revision can carry its own hash.
Every other versioned reference — schema, deterministic and adapter
operation, context source, read operation, profile, skill, tool, and the policy,
budget, retry and cancellation policies — resolves against the registry of the kind
its authored position puts it in, by the exact revision hash it pins, and so does
every reference of every child the document reuses. A reference whose revision is no
pinned hash, that no publication of that kind carries, or that a registry answers
with a revision of another kind or another hash is refused naming the node, the
field, the declared entry, the chain it was reached through, and the reference
itself. A `schema` reference proves more than its hash: the revision it pins must be
a schema, under one closed profile of JSON Schema Draft 2020-12 whose bounds keep a
published schema cheap to read — bounded bytes, container depth and value count,
UTF-8 without a byte order mark, no duplicate keys and no non-canonical numbers,
`$id`, `$anchor`, `$dynamicAnchor` and `$dynamicRef` refused, every `$ref` local and
resolvable, `$schema` absent or exactly Draft 2020-12, and `format` left the draft's
annotation instead of an assertion. Retrieval is off by construction rather than by
trust: evaluation runs against a registry whose only retrieval path raises. The
profile checks a reference's target and not only its form: a local `$ref` naming an
anchor or a pointer the document does not carry is refused where the bytes are read,
over the whole document rather than only where an evaluator would trip. A reference
cycle no instance can break is refused too — the rule is whether the cycle passes
through an applicator that descends into the instance, so `{"$ref": "#"}` alone is
refused while a tree whose child is `{"$ref": "#"}` under `properties` stays legal,
because that recursion ends on any finite instance. So nothing this profile accepts
can fail at first evaluation for want of a target, which would be an outage rather
than a refusal. What still waits for the owner that first evaluates an instance
against a schema (#57) is that evaluation itself: whether an agent's output
satisfies the schema its node declared. Bytes that fall outside the profile are
refused by name, so the whole snapshot fails rather than binding a type nobody can
read, and the preview says so instead of drawing it.
A subworkflow's own `workflow` reference is one of them and resolves through the
binder that already read that child, so one question keeps one answer. What resolves
is frozen into one run-configuration revision — the role matrix by its existing
binding identity and every resolved reference, the child revisions among them —
hash-framed as one immutable snapshot whose identity does not depend on the order it
was assembled in. From those parts one composed preview is derived, so what a
revision will do is readable before anything of it runs: every node under the kind
the document wrote it in, an agent node with the role, provider, model, configuration
revision and mode it is bound to, the dependency edges and the join a scheduler
really applies, the capabilities each node demands including the grants its skills
carry in transitively, the published revision every reference lands in or the named
reason it lands nowhere — a withdrawn skill among them, named rather than ending the
drawing — every published skill whose contents nobody read, whose carried grants the
preview says are unknown instead of answering that it carries none, every order a
graph declares with its name and schema revision at the graph that declared it, the
child every subworkflow node binds, by the reference it authored and the exact
revision that resolved to, with that child's own preview under it, and the
executability verdict with the capability each still-waiting node needs — marked
proposed or bound, so an
author's intent is never read as a binding. That preview is a derivation and nothing
else: no route, no rendering and no stored shape carries it. Behind that, nothing:
the registries are ports a caller supplies. A durable catalog adapter now
publishes exact revision bytes, founds a named lineage through a typed writer
that derives the lineage id, and resolves an admitted name or lineage id to
those bytes; run-configuration binding is still lineage-free and a reference's
`ref` is carried into that snapshot without calling `resolve_reference`. A
64-hex query is a lineage id; anything else is a display name. A retired
lineage is refused by id or any alias. There is no capability attestation, and
no runtime executes a child.

A valid V3 document is publishable long before it is executable: it
becomes an immutable revision under the same exact-bytes hash identity as V1 and V2,
and the revision projection names its format and marks it unexecutable, while an
invalid one is refused at publication carrying that named node and field. Starting a
run on such a revision is refused naming its format and writes no run. V1 and V2
documents keep their exact meaning under their own models, and their wire bytes are
unchanged.
[ADR 0006](decisions/0006-node-vocabulary.md) owns this vocabulary and the staging
rule behind it.

An agent is authored as one markdown file. Its frontmatter is a closed set of
`name`, `description`, an optional `model`, and an optional `tools` declaration;
the body is the system prompt, kept byte-exact. An absent `tools` field leaves
the agent able to use every tool its executor offers, because a restriction is
only ever explicit, and a present one declares exactly that closed set. Every
other key, missing required field, unreadable value, duplicated key or tool, and
empty prompt is refused by its own name. A definition renders back to a canonical
document that reads as the same definition — the declared tool set always as a
sequence, so a tool name that itself spells the comma an author may separate by
survives the round trip — and it publishes deterministically into an existing
agent-configuration revision, with the deployment rather than the file owning the
authentication profile, the executor, and the model an unspelled one falls back
to. This is the authoring format alone: nothing enforces a tool declaration yet,
no serving surface publishes a definition yet, and today's configuration revision
carries no field for a name, description, tool declaration, or system prompt, so
the published revision alone cannot reconstruct the definition it came from.
Where an authored definition durably lives is decided by the accepted but
unimplemented catalog-identity record; until it is implemented, the round trip
holds over the definition's own canonical bytes and not over the catalog.

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
closed durable event history as a resumable server-sent event stream. A served V2
run also names the state of every node of the revision it is bound to, so a reader
is told where each node stands instead of computing it: one pure function in the
core derives that rail from the run snapshot, that revision, and the events since,
with the snapshot authoritative only until an event overtakes it, and success
carries exactly one name on the wire. Existing
V1 JSON and OpenAPI component bytes remain frozen while exact V2 unions expose
the run's safe binding matrix and byte-safe Agent output. Public references are
transport identifiers, not new domain identities, and retries report whether a
command was newly accepted or already existed without duplicating its durable
write or wake-up. [ADR 0003](decisions/0003-http-api.md) owns the API and resume
contract.

A narrow local cockpit can list runs, publish and start a workflow from `/new`,
and project one durable run's bound revision, state, nodes, and resumable event
history. It opens in the Studio rather than in that list: one screen across the
whole workshop naming every run that waits for a human — the durable states
`WAITING_INPUT` and `WAITING_RECONCILIATION` — beside the one project of this
installation, where an area with nothing in it names the next action that is
possible today and the door to a conductor nobody has built yet says exactly
that. It can answer the exact integer requested by a Wait node and resolve an
unknown Action outcome as either an exact found effect or an accountable,
confirmed absence. For a V2 run it renders the node states the API names rather
than deriving them — the V2 event stream carries the rail with every event, so
nothing V2 is derived in the browser; the one named exception is the V1 half,
whose run resource is byte-frozen and which dies with the V3 cutover — and the
only state rule left in the browser is a client-owned interaction overlay that
lifts a node needing the operator while his form is open and stills it by that
open form alone. Its session-scoped mutation journal preserves exact retry
bytes without becoming a second durable truth. [ADR 0004](decisions/0004-local-cockpit.md)
owns this browser boundary. The cockpit still provides no provider or platform
integration, authentication boundary, public deployment, or general-purpose
workflow editing. The graph, API, and local cockpit are a proven durable
vertical, not yet a general-purpose workflow engine or a deployed remote
product.

That API now has a command-line client of its own, so starting real work costs
one command instead of four ceremonies. `atelier2 run` publishes one workflow
document and one agent file per bound role, starts the run they describe,
follows its event history to the end, and writes the agent output that run
produced to standard output, with the run, its revision, its terminal hash and
one hash per output on standard error. Every publication is idempotent and the
run identity is derived from the published hashes unless the operator names one,
so the same command run twice reports the first run instead of paying for a
second. The client owns nothing: it holds no durable state, adds no route, and
hands the service's typed problems on unchanged, whether the service refused an
answer or ended the event stream with its own failure frame. A run that stops on
a decision the command cannot make — a waiting node, an unknown effect outcome, a
failed agent attempt — ends it by name with a nonzero exit code instead of
waiting. Exit 0 says the command read that run's history as far as the run's own
latest event, so a history that broke off is refused by name and a truncated or
empty output is never dressed as a receipt. The
job still travels inside the published document, so one distinct input burns one
revision, and neither a run-level input, a workflow name, nor an output contract
that could decide an exit code exists yet.

The canonical store is schema V13. A fresh store is created as exact V13 and
carries published revisions of the closed kind set, lineage membership bound
to those revisions, append-only alias and retirement histories, format-3
runs, immutable node artifact bytes, node receipts, their ordered output and
access bindings, and the immutable context-package manifests those receipts
name. A typed in-process writer can commit that exact
supervised V3 start as one atomic set. The catalog adapter founds a lineage
and admits members through a typed writer that derives `CatalogLineageId`
from kind and founding hash and refuses a mismatched id before mutation. An
admitted name or lineage id resolves to the exact published bytes; a missing
founding, unpublished member, wrong kind, or retired lineage is refused by
name. Measurements and policy activations are not in this profile. The start
writer does not enqueue a V3 engine, validate artifact bytes against an
output schema, write run events, or declare a terminal hash. V11 remains a
published predecessor object; exact V7 through V11 files are refused without
mutation, with no runtime migration or downgrade. Until a named maturity
there is no compatibility promise.
