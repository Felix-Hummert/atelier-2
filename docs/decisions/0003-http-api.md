# ADR 0003: The HTTP API projects durable workflow truth

## Context

The durable runtime can recover a workflow after process loss, but callers need
a stable control and observation boundary that preserves those guarantees. An
in-memory event broker, API-owned command state, or process-local retry answer
would create a second truth and make reconnect behavior depend on which server
process receives the request.

## Decision

FastAPI owns a thin, versioned adapter at `/atelier/api/v1`. The API publishes
secret-free auth-profile and agent-configuration revisions and exact safe-YAML
workflow bytes, starts runs from published revisions, projects revision and run
pages, accepts Wait answers, current-attempt cancellation commands, and
reconciliation commands, and streams the eleven implemented durable event
kinds. It does not accept credentials or own a parallel run, command, or event
state machine. Cancellation returns `202` while cleanup is pending and `200` for
an exact terminal retry. Stale, terminal, non-current, conflicting-command, and
forbidden-replacement requests are distinct closed problems.

V1 and V2 workflow, start, run, and SSE resources coexist as exact closed
unions. Workflow and run resources carry `format_version` or
`workflow_format_version`; start uses the closed shape itself to select the
version. A V2 run projection includes its immutable, public binding matrix. A V2
`AGENT_COMPLETED` event carries canonical Base64 plus the exact output hash so
arbitrary bytes never pass through UTF-8 decoding. The preexisting V1 raw JSON
and named OpenAPI components are byte-frozen; adding V2 does not silently widen
them.

Every mutation delegates to the runtime owner and decides created-versus-
existing from the row written in that same transaction. Only a newly created
command schedules continuation. Starting a run verifies its published revision
inside the start transaction. Reads use short-lived SQLite connections behind
separate bounded admission for ordinary control work and event-page polling.
Admission has its own injected wait deadline; refusal is a pre-header 503 for
control work and closes an already-started stream. Database timing has three
explicit owners rather than one misleading clock: the composed SQLAlchemy engine
bounds pool checkout, the query adapter bounds SQLite lock waiting, and its
progress deadline starts only after checkout. None is described as cancelling a
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

JSON resources and commands are closed typed models. Workflow publication is
the one raw `application/yaml` request. Centrally injected limits reject declared
oversize bodies before route parsing and stop undeclared or chunked bodies while
they are received; they also bound individual fields, encoded and decoded payloads,
workflow graphs, response projections, and concurrent query work. Durable
control-read projections outside those limits have their encoded workflow bytes
refused before YAML parsing and are refused before serialization as temporarily
unavailable; their durable rows are not changed.
After an SSE response has started, an invalid or oversized durable event closes
the stream without inventing an event. Errors are closed RFC 9457
`application/problem+json` variants. The generated OpenAPI 3.1 document is
built and validated eagerly during application construction and adds a
documented extension for the closed SSE `id`, `event`, and `data` contract.
Streaming uses FastAPI's public `EventSourceResponse` and `ServerSentEvent`
mechanisms.

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
