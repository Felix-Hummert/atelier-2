# Interface status

An HTTP API now projects that durable state under `/atelier/api/v1`. It can
read the queue through one typed `GET /queue-items` projection across observed,
proposed, and admitted rows; revise a project's queue capacity policy with a
CAS-guarded `PUT /projects/{public_project_reference}/queue-policy`; write the
priority, workflow lineage, and prerequisites the operator will inspect through
`PUT /queue-proposals`; and confirm exactly that proposal through
`POST /queue-admissions`. The priority wire shape is `{"rank": n}`. Queue
responses use the contract's typed state, authority, automation disposition,
and blocker values. A tracker read failure never drops a durable queue row: the
resource instead reports `ENRICHMENT_UNAVAILABLE` and leaves its title absent.
It can
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
for a bounded loop, every run query selects the durable current round's exact
node execution, while the receipt list and event page retain every round and
stream preparation agrees with the page about the one terminal event;
an `invalid-request` names the field and reason the validator already knew;
answer a waiting node; cancel the current V2 Agent attempt with an optional
single replacement; cancel a V3 run through `POST /runs/{ref}/cancellations`,
which carries only the operator's opaque idempotency key and the node execution
its confirmation fenced on and answers the closed cancellation vocabulary
(accepted, terminal retry, overtaken by success, not cancellable, command
conflict); submit an accountable reconciliation; and follow the
closed durable event history as a resumable server-sent event stream. A
subscriber who does not already know a run holds `GET /events`; opening that
stream is the subscription. The cockpit holds that stream, so a Wait or an
agent failure appears without `POST /subscriptions`. The feed is closed to
`WAITING_INPUT`, `AGENT_FAILED`, and `ACTION_RECONCILIATION_REQUIRED` — the
events that stop a run until an operator acts — in the same envelope and
`VersionedRunEventResource` the per-run stream emits. A run whose projection cannot be served is named on this feed as `RUN_PROJECTION_CORRUPT` (`durable-state-corrupt`, `public_run_reference`) and does not end the subscription; that run's own event stream still ends with `STREAM_FAILED`. `Last-Event-ID` resumes by same-instant identity
exclusion: from that event1's instant T,
`recorded_at > T OR (recorded_at == T AND (run_id, seq) not among identities
already emitted at T)`. Last-Event-ID seeds the set with that cursor only; a
live holder adds each identity it emits and resets the set when the second
advances. Second-precision instants make two waits in one second the normal
case, so lexicographic `(recorded_at, run_id, seq) > cursor` is not the resume
rule. Pre-V22 events whose instant is NULL
stay off the feed rather than inventing a time. An operator who configures a
webhook target and a signing-key file receives the same attention events as
outbound HTTP POSTs, without a client holding
the stream: a background delivery loop signs each payload with HMAC-SHA256 over
its exact bytes, carries the event's `(run_id, event_sequence)` identity for the
receiver's own dedup, and advances its one durable cursor only after a 2xx — so
a receiver sees every attention event at least once, and a stuck receiver holds
the cursor on its event rather than skipping ahead. The signing key is read once
from its file at startup and never re-read; a webhook declared with only one of
the two settings fails the start rather than delivering to nowhere. A served V2
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

The stdio MCP `start_run` tool accepts artifact and work-item orders only;
inline orders remain an HTTP-only form until their retirement is a later slice.
`publish_artifact` accepts at most 1,047,552 Base64 characters, or 785,664
decoded bytes: the artifact store permits 1,048,576 bytes, but Base64 and the
JSON-RPC request envelope must fit the 1,048,576-byte MCP line cap (1,024 bytes
are reserved for that envelope). Publishing material and starting a run are two
calls. If the start is refused or fails, its already-published immutable
artifact remains reusable and no run exists.

[ADR 0003](../decisions/0003-http-api.md) owns the API and resume
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
Unnamed documents stay one row each, as they did. Publication and admission
remain two durable states, but `POST /library/additions` reaches both for a
workflow in one attributed act: the document bytes go in and the named library
entry comes out. A merely published revision remains outside the library; this
door removes the stranded intermediate step rather than merging the states.
Before that act, `POST /library/recognitions`
says what a loose document is without writing anything: opaque bytes plus an
optional `file_name`, answered as a recognized workflow (format, authored name
and description), a recognized agent definition (name, description, provider
mark), a kind the library recognises but does not hold yet (a `SKILL.md` with
a closed frontmatter block, or JSON with `mcpServers`, each with its reason), or
unrecognized with what every kind expected and why these bytes are not it. A
document two markers claim is refused naming both; the file name is a marker,
not a tie-breaker, so a `SKILL.md` whose frontmatter is a valid agent
definition is ambiguous and only a `SKILL.md` that is not a valid agent is a
skill. The document publishes the byte bound of the body and the character
bound of `file_name`. Recognition reuses the workflow and
agent-definition parsers publication already runs; no skill or MCP store
exists. Details repeats what the published graph already answers —
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
Output, never Asked or Answered. The run head is the one standing sentence;
the node's Result tab carries the decoded declared output with the Exact-text
fold — a declared object's own `answer` field as one sentence with its other
non-empty fields named after it, a declared array as its own items, an object
with no `answer` field as all of its fields, a bare string as itself — the
declared bytes kept behind a collapsed disclosure. The Who panel labels the receipt's model as
the declared configuration model and says a provider-resolved model is not
recorded — the same honest absence as usage. A hash leads with its human
name and is copied by a click on that named control — the hex is the proof
behind the name, not the reading title. The live event line names which node
finished and does not paste the output the node already holds. A STARTED run paints the working node
as live work, not as a finished card, and shows new events from the existing
SSE door as they arrive. Empty, connecting, and failed stream states are each
named as themselves. The process log is not on that door — it stays in the
lease (#104) — and the page says so rather than inventing a progress bar.
Node detail now serves the stored transcript of a finished attempt; the Log
tab that would render it is still not built. The
live event line stays open until the events it has applied match the latest
cursor the run itself names, so a run that has already ended still shows every
node that finished. Details on the
saved-workflow picker reuses the same drawing without run state. A chosen V3 revision that declares
orders shows one material field per order — the name and the schema the
author pinned — and sends the typed text as `orders` on the start; a revision
that declares none shows no field. Role
bindings on the Catalog detail's start sheet offer eligible registered
configurations by provider, exact model id, and readable Account. There is no
remembered role choice. For an admitted V3 workflow, that sheet reads the same
model-resolution door as every other start path. Each role is resolved by the
fixed order: a run-local override, an
exact workflow pin, the configured model default for its declared difficulty,
then a configured higher difficulty.
Missing or ambiguous pins and unknown overrides are terminal; they never fall
through to a default. A `family_differs_from` declaration is checked against
the final provider assignments, including overrides. Any roles left without an
assignment are returned together in one typed refusal naming the role, the
reason, and the family relation where that caused the refusal. The same
decision runs inside the canonical start transaction against one
host-configuration snapshot, so a run never combines registries and defaults
from different instants. The agent list is empty until a configuration is
published, and says so. The Workbench is the one workshop surface for
work that needs a person or is moving. Its stage is the notification surface;
the rail's ochre count is the notification count. Catalog, History, and
Settings complete the rail described by the blessed Mockup v8 and ADR 0019.
History's finished-run row names when it ran, the work item or an em dash,
and the result sentence — not only whether the run ended.
Settings shows the project's source connection as read-only provenance. It is
the one editing surface only for each provider's exact model registry and the
three difficulty defaults, in that order. The startable configuration list is
the owner of that provider-grouped rendering: a provider whose registry is
missing, or whose entry is not yet checked, still renders, marked unavailable,
with the Check action that publishes a missing registry if needed, then asks
the server to append its dry-run result.
Registry rows name the exact model
id, Account, provenance, and current provider check as separate facts; adding
or removing a row writes immediately. Only checked, startable registry entries
are selectable as defaults. Defaults are shown
as Difficulty 3, 2, and 1; selecting a model or clearing a row is an immediate
write that replaces only that difficulty. The other two saved rows are carried
byte-for-byte. A new choice is admitted when its provider, model, and
configuration are a checked, startable registry row; a carried row stays
admissible if that provider later stops reporting it. The retained model,
Account, and unavailable state wrap as one visible surface
until that row is changed or cleared. Neither operation renders a saving or saved caption, and an uncertain
write retries its identical bytes. Check is one operation; Retry of an
uncertain publish or validation resumes at that step and continues through
validation. Settings does not read or count runs; the
Workbench alone owns that live-work signal. The new-run trail names the project the same way the other
levels do. It can answer the exact integer requested by a Wait node and resolve an
unknown Action outcome as either an exact found effect or an accountable,
confirmed absence. For a V2 run it renders the node states the API names rather
than deriving them — the V2 event stream carries the rail with every event, so
nothing V2 is derived in the browser; the one named exception is the V1 half,
whose run resource is byte-frozen and which dies with the V3 cutover — and the
only state rule left in the browser is a client-owned interaction overlay that
lifts a node needing the operator while his form is open and stills it by that
open form alone. Its session-scoped mutation journal preserves exact retry
bytes without becoming a second durable truth. [ADR 0004](../decisions/0004-local-cockpit.md)
owns this browser boundary. The cockpit still provides no provider or platform
integration, authentication boundary, public deployment, or general-purpose
workflow editing. The graph, API, and local cockpit are a proven durable
vertical, not yet a general-purpose workflow engine or a deployed remote
product.
