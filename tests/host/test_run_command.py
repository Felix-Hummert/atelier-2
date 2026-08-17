"""What `atelier2 run` does against a service that answers the published API.

The service here is a real HTTP server speaking the product's own resources, so
these tests pin the command's conversation and its operator-visible answer, not
the shape of an internal call.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Literal, Self

import pytest

from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import problem_resource
from atelier2.api.references import encode_canonical_base64
from atelier2.api.wire.events import (
    AgentCompletedEventResource,
    AgentCompletedEventResourceV2,
    AgentFailedEventResourceV2,
    WaitingInputEventResourceV2,
)
from atelier2.api.wire.resources import (
    AgentConfigurationRevisionResource,
    AgentNodeResourceV2,
    AuthProfileRevisionResource,
    NodeRailResource,
    NodeStateName,
    NoWaitingResource,
    NoWaitingResourceV2,
    ProblemResource,
    RunResource,
    RunResourceV2,
    StreamFailureResource,
    SubworkflowNodeResource,
    WorkflowGraphResourceV2,
    WorkflowRevisionDetailResource,
)
from atelier2.contracts.run_projections import NodeState
from atelier2.host import main
from atelier2.host.run_command import (
    AGENT_CONFIGURATION_PATH,
    AUTH_PROFILE_PATH,
    JSON_MEDIA_TYPE,
    RUN_PATH,
    WORKFLOW_REVISION_PATH,
    AgentRoleBinding,
    NameOrder,
    ServiceRefused,
    derived_run_id,
    describe_resolution,
    resolve_published_name,
)

PROBLEM_MEDIA_TYPE = "application/problem+json"
EVENT_STREAM_MEDIA_TYPE = "text/event-stream"

AUTH_PROFILE_HASH = "a" * 64
AGENT_CONFIGURATION_HASH = "b" * 64
REVISION_HASH = "c" * 64
TERMINAL_HASH = "d" * 64
OUTPUT_HASH = "e" * 64
NODE_EXECUTION_ID = "f" * 64
EVENT_HASH = "1" * 64
ATTEMPT_ID = "2" * 64
BINDING_SET_HASH = "3" * 64

PUBLIC_RUN_REFERENCE = "run1.dGVzdA"
EVENT_CURSOR = f"event1.dGVzdA.{1}"
LATER_EVENT_CURSOR = f"event1.dGVzdA.{2}"
STREAM_FAILURE_CODE = "durable-state-corrupt"
AGENT_ROLE = "writer"
AGENT_NODE_ID = "draft"
TERMINAL_NODE_ID = "total"
AGENT_OUTPUT = b"the answer the run produced"

WORKFLOW_DOCUMENT = b"""format_version: 2
start: draft
nodes:
  - {id: total, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: draft, type: agent, role: writer, job: say-something, next: total}
"""
BINDING_DOCUMENT = json.dumps(
    {
        "auth_profile": {
            "profile_id": "personal",
            "revision_number": 1,
            "provider_id": "claude",
            "auth_mode": "subscription",
        },
        "model": "claude-opus-4",
        "executor_revision": "claude-subscription-v1",
    }
).encode()

RUNS_URL_PATH = API_PREFIX + RUN_PATH
RUN_URL_PATH = f"{RUNS_URL_PATH}/{PUBLIC_RUN_REFERENCE}"
EVENTS_URL_PATH = f"{RUN_URL_PATH}/events"


@dataclass(frozen=True)
class Answer:
    body: bytes
    status: HTTPStatus = HTTPStatus.OK
    media_type: str = JSON_MEDIA_TYPE


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    body: bytes


@dataclass
class ScriptedService:
    """One real HTTP server answering the routes this command uses."""

    answers: dict[tuple[str, str], list[Answer]]
    calls: list[Call] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None

    def __enter__(self) -> Self:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._answer("GET")

            def do_POST(self) -> None:
                self._answer("POST")

            def _answer(self, method: str) -> None:
                length = int(self.headers.get("content-length", "0"))
                service.calls.append(Call(method, self.path, self.rfile.read(length)))
                scripted = service.answers.get((method, self.path))
                answer = (
                    unrouted_answer()
                    if not scripted
                    else (scripted.pop(0) if len(scripted) > 1 else scripted[0])
                )
                self.send_response(answer.status)
                self.send_header("content-type", answer.media_type)
                self.send_header("content-length", str(len(answer.body)))
                self.end_headers()
                self.wfile.write(answer.body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exception: object) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def sent(self, method: str, path: str) -> list[bytes]:
        return [
            call.body
            for call in self.calls
            if (call.method, call.path) == (method, path)
        ]


def unrouted_answer() -> Answer:
    return problem_answer(
        HTTPStatus.NOT_FOUND, "not-found", "Not Found", "no such resource"
    )


def problem_answer(status: HTTPStatus, kind: str, title: str, detail: str) -> Answer:
    problem = ProblemResource(type=kind, title=title, status=int(status), detail=detail)
    return Answer(
        problem.model_dump_json().encode(), status=status, media_type=PROBLEM_MEDIA_TYPE
    )


def published_auth_profile() -> Answer:
    return Answer(
        AuthProfileRevisionResource(
            profile_id="personal",
            revision_number=1,
            provider_id="claude",
            auth_mode="subscription",
            auth_profile_revision_hash=AUTH_PROFILE_HASH,
        )
        .model_dump_json()
        .encode()
    )


def published_agent_configuration() -> Answer:
    return Answer(
        AgentConfigurationRevisionResource(
            model="claude-opus-4",
            auth_profile_revision_hash=AUTH_PROFILE_HASH,
            executor_revision="claude-subscription-v1",
            provider_id="claude",
            auth_mode="subscription",
            requested_capability="headless",
            agent_configuration_revision_hash=AGENT_CONFIGURATION_HASH,
        )
        .model_dump_json()
        .encode()
    )


def published_workflow_revision() -> Answer:
    return Answer(
        WorkflowRevisionDetailResource(
            revision_hash=REVISION_HASH,
            document_base64=encode_canonical_base64(WORKFLOW_DOCUMENT),
            graph=WorkflowGraphResourceV2(
                format_version=2,
                start_node_id=AGENT_NODE_ID,
                nodes=(
                    AgentNodeResourceV2(
                        type="agent",
                        node_id=AGENT_NODE_ID,
                        role=AGENT_ROLE,
                        job="say-something",
                        next_node_id=TERMINAL_NODE_ID,
                    ),
                    terminal_node(),
                ),
            ),
        )
        .model_dump_json()
        .encode()
    )


def terminal_node() -> SubworkflowNodeResource:
    return SubworkflowNodeResource(
        type="subworkflow",
        node_id=TERMINAL_NODE_ID,
        operation="add",
        operands=(2, 3),
        next_node_id=None,
    )


def node_rail(terminal_state: NodeStateName) -> tuple[NodeRailResource, ...]:
    """The rail the service answers with, on the two nodes this command walks."""
    return (
        NodeRailResource(
            node_id=AGENT_NODE_ID, state=NodeState.SUCCEEDED, attempt=None
        ),
        NodeRailResource(node_id=TERMINAL_NODE_ID, state=terminal_state, attempt=None),
    )


def run_resource(
    state: Literal["STARTED", "COMPLETED"],
    terminal_hash: str | None,
    latest_event_cursor: str = EVENT_CURSOR,
) -> RunResourceV2:
    return RunResourceV2(
        workflow_format_version=2,
        run_id="unread-by-the-command",
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        agent_binding_set_hash=BINDING_SET_HASH,
        agent_bindings=(),
        state_version=2,
        state=state,
        current_node=terminal_node(),
        node_rail=node_rail(
            NodeState.SUCCEEDED if state == "COMPLETED" else NodeState.WORKING
        ),
        agent_attempts=(),
        waiting=NoWaitingResourceV2(type="NONE"),
        terminal_hash=terminal_hash,
        latest_event_cursor=latest_event_cursor,
    )


def started_run() -> Answer:
    return Answer(run_resource("STARTED", None).model_dump_json().encode())


def completed_run(latest_event_cursor: str = EVENT_CURSOR) -> Answer:
    return Answer(
        run_resource("COMPLETED", TERMINAL_HASH, latest_event_cursor)
        .model_dump_json()
        .encode()
    )


def unbound_run_resource(
    state: Literal["STARTED", "COMPLETED"], terminal_hash: str | None
) -> RunResource:
    """A run of a workflow that binds no agent: the version-1 shape of the same run."""

    return RunResource(
        run_id="unread-by-the-command",
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        state_version=2,
        state=state,
        current_node=terminal_node(),
        waiting=NoWaitingResource(type="NONE"),
        terminal_hash=terminal_hash,
        latest_event_cursor=EVENT_CURSOR,
    )


def event_stream(*events: str, failure: str | None = None) -> Answer:
    """The frames exactly as the served API writes them: data, then id.

    A failure frame ends the stream and carries no id, because the route offers
    no resume cursor into its own refusal.
    """

    frames = "".join(f"data: {payload}\nid: {EVENT_CURSOR}\n\n" for payload in events)
    if failure is not None:
        frames += f"data: {failure}\n\n"
    return Answer(frames.encode(), media_type=EVENT_STREAM_MEDIA_TYPE)


def stream_failure(code: str = STREAM_FAILURE_CODE) -> str:
    """The frame the route writes when the stream itself fails, in its own words."""

    return StreamFailureResource(problem=problem_resource(code)).model_dump_json()


def agent_completed() -> str:
    return AgentCompletedEventResourceV2(
        workflow_format_version=2,
        node_rail=node_rail(NodeState.WORKING),
        cursor=EVENT_CURSOR,
        sequence=1,
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        node_id=AGENT_NODE_ID,
        node_execution_id=NODE_EXECUTION_ID,
        event_hash=EVENT_HASH,
        event="AGENT_COMPLETED",
        output_base64=encode_canonical_base64(AGENT_OUTPUT),
        output_hash=OUTPUT_HASH,
        attempt_id=ATTEMPT_ID,
        attempt_ordinal=1,
    ).model_dump_json()


def unbound_agent_completed() -> str:
    """The version-1 event: the output travels as text, and no attempt names it."""

    return AgentCompletedEventResource(
        cursor=EVENT_CURSOR,
        sequence=1,
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        node_id=AGENT_NODE_ID,
        node_execution_id=NODE_EXECUTION_ID,
        event_hash=EVENT_HASH,
        event="AGENT_COMPLETED",
        output=AGENT_OUTPUT.decode(),
        payload_hash=OUTPUT_HASH,
    ).model_dump_json()


def agent_failed() -> str:
    return AgentFailedEventResourceV2(
        workflow_format_version=2,
        node_rail=node_rail(NodeState.WORKING),
        cursor=EVENT_CURSOR,
        sequence=1,
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        node_id=AGENT_NODE_ID,
        node_execution_id=NODE_EXECUTION_ID,
        event_hash=EVENT_HASH,
        event="AGENT_FAILED",
        failure_code="PROCESS_EXITED_UNSUCCESSFULLY",
        attempt_id=ATTEMPT_ID,
        attempt_ordinal=1,
    ).model_dump_json()


def waiting_for_input() -> str:
    return WaitingInputEventResourceV2(
        workflow_format_version=2,
        node_rail=node_rail(NodeState.WORKING),
        cursor=EVENT_CURSOR,
        sequence=1,
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        node_id="approval",
        node_execution_id=NODE_EXECUTION_ID,
        event_hash=EVENT_HASH,
        event="WAITING_INPUT",
        answer_type="integer",
    ).model_dump_json()


def serving_answers(
    **replacements: Answer,
) -> dict[tuple[str, str], list[Answer]]:
    """The whole conversation of one run that ends, with named replacements."""

    scripted = {
        "auth_profile": (
            "POST",
            API_PREFIX + AUTH_PROFILE_PATH,
            published_auth_profile(),
        ),
        "agent_configuration": (
            "POST",
            API_PREFIX + AGENT_CONFIGURATION_PATH,
            published_agent_configuration(),
        ),
        "workflow_revision": (
            "POST",
            API_PREFIX + WORKFLOW_REVISION_PATH,
            published_workflow_revision(),
        ),
        "start": ("POST", RUNS_URL_PATH, started_run()),
        "events": ("GET", EVENTS_URL_PATH, event_stream(agent_completed())),
        "run": ("GET", RUN_URL_PATH, completed_run()),
    }
    return {
        (method, path): [replacements.get(name, answer)]
        for name, (method, path, answer) in scripted.items()
    }


def unbound_serving_answers() -> dict[tuple[str, str], list[Answer]]:
    """The same conversation for a workflow that binds no agent."""

    return serving_answers(
        start=Answer(unbound_run_resource("STARTED", None).model_dump_json().encode()),
        events=event_stream(unbound_agent_completed()),
        run=Answer(
            unbound_run_resource("COMPLETED", TERMINAL_HASH).model_dump_json().encode()
        ),
    )


@pytest.fixture
def order(tmp_path: Path) -> Iterator[list[str]]:
    workflow = tmp_path / "workflow.yaml"
    workflow.write_bytes(WORKFLOW_DOCUMENT)
    binding = tmp_path / "writer.json"
    binding.write_bytes(BINDING_DOCUMENT)
    yield ["run", "--workflow", str(workflow), "--binding", f"{AGENT_ROLE}={binding}"]


@pytest.fixture
def unbound_order(tmp_path: Path) -> Iterator[list[str]]:
    workflow = tmp_path / "workflow.yaml"
    workflow.write_bytes(WORKFLOW_DOCUMENT)
    yield ["run", "--workflow", str(workflow)]


def run_command(order: list[str], service: ScriptedService, *extra: str) -> int:
    return main([*order, "--service", service.url, *extra])


def test_the_output_of_a_run_that_ended_is_printed_with_what_binds_it_to_that_run(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers()) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (0, AGENT_OUTPUT)
    reported = printed.err.decode()
    assert PUBLIC_RUN_REFERENCE in reported
    assert TERMINAL_HASH in reported
    assert OUTPUT_HASH in reported
    assert ATTEMPT_ID in reported


def test_an_event_kind_this_command_knows_nothing_about_is_passed_over(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """A history may carry kinds this client predates; only ours decide."""

    unknown = json.dumps({"event": "SOMETHING_LATER", "sequence": 1})
    with ScriptedService(
        serving_answers(events=event_stream(unknown, agent_completed()))
    ) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (0, AGENT_OUTPUT)


def test_the_started_run_binds_the_hashes_the_service_answered_with(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers()) as service:
        run_command(order, service)
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])

    assert started == {
        "workflow_format_version": 2,
        "run_id": derived_run_id(
            REVISION_HASH, (AgentRoleBinding(AGENT_ROLE, AGENT_CONFIGURATION_HASH),)
        ),
        "workflow_revision_hash": REVISION_HASH,
        "agent_bindings": [
            {
                "role": AGENT_ROLE,
                "agent_configuration_revision_hash": AGENT_CONFIGURATION_HASH,
            }
        ],
    }


def test_a_workflow_that_binds_no_agent_starts_without_publishing_one(
    unbound_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(unbound_serving_answers()) as service:
        exit_code = run_command(unbound_order, service)
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])
        published_agents = service.sent("POST", API_PREFIX + AUTH_PROFILE_PATH)

    assert (exit_code, published_agents) == (0, [])
    assert started == {
        "run_id": derived_run_id(REVISION_HASH, ()),
        "workflow_revision_hash": REVISION_HASH,
    }


def test_the_output_of_a_workflow_that_binds_no_agent_is_printed_as_it_was_written(
    unbound_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(unbound_serving_answers()) as service:
        exit_code = run_command(unbound_order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (0, AGENT_OUTPUT)
    reported = printed.err.decode()
    assert OUTPUT_HASH in reported
    assert "attempt" not in reported


def test_the_same_command_twice_asks_for_the_same_run(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers()) as service:
        first = run_command(order, service)
        second = run_command(order, service)
        started = service.sent("POST", RUNS_URL_PATH)

    printed = capsysbinary.readouterr()
    assert (first, second) == (0, 0)
    assert printed.out == AGENT_OUTPUT + AGENT_OUTPUT
    assert started[0] == started[1]


def test_a_named_run_identity_is_the_one_asked_for(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers()) as service:
        run_command(order, service, "--run-id", "the-operators-own-identity")
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])

    assert started["run_id"] == "the-operators-own-identity"


def test_a_failed_agent_attempt_ends_the_command_unsuccessfully(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(
        serving_answers(events=event_stream(agent_failed()))
    ) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (1, b"")
    assert b"PROCESS_EXITED_UNSUCCESSFULLY" in printed.err
    assert ATTEMPT_ID.encode() in printed.err


def test_a_run_waiting_for_input_says_which_capability_is_missing(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(
        serving_answers(events=event_stream(waiting_for_input()))
    ) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert exit_code == 1
    assert b"#38" in printed.err


def test_an_event_history_that_ends_before_the_run_does_is_refused(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers(run=started_run())) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (1, b"")
    assert b"STARTED" in printed.err


def test_a_stream_that_fails_hands_the_services_own_problem_to_the_operator(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """The stream's only problem channel is a frame, and it must not be dropped."""

    with ScriptedService(
        serving_answers(events=event_stream(failure=stream_failure()))
    ) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (1, b"")
    problem = problem_resource(STREAM_FAILURE_CODE)
    reported = printed.err.decode()
    assert problem.type in reported
    assert problem.title in reported
    assert problem.detail in reported
    assert TERMINAL_HASH not in reported


def test_a_stream_that_ends_without_an_event_is_refused_rather_than_reported_as_none(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """Backpressure ends the stream regularly; a completed run is no proof of it."""

    with ScriptedService(serving_answers(events=event_stream())) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (1, b"")
    assert EVENT_CURSOR.encode() in printed.err
    assert TERMINAL_HASH.encode() not in printed.err


def test_a_history_that_stops_before_the_runs_latest_event_prints_no_half_output(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(
        serving_answers(run=completed_run(latest_event_cursor=LATER_EVENT_CURSOR))
    ) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (1, b"")
    assert LATER_EVENT_CURSOR.encode() in printed.err
    assert TERMINAL_HASH.encode() not in printed.err


def test_a_typed_problem_reaches_the_operator_as_the_service_wrote_it(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    refusal = problem_answer(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "https://atelier/problems/invalid-workflow-document",
        "Invalid workflow document",
        "node draft names an unreachable successor",
    )
    with ScriptedService(serving_answers(workflow_revision=refusal)) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert exit_code == 1
    assert b"https://atelier/problems/invalid-workflow-document" in printed.err
    assert b"node draft names an unreachable successor" in printed.err


def test_an_answer_that_is_not_the_published_contract_is_refused_by_name(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers(start=Answer(b'{"run_id": 17}'))) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert exit_code == 1
    assert b"cannot read" in printed.err


def test_a_binding_file_that_describes_no_agent_is_refused_before_anything_runs(
    tmp_path: Path, order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    (tmp_path / "writer.json").write_bytes(b'{"model": "claude-opus-4"}')

    with ScriptedService(serving_answers()) as service:
        exit_code = run_command(order, service)
        started = service.sent("POST", RUNS_URL_PATH)

    printed = capsysbinary.readouterr()
    assert (exit_code, started) == (1, [])
    assert b"writer" in printed.err


def test_no_service_at_the_named_address_is_named_instead_of_traced(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers()) as service:
        unserved = service.url
    exit_code = main([*order, "--service", unserved])

    printed = capsysbinary.readouterr()
    assert exit_code == 1
    assert unserved.encode() in printed.err


def test_an_address_that_is_not_a_served_api_is_refused(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    exit_code = main([*order, "--service", "file:///etc/passwd"])

    printed = capsysbinary.readouterr()
    assert exit_code == 1
    assert b"file:///etc/passwd" in printed.err


NAME = "review-bounded-diff"
LINEAGE_ID = "b" * 64
REVISION_NUMBER = 2
BY_NAME_URL_PATH = f"{API_PREFIX}{WORKFLOW_REVISION_PATH}/by-name/{NAME}"


def name_answer() -> Answer:
    return Answer(
        json.dumps(
            {
                "display_name": NAME,
                "lineage_id": LINEAGE_ID,
                "revision_hash": REVISION_HASH,
                "revision_number": REVISION_NUMBER,
            }
        ).encode()
    )


@pytest.mark.proves("one-command-answers-what-a-name-holds")
def test_a_name_is_resolved_through_the_service_and_shown_with_what_binds_it() -> None:
    with ScriptedService({("GET", BY_NAME_URL_PATH): [name_answer()]}) as service:
        resolution = resolve_published_name(NameOrder(service.url, NAME))

    assert resolution.revision_hash == REVISION_HASH
    assert resolution.revision_number == 2
    shown = describe_resolution(resolution)
    assert NAME in shown
    assert REVISION_HASH in shown
    assert "2" in shown


@pytest.mark.proves("one-command-answers-what-a-name-holds")
def test_resolving_a_name_starts_nothing() -> None:
    with ScriptedService({("GET", BY_NAME_URL_PATH): [name_answer()]}) as service:
        resolve_published_name(NameOrder(service.url, NAME))

        assert service.sent("POST", RUNS_URL_PATH) == []
        assert [call.method for call in service.calls] == ["GET"]


def test_a_position_is_asked_of_the_service_rather_than_chosen_here() -> None:
    path = f"{BY_NAME_URL_PATH}?position=1"
    with ScriptedService({("GET", path): [name_answer()]}) as service:
        resolve_published_name(NameOrder(service.url, NAME, position="1"))

        assert [call.path for call in service.calls] == [path]


@pytest.mark.proves("a-refused-name-ends-the-command-unsuccessfully")
def test_a_name_the_service_refuses_is_handed_on_in_its_own_words() -> None:
    refused = problem_answer(
        HTTPStatus.NOT_FOUND,
        "urn:atelier2:problem:v1:catalog-name-not-found",
        "Catalog name not found",
        "No lineage of this kind holds that name at that position.",
    )
    with (
        ScriptedService({("GET", BY_NAME_URL_PATH): [refused]}) as service,
        pytest.raises(ServiceRefused) as refusal,
    ):
        resolve_published_name(NameOrder(service.url, NAME))

    assert "catalog-name-not-found" in str(refusal.value)
    assert "No lineage of this kind holds that name" in str(refusal.value)


@pytest.mark.proves("one-command-answers-what-a-name-holds")
def test_the_command_shows_what_the_name_holds_and_ends_successfully(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with ScriptedService({("GET", BY_NAME_URL_PATH): [name_answer()]}) as service:
        exit_code = main(["resolve", "--name", NAME, "--service", service.url])

    shown = capsys.readouterr()
    # One line, and every value in it distinct: a name, a lineage id, a member
    # number and a revision hash that cannot stand in for one another. Asserting
    # the whole line is what makes a swapped or dropped field fail here rather
    # than read plausibly to an operator.
    assert exit_code == 0
    assert shown.out == (
        f"{NAME} is revision {REVISION_NUMBER} "
        f"of lineage {LINEAGE_ID}: {REVISION_HASH}\n"
    )
    assert shown.err == ""


@pytest.mark.proves("a-refused-name-ends-the-command-unsuccessfully")
def test_a_refused_name_ends_the_command_unsuccessfully(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refused = problem_answer(
        HTTPStatus.GONE,
        "urn:atelier2:problem:v1:catalog-lineage-retired",
        "Catalog lineage retired",
        "This name was retired; it resolves to no revision a run may use.",
    )
    with ScriptedService({("GET", BY_NAME_URL_PATH): [refused]}) as service:
        exit_code = main(["resolve", "--name", NAME, "--service", service.url])

    shown = capsys.readouterr()
    assert exit_code == 1
    assert shown.out == ""
    assert "catalog-lineage-retired" in shown.err


def named_serving_answers() -> dict[tuple[str, str], list[Answer]]:
    """The conversation of a run started by name: resolve, then run.

    No workflow revision is published, because the name already holds one. The
    absence of that answer is what proves it: a command that still published
    would meet a service with nothing scripted for it.
    """

    answers = serving_answers()
    del answers[("POST", API_PREFIX + WORKFLOW_REVISION_PATH)]
    answers[("GET", BY_NAME_URL_PATH)] = [name_answer()]
    return answers


@pytest.fixture
def named_order(tmp_path: Path) -> Iterator[list[str]]:
    binding = tmp_path / "writer.json"
    binding.write_bytes(BINDING_DOCUMENT)
    yield ["run", "--name", NAME, "--binding", f"{AGENT_ROLE}={binding}"]


@pytest.mark.proves("one-command-runs-the-workflow-a-name-holds")
def test_a_named_workflow_runs_and_prints_the_output_of_the_run_it_started(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """The command #111 exists for: a name in, the run's own output out.

    Until the runtime could execute a published revision end to end, a `run` that
    resolved a name would have been a verb that lies. It starts now, so the
    resolution and the run are one command rather than two and a copied hash.
    """
    with ScriptedService(named_serving_answers()) as service:
        exit_code = run_command(named_order, service)
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])
        published_workflows = service.sent("POST", API_PREFIX + WORKFLOW_REVISION_PATH)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (0, AGENT_OUTPUT)
    assert published_workflows == []
    assert started["workflow_revision_hash"] == REVISION_HASH
    reported = printed.err.decode()
    assert PUBLIC_RUN_REFERENCE in reported
    assert TERMINAL_HASH in reported
    assert NAME in reported


@pytest.mark.proves("one-command-runs-the-workflow-a-name-holds")
def test_a_named_run_asks_the_service_for_the_name_before_it_starts_anything(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """Resolution is a question, and it is asked before any run exists."""
    with ScriptedService(named_serving_answers()) as service:
        run_command(named_order, service)
        asked = [(call.method, call.path) for call in service.calls]

    assert asked[0] == ("GET", BY_NAME_URL_PATH)
    assert asked.index(("POST", RUNS_URL_PATH)) > asked.index(("GET", BY_NAME_URL_PATH))


@pytest.mark.proves("a-refused-name-ends-the-command-unsuccessfully")
def test_a_name_the_service_refuses_starts_no_run_and_ends_unsuccessfully(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    refused = problem_answer(
        HTTPStatus.NOT_FOUND,
        "urn:atelier2:problem:v1:catalog-name-not-found",
        "Catalog name not found",
        "No lineage of this kind holds that name at that position.",
    )
    answers = named_serving_answers()
    answers[("GET", BY_NAME_URL_PATH)] = [refused]
    with ScriptedService(answers) as service:
        exit_code = run_command(named_order, service)
        started = service.sent("POST", RUNS_URL_PATH)

    assert (exit_code, started) == (1, [])
    assert "catalog-name-not-found" in capsysbinary.readouterr().err.decode()


def test_a_run_names_either_a_document_or_a_name_and_never_both(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """Two sources for the same revision would leave the operator guessing."""
    workflow = tmp_path / "workflow.yaml"
    workflow.write_bytes(WORKFLOW_DOCUMENT)

    with pytest.raises(SystemExit):
        main(
            [
                "run",
                "--workflow",
                str(workflow),
                "--name",
                NAME,
                "--service",
                "http://x",
            ]
        )

    assert b"--name" in capsysbinary.readouterr().err


def test_a_run_that_names_no_workflow_at_all_is_refused_before_any_request(
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    with pytest.raises(SystemExit):
        main(["run", "--service", "http://x"])

    assert b"--workflow" in capsysbinary.readouterr().err


def test_a_position_without_a_name_is_refused_rather_than_ignored(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """Reading an option and then ignoring it is a quiet disagreement."""
    workflow = tmp_path / "workflow.yaml"
    workflow.write_bytes(WORKFLOW_DOCUMENT)

    with pytest.raises(SystemExit):
        main(
            [
                "run",
                "--workflow",
                str(workflow),
                "--position",
                "2",
                "--service",
                "http://x",
            ]
        )

    assert b"--position" in capsysbinary.readouterr().err


@pytest.mark.proves("one-command-runs-the-workflow-a-name-holds")
def test_a_named_run_at_an_exact_member_asks_the_service_for_that_member(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """A name can be run at the member the operator meant, not only at its head."""
    path = f"{BY_NAME_URL_PATH}?position=1"
    answers = named_serving_answers()
    del answers[("GET", BY_NAME_URL_PATH)]
    answers[("GET", path)] = [name_answer()]
    with ScriptedService(answers) as service:
        exit_code = run_command(named_order, service, "--position", "1")
        asked = [call.path for call in service.calls]

    assert exit_code == 0
    assert path in asked
