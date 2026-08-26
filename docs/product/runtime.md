# Runtime status

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
agent-definition revision or system prompt. A Markdown agent definition itself is
already publishable as an immutable `agent_definition` catalog revision over
`POST /agent-definition-revisions` — exact authored bytes in, hash out, with the
identical definition reconstructable from the stored bytes — but no
configuration references it yet. A format-3 node's executable job today
is its authored instruction composed with the exact named run material it reads.
The target configuration-to-definition link is owned by
[ADR 0007](../decisions/0007-catalog-identity.md), not inferred from matching model
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

Schema V27 stages the Core half of the external Runner handoff. A disposable
#301-A witness candidate now exercises a real codec against it end to end
outside any deployment; no deployed runtime composes or calls it. One attempt
can instead bind one manifest and Runner
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
distinct without inventing evidence. This is proven against a byte-backed test
Fake and, for the #301-A disposable witness candidate (`src/atelier2/runner/`,
its adapters, and `application/run_runner_session.py`), a real session codec,
TLS-authenticated transport, journal, and Landlock-confined child; the
witness itself remains a disposable proof, not a packaged, deployed Runner,
but Serve can now compose the same real driver as an executor's dispatch
carrier (`#540` C-3.6, below).

Dispatch names which authority starts one executor key's process (`#540`
C-3.6): a registration declares itself `LOCAL_PROCESS` or `RUNNER_LEASE`, and
only a `LOCAL_PROCESS` key needs Serve's own process supervisor, cgroup and
scratch root — a deployment serving nothing but `RUNNER_LEASE` keys starts
without any of them. `RUNNER_LEASE` reuses the driver above rather than a
second one: Serve publishes the lease a host launcher claims
(`atelier2.adapters.file_runner_leases.FileRunnerLeasePublisher`), drives the
accepted session to `RELEASED` over the same real transport, and never runs,
mounts, or supervises a process itself. Exactly one such offer exists today —
the fixed fake-free candidate — and reaching it names a lease root, Runner
image, image digest, console container, core identity directory and accept
deadline together, refused by name if only some are declared; the packaged
`atelier2 serve` command line exposes all six as flags, and the shipped
container entrypoint carries them from `ATELIER2_RUNNER_*` environment
variables without validating them, so this carrier is reachable from the
shipped container while an undeclared deployment stays runner-free — no
runner value or identity file is baked into the image. At most one
`RUNNER_LEASE` Attempt runs at a time per Serve process — its Core session listener binds one fixed port — and a second,
concurrent one waits as a durable queue row, holding no worker, rather than
failing. At every start
Serve withdraws its own still-open leases and converges every `RUNNER_LEASE`
Attempt no workflow still owes its next move: it reads the launcher's own
retained terminal record back from the Attempt's handoff and commits it to the
terminal the Runner reported, exactly once, rather than the invented
`INTERRUPTED` a driverless sweep would write — an Attempt whose fact never
reached the handoff is left armed and named, never forced (`#540` Kind #585).
The launcher lays that record down out of the per-Attempt journal volume — the
only place it lives — before the Attempt is removed, and only for an exited
Runner, so a running one is never read mid-write. A `RUNNER_LEASE` Attempt that
crashed between binding its generation and publishing its lease is
manifest-bound with no lease document, so a later cancel fails loud with
`RunnerLeaseUnknown` rather than lying; its never-launched durable close is a
named gap with its own item (`#540` Kind #584).

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

P2a candidate store (`.atelier2-candidates.git`, ADR 0011 decision 2) is built
and deliberately unwired until P2b.

An attempt now leaves behind what it did, not only what it answered. The
executor decodes its provider's own structured stream into one neutral shape --
a tool call with its arguments, the result that came back, an assistant turn,
and what the call spent -- and every step is scanned for credential shapes and
cut to a readable width before anything durable sees it, so a token a tool
happened to print is replaced and marked rather than dropped or kept whole. A
line this vocabulary cannot read, including whatever a call that produced no
stream printed instead, is kept as the provider's own output under its own
name; a transcript with more steps than one artifact may hold loses its oldest
and says how many. The whole is one content-addressed artifact, and the attempt
names its address. Every ending keeps one — a success, a refusal, a nonzero
exit, and a granted check that never answered — because an exit code beside an
empty standard error was the entire account of a real failed run, which is no
account at all, and because an absence meaning two different things could not be
read at all. An attempt whose executor publishes no structured stream names
nothing, which is the honest absence rather than an invented shape.

Three guards make the address mean what it says: it must resolve to bytes this
store holds, it may stand only on an ended attempt, and once written it can
never be moved or cleared. Redaction is not a door a caller may walk past
either — building a transcript at all is what makes its steps safe. What the
boundary to a Runner cannot yet carry it refuses out loud instead of dropping:
that record is bounded far below one transcript and its hash is part of Core's
acceptance chain, so carrying one is its own decision rather than a silent
truncation. No read surface serves the transcript yet — the API projection and
the node panel's Log tab are the next slice.

The first real provider now sits behind that durable contract. When the operator
declares a Claude executable and a credential directory, the host composes one
Claude subscription executor. It decides no working directory. It runs the bound
model headless through the CLI's print stream, whose last line is the same
answer envelope its predecessor read alone and whose earlier lines are the
attempt's steps; that changed wire shape is a changed operation, so each of the
three Claude vectors carries its own new operational identity rather than
widening the one it had. It hands the job to the
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
instead of recording invented output — each of them keeping the transcript of
what the call did write, so a refusal names more than its own code. Undeclared, the host composes no V2
provider factory and behaves exactly as before. When the
operator also declares a Grok executable, workspace, and credential directory,
the host composes one Grok subscription executor beside Claude. It runs the
bound model headless through `grok --output-format json`, decodes every
concatenated JSON value the CLI writes, keeps progress values in the attempt
transcript, and takes only the last envelope's final answer to the output seam.
It names an ending with no final message, rather than recording progress as an
answer. When the node declared an
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
Grok, and Codex alike, pin and attest still run. A failed deployment remains a
declared registry entry without a factory, so Serve stays up and the current
configuration list answers the closed startability pair without a host
diagnostic. New Run retains a known unavailable Project or Remembered binding,
marks it `◇ Unavailable`, and permits an explicit healthy draft choice without
rewriting project occupancy. When an already bound, unstarted run reaches that
entry, it writes one attempt-less `agent-executor-binding-unavailable` terminal
refusal before a provider process starts. A missing declaration or capability
mismatch remains the loud runtime binding conflict, and every attempt that may
have crossed its launch fence remains #15 recovery work.

The raw frame a provider writes has its own bound, distinct from the durable
output bound, because the durable answer travels inside a JSON envelope. The
executor declares that bound on the invocation it prepares, since the frame is a
property of its own wire format, and supervision holds the process to exactly
that declaration: a frame past it is terminated and refused before the executor
ever decodes it, so no answer is recorded. It is sized so no answer the durable
contract accepts can be refused as a frame, and where the wire format carries
the attempt's steps ahead of that answer it is sized for a whole transcript's
worth of them too. Each provider states its own number; the port's ceiling is
deliberately nobody's, so one operation's measurement never becomes another's
allowance. The job those processes receive —
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
