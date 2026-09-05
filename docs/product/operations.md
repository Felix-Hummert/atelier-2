# Operations and command-line status

A packaged container candidate now runs that same cockpit and catalog as one
provider-free Serve image. It builds only from a clean committed tree, runs
non-root with a read-only root, dropped capabilities and
`no-new-privileges`, and keeps durable state in one fresh project-scoped volume.
Each start chooses a Compose project and loopback port, labels its resources
with the exact source identity, builds from an archived committed snapshot,
waits for the matching health identity, and prints its exact shell-safe
teardown command from a private candidate-lifecycle descriptor, so later
checkout changes cannot redirect cleanup. It has no provider executable,
credential/configuration or host-home/scratch mount, Runner service, or
external execution claim. The
operator runbook is
[OPERATIONS.md](../OPERATIONS.md); network hardening remains
[ADR 0009](../decisions/0009-runner-trust.md).

One separate stable local installation now gives the operator that same
provider-free Serve console at exact loopback port 8422. Its private XDG state
first records durable installation intent, then binds the completed source,
engine, frozen Compose descriptor, image, container, volume, network and
configuration identities. Read-only status names running, stopped, incomplete
or drifted state; stop and start validate that record and address only its exact
container ID. Its volume survives both operations and its restart policy is
`unless-stopped`. Colliding listeners, host Atelier services, duplicate stable
Docker resources and identity drift are refusals, never adoption authority.
This slice can install, inspect, stop and start one fresh installation; it
cannot update, copy, migrate, preview, activate, roll back, accept, retire or
delete one. It remains current Core/V1 behavior without a provider or Runner,
while the disposable candidate remains the zero-residue release gate. The
operator contract and commands live in [OPERATIONS.md](../OPERATIONS.md).

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
`graph_inputs` that workflow declared: the command publishes every one of them
as a content-addressed artifact first, so `POST /runs` names the address the
artifact door answered instead of carrying bytes, and a ten-byte order takes
the same door as a hundred-kilobyte diff. Publishing the same bytes twice is
the same artifact, so a repeated command pays for nothing new. A surface that
lists or reads stored artifacts back is not built. A name the document never
declared, a declared name that is missing, and a value that is not valid JSON
for the schema the document pinned are each refused by name; a typed 422 from
the service is handed on in the service's own words. An output contract that
could decide an exit code still does not exist.

That API now also has a third door: `atelier2 mcp` speaks MCP as one
JSON-RPC object per line on standard input and standard output against the
same public HTTP API. A client launches it as a child. There is no listener,
no port and no token. The five tools are `list_workflows` (catalog name,
lineage and head), `start_run` (the revision a name holds, the same resolution
`run --name` asks, with artifact and work-item orders only; inline orders remain
an HTTP-only form until their retirement is a later slice), `run_status` (the
run resource as the API answers it), `answer_wait` (the #194 door) and
`publish_artifact` (`POST /artifacts` as octet-stream; MCP JSON carries those
bytes as standard Base64 because it cannot speak octet-stream). Each call is
the HTTP door; a typed problem is returned unchanged, field pointers included.
`publish_artifact` accepts at most 1,047,552 Base64 characters, or 785,664
decoded bytes, derived from `min(artifact bound, (1 MiB JSON-RPC line cap − 1
KiB envelope) × 3/4)`. Publishing material and starting a run are two calls;
if the start is refused or fails, its already-published immutable artifact
remains reusable and no run exists. The API has no caller authentication: #82
is human OIDC and ADR 0009 (machine credentials) is not landed, so this child
invents none and refuses any service that is not a literal loopback address —
the same trust the browser already has on this machine. Instants on the run
wait for #355; this door does not invent them.
