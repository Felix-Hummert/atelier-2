# ADR 0003: The HTTP API projects durable workflow truth

- Status: ACCEPTED 2026-08-11 — implemented: the projecting API landed with this
  record and serves under `/atelier/api/v1`

## Context

The durable runtime can recover a workflow after process loss, but callers need
a stable control and observation boundary that preserves those guarantees. An
in-memory event broker, API-owned command state, or process-local retry answer
would create a second truth and make reconnect behavior depend on which server
process receives the request.

## Decision

FastAPI owns a thin, versioned adapter at `/atelier/api/v1`. The API publishes
secret-free auth-profile and agent-configuration revisions, exact safe-YAML
workflow bytes, and exact JSON Schema bytes, starts runs from published
revisions, projects revision and run pages, accepts Wait answers,
current-attempt cancellation commands, and reconciliation commands, and streams
the eleven implemented durable event kinds. It does not accept credentials or own a parallel run, command, or event
state machine. Cancellation returns `202` while cleanup is pending and `200` for
an exact terminal retry. Stale, terminal, non-current, conflicting-command, and
forbidden-replacement requests are distinct closed problems.

A value that crosses from an answer into the next request is named for what it
identifies, not for where it stands: a workflow's revision hash is
`workflow_revision_hash` and its format version `workflow_format_version` on
every body that carries them, a published schema or budget revision names its
own kind, and an artifact travels as `artifact_hash` from the publication that
minted it into the order that names it. No published body carries a bare
`revision_hash` or `format_version`, and no path parameter is named
`revision_hash`; only the grammar of the authored workflow document keeps the
bare words, because inside a document each means one thing. A declared order
answers the author's own `schema: {ref, revision}` hull rather than flattening
it under other names. Moving the
revision and catalog resources onto that language was a second explicit breaking
pre-release migration, taken for the same reason as the SSE envelope below and
under the same condition: no external consumer exists, and the cockpit, the
command, the MCP door and the frozen document migrate together.

V1, V2, and V3 SSE event resources coexist as exact closed unions. V1 and V2
workflow, start, and run resources remain their own closed families.
Start uses the closed shape itself to select the version. A V2 run projection includes
its immutable, public binding matrix. A V2 or V3 `AGENT_COMPLETED` event carries
canonical Base64 plus the exact output hash so arbitrary bytes never pass
through UTF-8 decoding. The preexisting V1 raw JSON and named OpenAPI
components are byte-frozen; adding V2 or V3 does not silently widen them. The
V3 event family publishes only the agent kinds a format-3 line actually writes.
The SSE envelope carries only `id` and `data`: omitting the transport
`event:` field makes every frame a default `message`, while `data.event` is the
sole domain discriminant. The stream has exactly two frame shapes under that
one envelope: a durable event, which carries its cursor as `id`, and the
terminal failure frame `STREAM_FAILED`, which carries a problem body and no
`id`, because a resume cursor on a refusal would invite the browser to
reconnect into the same refusal forever.

Every mutation delegates to the runtime owner and decides created-versus-
existing from the row written in that same transaction. Only a newly created
command schedules continuation. Starting a run verifies its published revision
inside the start transaction. Reads use short-lived SQLite connections behind
separate bounded admission for ordinary control work and event-page polling.
Admission has its own injected wait deadline; refusal is a pre-header 503 for
control work and ends an already-started stream regularly. Database timing has
three explicit owners rather than one misleading clock: the composed SQLAlchemy
engine bounds pool checkout, the query adapter bounds SQLite lock waiting, and
its progress deadline starts only after checkout. None is described as cancelling a
running thread. An idle stream backs off deterministically to a configured
ceiling and resets that delay after progress. Cancellation keeps the applicable
bound occupied until the underlying blocking durable call has actually returned.

Run references are canonical `run1` encodings of the domain's UTF-8 run ID.
Event cursors canonically bind that reference to a positive durable sequence as
`event1`. Run pagination uses SQLite's `BINARY` ordering for `TEXT`, then refuses
the projection if that observed order or boundary disagrees with the exact UTF-8
byte order required by the API. `Last-Event-ID` is an exclusive acknowledgement:
the stream begins with the next durable event. Reusing an older cursor
intentionally replays unacknowledged events, and reconnecting to a new process
reads the same history from the durable store. A terminal stream ends only
after its durable tail has been delivered.

JSON resources and commands are closed typed models. Publications of exact
bytes rather than a typed model are workflow (`application/yaml`) and the
JSON documents whose hash is of those exact bytes: schema, budget, and
tool-grant (`application/json`). Taking bytes does not mean saying
nothing about them: the workflow publication body carries the shape of the
document, derived from the same models the publication reads it against rather
than written a second time. The shape decides the form; whether a named edge
resolves, whether a cycle closes and whether this build executes the result are
statements about the whole document and keep their named refusals. A refused
path names the location of the OpenAPI document, so a consumer holding only a
base URL reaches all of this without guessing. Each schema-profile refusal the
store already names is a distinct closed problem, not a generic invalid
request. Centrally injected limits reject declared
oversize bodies before route parsing and stop undeclared or chunked bodies while
they are received; they also bound individual fields, encoded and decoded payloads,
workflow graphs, response projections, and concurrent query work. Durable
control-read projections outside those limits have their encoded workflow bytes
refused before YAML parsing and are refused before serialization as temporarily
unavailable; their durable rows are not changed.
After an SSE response has started, the same failures the REST surface names are
named in the stream: a corrupt or unprojectable durable row, a durable row
outside the configured projection limits, and a port that breaks its page
contract each end the stream with a terminal failure frame carrying the problem
body that surface would have answered. Only backpressure and transient store
unavailability end the stream regularly, because a client's own reconnect is
the correct answer to both, and a client that reconnects into a permanent
refusal would never be told. Errors are closed RFC 9457
`application/problem+json` variants on both paths. The generated OpenAPI 3.1
document is built and validated eagerly during application construction and adds
a documented extension naming both SSE frame shapes. The published failure frame
promises exactly the problems the stream can speak, not the open problem shape,
so it never offers a consumer a body the closed vocabulary would refuse.
Streaming uses FastAPI's public `EventSourceResponse` and `ServerSentEvent`
mechanisms.

This default-message envelope is an explicit breaking pre-release migration
from named SSE frames. Atelier 2 had no external consumer at the decision point;
the sole local cockpit and tests migrate together. A cockpit tab left open
across that deploy must reload. The durable payload, cursor, native reconnect,
and `Last-Event-ID` semantics do not change.

## Consequences

- HTTP retries and server restarts preserve the runtime's durable idempotency;
  the API adds no recovery log or broker.
- Stream delivery is replayable rather than exactly-once. Clients acknowledge
  progress by reconnecting with the last cursor they have durably consumed.
- Adding a new public event or problem kind changes a closed contract and must
  be treated as an API-version decision.
- Auth-profile and agent-configuration publication stores selection metadata,
  not credentials; credential lookup remains a future host/provider concern.
- This boundary supplies no authentication, browser CORS policy, provider or
  platform integration, cockpit, process supervision, or deployment. A host
  must supply any required access boundary before exposing it beyond a trusted
  environment.

## Supersedes

None.
