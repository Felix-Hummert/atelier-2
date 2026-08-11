# ADR 0003: The HTTP API projects durable workflow truth

## Context

The durable runtime can recover a workflow after process loss, but callers need
a stable control and observation boundary that preserves those guarantees. An
in-memory event broker, API-owned command state, or process-local retry answer
would create a second truth and make reconnect behavior depend on which server
process receives the request.

## Decision

FastAPI owns a thin, versioned adapter at `/atelier/api/v1`. The API publishes
exact safe-YAML workflow bytes, starts runs from published revisions, projects
revision and run pages, accepts Wait answers and reconciliation commands, and
streams the seven implemented durable event kinds. It does not own a parallel
run, command, or event state machine.

Every mutation delegates to the runtime owner and decides created-versus-
existing from the row written in that same transaction. Only a newly created
command schedules continuation. Starting a run verifies its published revision
inside the start transaction. Reads use short-lived SQLite connections behind a
shared concurrency bound and a per-query database deadline; cancellation keeps
the bound occupied until the underlying blocking durable call has actually
returned.

Run references are canonical `run1` encodings of the domain's UTF-8 run ID.
Event cursors canonically bind that reference to a positive durable sequence as
`event1`. Run pagination compares the stored UTF-8 bytes, so its order is stable
across SQLite text collations. `Last-Event-ID` is an exclusive acknowledgement:
the stream begins with the next durable event. Reusing an older cursor
intentionally replays unacknowledged events, and reconnecting to a new process
reads the same history from the durable store. A terminal stream ends only
after its durable tail has been delivered.

JSON resources and commands are closed typed models. Workflow publication is
the one raw `application/yaml` request. Errors are closed RFC 9457
`application/problem+json` variants. The generated OpenAPI 3.1 document is
validated at construction and adds a documented extension for the closed SSE
`id`, `event`, and `data` contract. Streaming uses FastAPI's public
`EventSourceResponse` and `ServerSentEvent` mechanisms.

## Consequences

- HTTP retries and server restarts preserve the runtime's durable idempotency;
  the API adds no recovery log or broker.
- Stream delivery is replayable rather than exactly-once. Clients acknowledge
  progress by reconnecting with the last cursor they have durably consumed.
- Adding a new public event or problem kind changes a closed contract and must
  be treated as an API-version decision.
- This boundary supplies no authentication, browser CORS policy, provider or
  platform integration, cockpit, process supervision, or deployment. A host
  must supply any required access boundary before exposing it beyond a trusted
  environment.

## Supersedes

None.
