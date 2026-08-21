# Product intent

Audience: humans and agents deciding what Atelier 2 should become.

This file owns implementation status: what Atelier 2 currently is, proven by
what has landed. It does not own why the atelier exists — that reading is
[VISION.md](VISION.md), and behind it
[GitHub Issue #1](https://github.com/FlexOr2/atelier-2/issues/1). The intent
section below is a derived view of those sources, kept only so this file still
reads as one piece; where it and a source disagree the source is right and
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
- treat every agent node as the same kind — a code node and a review node
  differ only by what the workflow gives and allows and by the agent Markdown;
  a successor sees declared outputs, not the predecessor's workspace;
- land a changed tree only through the platform adapter (an Action node, or
  the same effect as a grant on an Agent), with the secret never in the lease
  and no ambient CI credential; and
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
That frozen chain currently ends at the `AgentConfigurationRevision`: it binds
model, authentication profile, executor and requested capability, but no Markdown
agent-definition revision or system prompt. A format-3 node's executable job today
is its authored instruction composed with the exact named run material it reads.
The target configuration-to-definition link is owned by
[ADR 0007](decisions/0007-catalog-identity.md), not inferred from matching model
fields.
Each configuration also binds a typed requested execution capability. Migrated
configuration revisions retain their original V1 hash and mean `headless`; new
API publications use the capability-aware V2 hash format and name the capability
they request, `headless`, `headless_with_tools`, or `interactive`. A caller that
omits it publishes `headless`, byte-identically to a publication made before the
field could be sent. Publication binds an executor key, not a capability, so a
configuration may request more than today's executors serve. Every executor
registry entry must attest at least one capability an unattended attempt can ask
for — `headless` or `headless_with_tools` — because the runtime drives every
attempt and stands at no terminal; a run requesting a capability absent from its
exact provider/executor entry is refused before any provider process starts,
whichever direction the mismatch runs. A
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
replaying the invocation, and reads an absent witness as the cleanup it attests
rather than as an answer it must wait for -- a host that restarts its serving
unit takes the whole cgroup subtree with it. A serve start also stops every
attempt no live workflow is driving any more: a workflow that ended without
moving the attempt it drove is not pending, so nothing replays it, and the
restart puts each such attempt under one durable `atelier2-driver-lost` command
and lets that same cleanup path end it as `INTERRUPTED`. The inventory is read
lazily in bounded attempt-identity keyset pages; scan progress is not durable,
so a restart begins again from current durable truth. An attempt whose driver
is merely waiting to be recovered is left alone. One explicit replacement
creates a distinct ordinal-2
attempt and workflow only after cleanup; ordinal 3 and automatic provider retry
do not exist. A known reaped unsuccessful child becomes `FAILED`; a success
records the non-secret operational identity and the exact output bytes in one
atomic attempt/receipt/event/run transition. Which bytes may become that success
is the declaring node's decision: a V2 node bounds them and nothing else, while a
V3 node's answer is read against the schema its own output pins before any of
that transition is written.

Schema V27 stages the Core half of the external Runner handoff; no runtime or
codec calls it yet. One attempt can instead bind one manifest and Runner
generation, arm one invocation, and accept one of six typed terminal evidence
variants under a semantic hash. The product result and that evidence hash commit
in one transaction, retries of the same evidence are idempotent, and different
evidence collides. Success keeps the exact provider bytes; a provider failure,
an output-limit ending, and a supervision-boundary failure use the existing
failed-attempt seam under their closed codes, which the V2/V3 API and cockpit
decoders now carry. A lost invocation stays publicly `POSSIBLY_RAN` and writes no
invented ending. Physical cancellation becomes `CANCELLED` only for the exact
Core command; an in-hand completion for a no-replacement cancellation wins and
keeps its bytes. `NEVER_LAUNCHED` is control evidence instead: the prepared
attempt stays nonterminal until Core commits and acknowledges it, after which
only that same attempt may bind a fresh Runner generation and clear the old
handoff fields. A runner-bound replacement request and a tool-grant-bound result
are refused before product or evidence mutation. Runner-bound attempts never
enter the legacy driver-loss queue. A carrier-neutral Application handshake now
reads terminal evidence, commits it, acknowledges it outside the Core
transaction, and recovers Runner GC from the typed ACK tombstone. A canonical,
bounded, self-checking V1 record now carries the envelope or its payload-free
tombstone; missing, corrupt, oversized and unavailable-ACK outcomes remain
distinct without inventing evidence. This is proven only against a byte-backed
test Fake: no production journal reader, Runner adapter, transport, runtime
caller, carrier, or live execution path exists yet.

Every attempt is started in a scratch working directory of its own. The operator
declares one provider-neutral scratch root, and the runtime leases from it a
directory named after the exact attempt identity, so an attempt and its
deliberate replacement never share one. The root is bound to a held directory
descriptor and refused before any provider starts when it is shared, belongs to
another user, is reached through a symbolic link, lies inside a git worktree, or
holds anything that is not an attempt workspace; an attempt whose directory
already exists is refused with that directory untouched, and no provider runs.
A provider may write whatever it likes inside its own directory: the measured
Claude CLI materializes a set of empty configuration and lock files there even
tool-free, which is why a provider is never handed the operator's own checkout.
What it is handed instead, where a project is declared, is that project's source
at one commit: the source is resolved to a commit when the node's durable binding
is composed and never again, and the tree that commit names is unpacked into the
leased directory before the provider starts. The tree travels without its
repository, so nothing in the lease can commit, fetch or push, and an attempt
whose pinned commit the source can no longer answer for is refused in that
source's own words before the attempt is claimed. The directory is removed once
the process and its descendants are proven gone and the attempt is durably
terminal -- after attested cleanup for a cancelled one -- and a restart removes
what terminal attempts left behind while preserving every nonterminal one.
Removal never follows a symbolic link out and never touches the root itself. This
is a directory holding pinned material, not an operating-system sandbox: the
process still runs as the serving user and can name other paths.

The first real provider now sits behind that durable contract. When the operator
declares a Claude executable and a credential directory, the host composes one
Claude subscription executor. It decides no working directory. It runs the bound
model headless through the CLI's print-JSON envelope, hands the job to the
process over standard input rather than its command line, and grants it only
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
bound model headless through `grok --output-format json`, takes only the
envelope's final answer to the output seam — never the turn narration —
and refuses an unreadable, empty, or answer-less envelope instead of
recording the raw frame or the story of the run. When the node declared an
output schema that is not a bare `type: string`, the same published
document bytes the seam later judges travel as `--json-schema`; the
seam remains the last instance if the provider ignores the flag. A
bare string schema does not take that flag: the model writes free
text and the adapter serializes it as one JSON string, because
constraining grok 1.0.4 to a string document produced announcements
or trailing `<|eos|>` rather than a later answer. The job travels
through `--prompt-file` rather than the argument vector. The same
vector pins a turn ceiling so a Diff-Review-sized order cannot run an
unbounded loop, and the child inherits only the serving host's search path
plus one disposable invocation-private `HOME`/`GROK_HOME`. That home
receives a private copy of the source `auth.json`; provider sessions and
responses stay there and the entire home is removed after success, known
failure, cancellation, retry refusal, or process error without touching
another invocation. Before launch, `grok inspect`
must report that exact home/configuration as its only configuration source,
all external-compatibility imports disabled, and no ambient trust surface.

An agent node may also use tools itself through a second Grok executor of the
same deployment rather than a widening of the first. It is armed separately
by `--grok-workspace-tools`, because it is a separate grant: naming a Grok
executable still serves only the tool-free call. Grok splits the two
switches Claude combines: `--tools` names the built-in IDs the model may see
(`read_file`, `list_dir`, `grep`, `search_replace`, `run_terminal_cmd` — the
Headless-documented shell ID; parse does not check names), and `--allow`
names the five permission classes it may run without asking (`Read`, `Edit`,
`Write`, `Grep`, `Bash`) under `--permission-mode dontAsk`. `--deny MCPTool`
keeps MCP meta-tools from remaining visible. Every other containment switch
and the private `HOME` of the tool-free call stay. It attests exactly one
capability, `headless_with_tools`, and no other; a node reaches it only where
its own durable binding asked for that capability, and a binding that asks
the tool-free executor for tools, or this one for a tool-free call, is
refused before the run exists. Because a version answer is not startability,
and because a Clap refusal without an isolated home can exit 0, the
deployment starts this exact argument vector once at composition with no
credentials and a dummy `--prompt-file` in a private `HOME`, and reads that
the CLI did not refuse an argument, with an unknown flag beside it as the
control; the marker is `unexpected argument`. Neither call reaches a model.
The executor claims no operating-system isolation, does not use
`--always-approve` or `bypassPermissions`, and does not pretend parse-time
ID validation: the process runs as the serving user and its tools reach what
that user reaches. Unlike the tool-free call, it has no billed tool-using
answer yet on any release — that a real answer uses exactly these tools, in
particular the Headless-documented shell ID, is the half one billed
secret-file probe still has to establish under the operator's gate, which is
why nothing composes this executor unless an operator armed it by name.

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
configuration records — is never inherited by a served agent. For Claude,
Grok, and Codex alike, pin and attest still run. A failure names
`start_refusal` and omits the factory; serve stays up. A run that binds the
refused executor is the same binding-unavailable refusal as a missing one,
before any process starts. Two leftovers: the picker has no startability field
yet, so an unstartable executor is simply absent rather than badged; and a
nonterminal run already bound to that executor still raises
`DbosRuntimeBindingConflict` at compose if the factory is omitted.

The raw frame a provider writes has its own bound, distinct from the durable
output bound, because the durable answer travels inside a JSON envelope. The
executor declares that bound on the invocation it prepares, since the frame is a
property of its own wire format, and supervision holds the process to exactly
that declaration: a frame past it is terminated and refused before the executor
ever decodes it, so no answer is recorded. It is sized so no answer the durable
contract accepts can be refused as a frame. The job those processes receive —
stdin for Claude and Codex, a job file for Grok — is held to the process-input
bound, a separate decision from the durable answer bound. After a chain the job
can carry the instruction, the run's orders, and earlier results; a composition
past that bound is refused by name.

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

Every one of those switches was measured to be there and to really parse against
each exact Claude release the executor admits, and one contained call on each
shows what they leave standing when they act together; what that one call cannot
separate — which single switch stops what, and the controls that make such an
attribution mean anything — stands from the release it was first measured
against. So the deployment reads the declared executable's version before it
composes anything and admits only a release such a conformance run covers — not
merely a new enough one, and not one that merely sits between two covered ones,
because a later CLI can change a control the containment depends on, and
admitting it costs a rerun of that one-call matrix and a decision about this
executor's operational identity. The same startup attests that this host
carries no administrator-managed policy: policy is outside every switch above by
design and can still start a child beside the credential directory. Reading the
declared executable's version is itself the first execution of an external
binary, so it is run with no environment at all, in an empty private directory,
under a byte bound on both streams and a deadline that kills the whole probe
session. That is containment, not isolation — the process still runs as the
serving user — so this executor may only be declared for a loopback bind.
Serving it on a reachable address is refused at startup, because starting a
billed provider is unauthenticated on this API. OS-enforced isolation remains
the planned stronger boundary; until it lands, these refusals are what keep the
tool-free premise true.

An agent node may also use tools itself, through a second Claude executor of the
same deployment rather than a widening of the first. It is armed separately,
because it is a separate grant: naming a Claude executable still serves only the
tool-free call. Its invocation drops the tool ban and names one closed set of the
CLI's built-in tools twice — as the tools it offers and as the tools it
pre-approves, which is the only grant the CLI honours once this deployment asks
it to scrub subprocess environments — and keeps every other switch and the whole
environment of the tool-free call. It attests exactly one capability,
`headless_with_tools`, and no other; a node reaches it only where its own durable
binding asked for that capability, and a binding that asks the tool-free executor
for tools, or this one for a tool-free call, is refused before the run exists.
Because a version answer is not startability, the deployment starts this exact
argument vector once at composition with no job and reads the CLI's own refusal
of a missing argument, with an unknown flag beside it as the control; neither
call reaches a model. The executor claims no operating-system isolation and does
not pretend the lease is one: the process runs as the serving user and its tools
reach what that user reaches, the workspace is where it is started rather than a
boundary it is held inside, and what it writes there falls with the attempt.
Unlike the tool-free call, it has no one-call conformance matrix against a real
subscription answer yet: that a real answer uses exactly these tools and brings
no customization back with them is the half one billed call still has to
establish, which is why nothing composes this executor unless an operator armed
it by name and the packaged deployment does not.

Workflow format V3 is authored in full and executed in one shape. The parser accepts a
format-3 document into its own closed model — the five node kinds with the field
matrix each requires, refuses, or accepts, `depends_on` as the only control edge,
the join rule in its three arities, the input sources a node may read — another
node's output, that node's terminal receipt, a context entry, and the order the
graph itself was started with — the two context-edge kinds, graph-level inputs
and outputs, and the loops that repeat a stretch of the graph — and refuses every
forbidden form naming the node and the field it concerns, including each retired
V1 or V2 key with its replacement.

A document may declare that a stretch of its own graph repeats. The declaration
names the loop, the nodes it repeats and a maximum number of rounds that has no
default: an unbounded loop is refused by name, as is a body the declared edges do
not order in one uninterrupted stretch, a node two loops claim, and a loop
repeating a node nothing declares. A control edge pointing backwards stays refused
as the cycle it always was, so the declaration is the only legal way back. A run
whose document declares a loop runs the body again from its first node while
rounds remain and ends when the bound is reached, through the terminal path every
other node ends by. Every round of every looped node is its own node execution
with its own deterministic identity, durable request, receipt, produced value,
durable workflow and event in the chain the terminal hash recomputes over — while
the first round of a node keeps byte for byte the identity it had before any loop
existed.

A loop has a second, earlier way out: the answer a round produced. The
declaration may name the node whose verdict decides and the verdict that sends
the loop round again, and the engine reads that verdict out of the value the
round kept and chooses the edge — another round, or the way out — while the
declared bound stays the fallback no verdict gets past. The vocabulary is closed
and has one owner (`accepted` and `revise`), and the answer carrying it is judged
by a schema derived from that same vocabulary and published as an ordinary
revision: a deciding node's one output must pin exactly that revision, or the
document is refused by name, as is a verdict read from a node that does not close
the round. That is what makes a `revise` a *successful* node whose content chose
the next edge rather than a failure. What no verdict can say yet is the agent's
own named refusal — "the order is unclear because X" — because a run ends failed
only under an attempt failure code whose value list is a store contract, and a
refusal written under either existing code would name a schema or a dead process
that was never involved. A loop body may hold only agent nodes. A `from` edge
whose source sits in the same loop and is not ordered by `depends_on` reads that
source's immediately previous round — one payload, the producing output's schema
— and is honestly empty in round one. A value read *out of* a loop is still
refused by name because no rule here says which round wrote it. Unsafe YAML is
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
than a refusal. That profile is now applied to values as well as to schemas: an
agent's answer is read against the schema its node declared, by that one owner,
before the answer can become anything. Bytes that fall outside the profile are
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
those bytes. A workflow already published through `POST /workflow-revisions`
is named through `POST /workflow-lineages` from those same bytes and the same
hash; founding does not invent a second identity. Run-configuration binding is
still lineage-free and a reference's `ref` is carried into that snapshot
without calling `resolve_reference`. A
64-hex query is a lineage id; anything else is a display name. A retired
lineage is refused by id or any alias. There is no capability attestation, and
no runtime executes a child.

A valid V3 document is publishable long before all of it is executable: it
becomes an immutable revision under the same exact-bytes hash identity as V1 and V2,
and the revision projection names its format and says what still has no owner, while an
invalid one is refused at publication carrying that named node and field. One shape of
it runs: a single line of Agent, Wait and linear Action nodes, each entered by at most
one dependency and followed by at most one dependent, declaring no optional form the
runtime does not bind. `required_context` and `available_context` are parsed target
forms but remain in that refused set; neither is a template parameter and no start
substitutes its authored source or revision. Per-run work instead enters through a
declared `graph_input`, supplied as exact `RunInput` material. A document outside
that shape is refused at the start naming what it is waiting for — a node kind
nothing interprets, a branch nothing chooses between, an authored form nothing
binds — rather than naming its version, and writes no run. V1 and V2 documents keep
their exact meaning under their own models, and their wire bytes are unchanged.
[ADR 0006](decisions/0006-node-vocabulary.md) owns this vocabulary and the staging
rule behind it.

Inside that shape the runtime drives the line its author wrote. Each Agent node runs
its attempt through the same durable path a V2 node uses, and the heir its author
declared starts when its predecessor completes. A linear Action node pins a published
adapter-operation revision; the one operation this runtime performs is `open-pr`.
`POST /adapter-operation-revisions` is the publication door (bytes in, hash out,
idempotent), and a start whose `operation.revision` is that hash gets past the
reference that used to refuse as unpublished. The Action's request bytes are the
predecessor Agent's output. Tests inject a fake GitHub `EffectAdapterFactory` that
records a branch and pull-request number, writes the request hash into the pull
request body, and answers a replay by readback rather than creating a twin. The
served host still composes the loopback adapter; live GitHub, githubkit and a
credential reference are not composed on serve. The token never enters the lease,
a receipt, an event or an API projection, and the lease listing has no `.git`.
ADR 0010 stays PROPOSED. A Wait node holds the run in
`WAITING_INPUT` as a durable state rather than as work in progress: nothing is queued
behind it, a restart finds it still waiting, and it moves only when a person answers.
The V3 run page shows that wait as an answer card, with the authored question
the published document already carries, and sends the typed bytes through
the same `POST /runs/{ref}/answers` door the API already proved.
What that answer may be is the node's own declaration — a V1 or V2 Wait names an
`answer_type` and admits the canonical text of an integer, while a V3 Wait declares one
output with a schema and admits exactly what that schema admits, judged by the same
profile owner that reads every other value the run produces. An answer the schema
refuses is named as no answer at all and leaves the run waiting for another; an answer
to a run that is not waiting is refused as the state conflict it is. The admitted
answer is kept as the event's own bytes and carries the run to the next node, or, where
the Wait node is the line's sink, to the run's own terminal hash.

What makes a V3 agent node executable now includes the shape of its answer. The
one enforced shape is `single-json-output/v1`: exactly one declared output, whose
whole decoded bytes are its value. A node declaring none — bytes no schema could
judge — or several — one value answered by another — is refused under the name
`agent-output-shape-unavailable`, before the run is written and before any
provider process starts, while the document itself stays publishable. What comes
back from the provider is then read against that schema by the profile owner
above, inside the transaction that would have written the success and before its
first row: an answer the schema refuses leaves no agent receipt, no completion
event and no advanced run, so a run can no longer end successfully on work its
own contract rejects. The refusal is durable and named. The record family ADR
0006 declared has its production writer: the public start persists each node's
`node-execution-request/v3` and `context-package/v3` inside the start
transaction -- an order the run carries binds into that package as a material
member under its content hash -- and the terminal write ends the execution in
the same transaction as the agent receipt. A refused answer ends its attempt
`FAILED` under `OUTPUT_SCHEMA_REFUSED` with an `AGENT_FAILED` event, and the
`failed` `node-receipt/v3` carries the schema owner's own words as its reason
(`output-schema-refused: ...`) and, on that same family, the schema revision
and the hash of the exact decoded bytes the judgment used; the run itself ends
`FAILED` under that same reason — the node's ending lifted one level, so the
studio no longer lists it as Running. A success additionally keeps the exact
produced bytes as `node-artifact/v3` beside its `succeeded` receipt, and that
receipt names the same identity. An older receipt written before those fields
existed stays readable: the identity is honestly absent, not a refusal and not
corruption. Claude, Codex and Grok each take a decoded answer through that
same success-write seam; bytes the schema refuses end under the same token
for all three. The node detail reads the stored reason back, and a run started
before this writer existed stays honestly absent in those tables. A store that
still holds the old STARTED-after-failure shape is ended the same way at the
next serve start. A leftover whose last attempt on the current node is already
`INTERRUPTED` under the durable `atelier2-driver-lost` command, with no
replacement still in flight, ends the same way: the run becomes `FAILED` and
that existing interruption event is the named reason — no new event, no
attempt rewrite. A V1 run cannot take that lift, because the frozen V1 wire
refuses `FAILED`. A run that advanced past a succeeded predecessor onto a
node that never prepared an attempt is leftover only when the durable node
workflow that would start that attempt is missing or belongs to an
application version the running executor will not recover: the failed
`node-receipt/v3` on the current node names `atelier2-driver-lost`, and the
run becomes `FAILED` without a new event. A silent successor whose node
workflow is still pending under the running application version is left
standing.

The other way an attempt ends badly now says as much. A provider process that
leaves no usable answer ends `FAILED` under `PROCESS_EXITED_UNSUCCESSFULLY` on
that same seam, and its `failed` receipt carries what the supervision saw --
how the child ended (an exit code, a signal, or a clean exit whose answer no
executor could read) and a bounded tail of its standard error, under the token
`process-exited-unsuccessfully`. The node detail and the `run` command read that
reason back, and an ending nothing recorded is reported as exactly that rather
than as an empty one. Standard error stops at the receipt: the `AGENT_FAILED`
event keeps carrying the bare failure code, so the event stream stays a bounded
surface anybody may subscribe to. The bounded vocabulary deliberately not
written here: cancelled and blocked receipt dispositions.

An agent is authored as one markdown file. Its frontmatter is a closed set of
`name`, `description`, an optional `model`, and an optional `tools` declaration;
the body is the system prompt, kept byte-exact. An absent `tools` field leaves
the agent able to use every tool its executor offers, because a restriction is
only ever explicit, and a present one declares exactly that closed set. Every
other key, missing required field, unreadable value, duplicated key or tool, and
empty prompt is refused by its own name. A definition renders back to a canonical
document that reads as the same definition — the declared tool set always as a
sequence, so a tool name that itself spells the comma an author may separate by
survives the round trip — and the authoring helper maps it deterministically to an
existing agent-configuration revision, with the deployment rather than the file
owning the authentication profile, the executor, and the model an unspelled one
falls back to. That mapping is not a binding to the definition revision. This is
the authoring format alone: nothing enforces a tool declaration yet,
no serving surface publishes a definition yet, and today's configuration revision
carries no field for a name, description, tool declaration, or system prompt, so
the published revision alone cannot reconstruct the definition it came from.
Where an authored definition durably binds is decided by
[ADR 0007](decisions/0007-catalog-identity.md). Its exact bytes can be reconstructed
as an `agent_definition` revision today, but no serving surface publishes that
definition and no agent configuration references it; the round trip therefore
does not yet prove the configuration-to-definition chain.

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
publish secret-free auth-profile and agent-configuration revisions and list
both; publish
exact JSON Schema revisions; publish and
inspect immutable workflow revisions; start, list, and inspect V1 or V2 runs
(the list accepts a `state` filter so a consumer can ask which runs wait;
a page is admitted by one `PageLimit`, not a restated 1-to-100;
a persisted run format is one `WorkflowFormatVersion`,
not a restated 1-2-3 CHECK;
a cancelled attempt's cleanup disposition is one
`AgentAttemptCancellationDisposition`, not restated tokens on query and SSE);
list and inspect a V3 run from the published document it was started
against, not today's executable parse;
read the agent receipts a run has written;
an `invalid-request` names the field and reason the validator already knew;
answer a waiting node; cancel the current V2 Agent attempt with an optional
single replacement; submit an accountable reconciliation; and follow the
closed durable event history as a resumable server-sent event stream. A
subscriber who does not already know a run holds `GET /events`; opening that
stream is the subscription. The cockpit holds that stream, so a Wait or an
agent failure appears without `POST /subscriptions`. The feed is closed to `WAITING_INPUT` and
`AGENT_FAILED`, in the same envelope and `VersionedRunEventResource` the
per-run stream emits. `Last-Event-ID` resumes by same-instant identity
exclusion: from that event1's instant T,
`recorded_at > T OR (recorded_at == T AND (run_id, seq) not among identities
already emitted at T)`. Last-Event-ID seeds the set with that cursor only; a
live holder adds each identity it emits and resets the set when the second
advances. Second-precision instants make two waits in one second the normal
case, so lexicographic `(recorded_at, run_id, seq) > cursor` is not the resume
rule. Pre-V22 events whose instant is NULL
stay off the feed rather than inventing a time. A served V2
run also names the state of every node of the revision it is bound to, so a reader
is told where each node stands instead of computing it: one pure function in the
core derives that rail from the run snapshot, that revision, and the events since,
with the snapshot authoritative only until an event overtakes it. A failed
terminal snapshot names the failed node and the attempt that ended it, so a list
read matches the event stream. Success carries exactly one name on the wire. Existing
V1 JSON and OpenAPI component bytes stay pinned so nothing widens them by
accident — they moved once, deliberately, when every body learned to name a
value the way the next request writes it — while exact V2 unions expose
the run's safe binding matrix and byte-safe Agent output, and the event stream
answers a format-3 agent or wait event as its own family rather than dressing it
as V1 — a format-3 pause naming no answer type, because that format's Wait node
declares a schema instead, and its answer travelling as bytes rather than as the
decimal text only an `integer` wait can honestly produce.
Public references are transport identifiers, not new domain identities, and
retries report whether a command was newly accepted or already existed without
duplicating its durable
write or wake-up. The API also describes the one body it takes as bytes: a
guessed path is refused with the exact location of the OpenAPI document, and the
workflow publication body there carries the shape of the document itself —
derived from the models the publication reads it against, so no second
description can drift. That shape decides the form; the rules only a whole
document answers keep their named refusals at publication. It also answers in
the words the next request is written with: a workflow's revision hash and its
format version are spelled the same on every body that carries them, the path
that reads one revision is `{workflow_revision_hash}`, a declared order answers
the author's own `schema: {ref, revision}` hull, a published schema or budget
revision names its own kind, and material published as an artifact is ordered
under the address the publication answered. A machine consumer assembles each
request out of fields the answers before it named, without a translation table
of its own.
[ADR 0003](decisions/0003-http-api.md) owns the API and resume
contract.

A narrow local cockpit can list runs, publish and start a workflow from `/new`,
and project one durable run's bound revision, state, nodes, and resumable event
history. A V3 run, its list row, and a node that has run carry when they
started and ended: the store keeps UTC. The project list shows the local date
and time on the row, newest activity first, and names that sort; the run page
still keeps the exact stamp behind the info affordance. Predecessor rows that
never recorded an instant stay empty rather than inventing one. Each
project-list row also shows the one project and, when the published revision
answers a name, the workflow. The saved-workflow picker offers one row per authored name the described
listing already publishes, not one row per revision hash. Several revisions
that share a name collapse; the catalog head from
`GET /workflow-revisions/by-name/{name}` is the default when that name
resolves, and older members sit in a collapsed revision choice. A name with
one listed revision has no empty submenu. A published title the catalog does
not hold is named Unlisted when it is a legal catalog name and Unnamable when
the title cannot be one — the picker does not swallow that 404. Those
refusals, and a row that cannot be started, each have their own shape, so a
choice is not a muted twin of a refusal. After a choice the list collapses
onto that card with a Change path, and the start form sits directly under it.
Unnamed documents stay one row each, as they did. A V3 publish from the CLI or the
cockpit then names the revision through `POST /workflow-lineages`; publication
and admission stay two HTTP acts. Details repeats what the published graph already answers —
format, roles and node count where the V3 resource carries them, executability,
declared orders with the schema each pinned, the lineage's revision history,
and the graph miniature. A hash sits behind a proof affordance — hidden until
asked, copied by a click, naming what it seals. Edit shows the exact published YAML and
publishes a new revision through the same door; a legal catalog name then
joins the lineage. Per-node outputs stay in that document; the preview does
not copy them. A known start-refusal or problem token is shown as a sentence with
a next action; an unknown token stays raw. The V3 graph also answers an excerpt of each node — id, kind, role,
the bounded start of an agent instruction, and the authored `depends_on`
edges. A wait has a prompt, not an instruction, so that field is empty there.
An entry node answers an empty edge list. The authored node stays in the
document bytes. A V3 run page draws that excerpt as topological layers and
paints each node's state from the rail the server already walked — shape and
colour together, no zoom, no drag. The page leads with the published workflow
name and keeps the run id as identity. A click into a node speaks Prompt and
Output, never Asked or Answered. The Who panel labels the receipt's model as
the declared configuration model and says a provider-resolved model is not
recorded — the same honest absence as usage. A hash leads with its human
name and is copied by a click on that named control — the hex is the proof
behind the name, not the reading title. The live event line names which node
finished and does not paste the output the node already holds. A STARTED run paints the working node
as live work, not as a finished card, and shows new events from the existing
SSE door as they arrive. Empty, connecting, and failed stream states are each
named as themselves. The process log is not on that door — it stays in the
lease (#104) — and the page says so rather than inventing a progress bar. The
live event line stays open until the events it has applied match the latest
cursor the run itself names, so a run that has already ended still shows every
node that finished. Details on the
saved-workflow picker reuses the same drawing without run state. A chosen V3 revision that declares
orders shows one material field per order — the name and the schema the
author pinned — and sends the typed text as `orders` on the start; a revision
that declares none shows no field. Role
bindings on `/new` offer published agent-configuration
revisions by provider, model, and readable auth mode; the raw publication form
stays as a collapsed expert fallback. Last choice per role is remembered in
this browser only. For an admitted workflow with roles, the picker reuses the
catalog read's lineage id and reads the configured project's current occupancy.
Each untouched role takes a known project binding before a valid remembered
choice before empty; missing project roles fall through independently, while an
unknown project hash stays visibly unavailable rather than becoming the browser
choice. Late reads do not replace manual draft values, and a failed read keeps
same-lineage confirmed truth with one retry. Unlisted, retired, unnamable, and
roleless workflows do not read occupancy. The agent list is empty until a
configuration is published, and says so. It opens in the Studio rather than in
that list: one screen across the
whole workshop naming every run that waits for a human — the durable states
`WAITING_INPUT` and `WAITING_RECONCILIATION`, each asked of the list by
`state` — beside the one project of this installation, whose card counts
what is running, what waits, and how many have landed. An area with nothing
in it names the next action that is possible today. The chat column is the
named door to the conductor (#7) and says it is not built, without an input
that answers nobody. The listing has no clock, so the card does not invent
when the last run landed. Every level sits in the target-UI skeleton from mockup v4: a left rail
and a topbar. Studio and Projekte open today's pages; Runs, Library, and
Settings are named and disabled with their vision reference. The topbar carries
the atelier·2 wordmark and the one project. No page was added behind those
destinations. Project is the one editing surface for that durable
recommendation: after it confirms one complete workflow, catalog, agent,
detail, and occupancy snapshot, an operator may choose or explicitly remove an
authored role. Foreign and unavailable bindings stay intact unless the operator
changes an authored role; an uncertain save retries the same bytes and a
conflict requires Reload. The new-run trail names the project the same way the other
levels do. It can answer the exact integer requested by a Wait node and resolve an
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

A packaged container candidate now runs that same cockpit and catalog as one
provider-free Serve image. It builds only from a clean committed tree, runs
non-root with a read-only root, dropped capabilities and
`no-new-privileges`, and keeps durable state in one fresh project-scoped volume.
Each start chooses a Compose project and loopback port, labels its resources
with the exact source identity, builds from an archived committed snapshot,
waits for the matching health identity, and prints its exact shell-safe
teardown command from a private candidate-lifecycle descriptor, so later
checkout changes cannot redirect cleanup. It has no provider executable,
credential/configuration or host-home/scratch mount, Runner service, runner
protocol or external execution claim. The existing Core `ExactOutput` fixture
remains Core behavior, not a packaged provider. The operator runbook is
[OPERATIONS.md](OPERATIONS.md); network hardening remains
[ADR 0009](decisions/0009-runner-trust.md).

That API now has a command-line client of its own, so starting real work costs
one command instead of four ceremonies. `atelier2 run` publishes one workflow
document and one agent file per bound role, starts the run they describe,
follows its event history to the end, and writes the agent output that run
produced to standard output, with the run, its revision, its terminal hash and
one hash per output on standard error. The agent file may name
`requested_capability`; omitting it publishes the wire default `headless`, so a
tool node is startable from this command rather than only from a raw HTTP
client. Every publication is idempotent and the
run identity is derived from the published hashes unless the operator names one,
so the same command run twice reports the first run instead of paying for a
second. That identity compare pins authored `--input` orders the same way it
already pinned `run_inputs`, so a retry of the operator door is
`DurableRunExisting` rather than a conflict. The client owns nothing: it holds no durable state, adds no route, and
hands the service's typed problems on unchanged, whether the service refused an
answer or ended the event stream with its own failure frame. A run that stops on
a decision the command cannot make — a waiting node, an unknown effect outcome, a
failed agent attempt — ends it by name with a nonzero exit code instead of
waiting. Exit 0 says the command read that run's history as far as the run's own
latest event, so a history that broke off is refused by name and a truncated or
empty output is never dressed as a receipt. A
run started through `start_published` now carries its order beside the document:
the exact bytes are stored under the run and the name its author declared,
immutably, and the agent whose node reads that order is handed it -- so one
published revision serves every order instead of one revision per distinct input.
An order is refused before any row exists when it is missing, undeclared,
supplied twice, pinned to another schema than the document named, is a value
that schema does not admit, or names an artifact nobody published. An order need
no longer be written into the start it travels in: material larger than the
inline bound is published once as a content-addressed artifact -- the SHA-256 of
its exact bytes is its address, publishing the same bytes twice is the same
artifact -- and the order carries that address instead of the bytes. The start
resolves it before anything is written, the schema judges the resolved bytes, and
the agent is handed all of them, so a full pull-request diff reaches its reviewer
while the inline bound stays strict. Only an order the graph declares binds
today; an
input reading another node's output, a node receipt, a context entry or an
authored constant is refused by the source it named. A workflow name is no
longer among what is missing either: `--name` runs the revision a catalog name
holds, asked of the service before anything is written and at the lineage member
`--position` names, so an operator starts named work without translating a name
into a hash by hand. `--input NAME=VALUE` and `--input-file NAME=PATH` fill the
`graph_inputs` that workflow declared: the command publishes nothing for them
and hands the exact JSON bytes to `POST /runs` inline. Ordering an artifact from
the command line, and any surface that lists or reads stored artifacts back, are
not built. A name the document never
declared, a declared name that is missing, and a value that is not valid JSON
for the schema the document pinned are each refused by name; a typed 422 from
the service is handed on in the service's own words. An output contract that
could decide an exit code still does not exist.

That API now also has a third door: `atelier2 mcp` speaks MCP as one
JSON-RPC object per line on standard input and standard output against the
same public HTTP API. A client launches
it as a child. There is no listener, no port and no token. The five tools
are `list_workflows` (catalog name, lineage and head), `start_run` (the
revision a name holds, the same resolution `run --name` asks, with the same
inline-or-artifact order union `POST /runs` already publishes),
`run_status` (the run resource as the API answers it), `answer_wait` (the
#194 door) and `publish_artifact` (`POST /artifacts` as octet-stream; MCP
JSON carries those bytes as standard Base64 because it cannot speak
octet-stream). Each call is the HTTP door; a typed problem is returned
unchanged, field pointers included. The artifact size bound stays the
store's own: this door does not invent a second cap. A stdio JSON-RPC line
cannot carry a maximum-size artifact after Base64 expansion; that leftover
is named, not papered over by shrinking the bound. The API has no caller
authentication: #82 is human OIDC and ADR 0009 (machine credentials) is not
landed, so this child invents none and refuses any service that is not a
literal loopback address — the same trust the browser already has on this
machine. Instants on the run wait for #355; this door does not invent them.

A node can now say which tool it needs and have it redeemed. A `tools` entry is
a published tool grant the document pins by hash, exactly as an output pins its
schema, so what a node may do is byte-pinned like every other material it names;
the one capability a runtime here redeems is `run-project-verification`. A
client publishes those bytes through `POST /tool-grant-revisions`, the same
form as a schema: exact JSON in, the catalog's own write, hash out, refused by
the grant owner's own name before anything is stored. When
such a node runs, the command the project's own manifest declares under
`[tool.atelier2.verification]` is run in that attempt's own leased directory --
the project decides what verifies it, never the agent and never the atelier.
A command that exits zero leaves durable proof of exactly which command ran,
how it ended and the hash of what it wrote, beside the agent receipt whose
provider bytes stay its own. A command that exits nonzero ends the attempt
`FAILED` under `PROJECT_VERIFICATION_FAILED`, names how it ended on the
`failed` `node-receipt/v3`, and writes no agent receipt, no `AGENT_COMPLETED`,
and no `tool_redemptions` row. A granted verification that exceeds its
declared `timeout_seconds` after the claim ends the same way: the attempt is
`FAILED` under `PROJECT_VERIFICATION_FAILED`, the `failed` `node-receipt/v3`
reason names the timeout, and the attempt is not left `LAUNCH_ARMED`. The
manifest that is read is the one the pinned commit carries, and the
directory it runs in is that same lease after the provider has worked there, so
what a project declared stays the pin's and where it was run is the mutated
lease rather than a rematerialized pin tree or a living checkout. Refusals are
named rather than worked around: a grant
naming a capability nothing here performs, or bytes that are no grant at all,
refuses the run at the reference that pinned it; a node pinning more grants than
one attempt redeems is refused by that count; a project stating no verification
at the pinned commit refuses the attempt in the words of the manifest that should
have stated it; and a root that is no repository of its own is refused before the
server exists -- each before any provider process starts. What this does not
claim is isolation: the leased directory is still honestly "not a sandbox", the
verification runs as the served process's own user, and enforcement at a boundary
that cannot be talked out of is not built. Neither is the static capability
attestation of a build -- declared, resolved, redeemed and proven is the whole of
the claim.

A node's `budget` is content now, not a word. A `budget_policy` revision is
published through `POST /budget-revisions` and carries exactly four bounds: the
hard `attempt_deadline_seconds` every budget states, an optional hard
`maximum_assistant_turns`, and the two `reported_*_token_threshold` values a
provider can only report after the work it measures. The names carry that
difference, so no surface can offer a post-hoc number as a maximum, a cap or a
ceiling. Every present value is a positive signed 64-bit integer, an absent
optional is not zero, and money is absent by decision: an authentication mode
selects a credential path and measures no charge. Bytes that bound nothing are
refused by their own name -- an unknown field such as a cost ceiling or a run
budget, an explicit null, a zero, a fraction, a boolean, a value past signed
int64, prose -- at the publication door and again at the reference that pins
them, so no run starts under a budget nobody could read. A budget revision is
identified twice, on purpose: the registry and the node pin the exact bytes,
while the four bounds have their own `budget-revision/v1` content identity, which
catalog lineage, display name and revision position never enter. A document that
writes `budget:` is executable: the start resolves that pin the same way it
resolves a schema or a tool grant, and the attempt reads the bound from those
published bytes. The hard turn bound now reaches both workspace-tool executors:
a node that pins a budget naming `maximum_assistant_turns` launches with that
value as `--max-turns`; a node that pins no budget, or a budget that names no
turn bound, keeps the executor's existing default. What this still does not
claim: the deadline does not run a clock, the reported token thresholds judge no
usage report, a tool-free attempt does not read the bound, and the executor-side
declaration of which dimensions a revision requires and what ceiling it attests
is not built.
The first fully budgeted V3 Agent attempt -- deadline clock, reported-token
thresholds, executor-attested refusal, usage and receipt binding -- belongs to
#455 after the durable Runner work in #15 and #301.

Whoever recomputes a finished run's terminal hash now also proves under which
binding it ran. The agent receipt already folded provider, auth mode, auth
profile revision, model, executor revision, configuration revision and request
hash into one value; that value is a named position in the `AGENT_COMPLETED`
event's own preimage, so the fold from receipt fields through event hashes to the
terminal hash misses under any other binding. Older events are untouched: the
`node-event-hash/v3` domain is chosen by content, so a completion that carries no
receipt binding keeps the hash it always had, and an event written before this
version carries no binding rather than an invented one. What is still not proven
is the request hash's own preimage: the job bytes it is taken over have no
durable home, so a verifier copies that hash rather than recomputing it.

The host keeps one live-versioned configuration channel. It is durable,
append-only-versioned in the `auth_profile_revisions` form, and readable at
runtime. The first entry is `project id → root path`. The second is recommended
occupancy per workflow lineage, versioned beside that mapping and readable
over HTTP. Today's store is the
first project: that configuration entry, and the reads that treat it as a
project. CLI flags remain bootstrap of where the channel lives --
`--database` is the store, and `--project-id` with `--project-root` may write
the first mapping -- they are not a second copy of the map. After the mapping
exists, compose and the run path that needs a project root read it from the
channel for that project id, not from a second `--project-root` flag. A bad
project id -- including text that is not exact UTF-8 Unicode scalar text -- is
refused `project-unknown` before hashing or configuration. `GET
/atelier/api/v1/projects` answers zero or the one project this process opened,
and its delivered `project1.` reference addresses the identical detail
resource. That resource exposes neither the internal id nor the root path. A
different well-formed reference is `project-unknown`; a malformed reference is
`invalid-public-project-reference`. A configured id with no root is not an
empty collection, and unreadable or corrupt configuration stays visibly
unavailable or corrupt. There is no HTTP project write, second project,
pagination, project editor, or store-per-project process.

The canonical store is schema V27. A fresh store is created as exact V27 and
carries published revisions of the closed kind set, lineage membership bound
to those revisions, append-only alias and retirement histories, format-3
runs, immutable node artifact bytes, node receipts, their ordered output and
access bindings, and the immutable declared context packages, node-execution request
preimages and run configuration snapshots those receipts name, and the immutable
orders a run was started with, the immutable proof of every redeemed tool
grant, the receipt hash an agent completion binds, immutable content-addressed
artifacts an order may name instead of carrying their bytes, the round a
declared loop was turning when each run, event and agent receipt was written, and
the host configuration channel's project-root revisions and occupancy
revisions. The catalog adapter founds a lineage
and admits members through a typed writer that derives `CatalogLineageId`
from kind and founding hash and refuses a mismatched id before mutation. An
admitted name or lineage id resolves to the exact published bytes; a missing
founding, unpublished member, wrong kind, or retired lineage is refused by
name. Measurements and policy activations are not in this profile. Every schema
from V9 up to the one just below current remains a published predecessor
object -- `schema.py` names each as its own `V*_SCHEMA_HANDOFF` constant -- and
an exact file at V7 through the version just below current is refused by
runtime without mutation, with no runtime migration or downgrade. An offline
`atelier2 migrate` command raises an exact store on any source version
`schema.py`'s `_SCHEMA_MIGRATION_STEPS` ladder still names to the current
schema, one published step at a time. Until a named maturity there is no
compatibility promise.

On 2026-08-19 at `ed6376b` this landing measured how many concurrent
fake-executor runs one SQLite instance carries. The harness is in-process ASGI on one event loop,
production query-admission bounds, a V3 one-agent document, and
`RecordingAgentExecutorFactoryV2` — not Claude, Grok, or Codex. It carried 96
concurrent runs without a named HTTP or stream refusal. The first observed
pressure was event-write latency: 0.42s at the CI n=2, 12.3s at n=96. The start
door crossed the instance's 1s query-admission wait from n=16 (1.22s) and still
answered 201. The 30s SQLite writer-lock timeout, process-spawn, watchdog
cgroup, and memory failures were not observed. That is a measurement, not a
capacity promise and not a Postgres or #312 decision. The 503 knee is leftover;
[OPERATIONS.md](OPERATIONS.md) names the operator command that raises n.
