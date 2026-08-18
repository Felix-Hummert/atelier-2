"""The stdio MCP door is a client of the published API, not a second way in."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Self
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from atelier2.api.openapi import API_PREFIX, WORKFLOW_DOCUMENT_COMPONENT
from atelier2.api.problems import problem_resource
from atelier2.api.wire.requests import (
    InlineOrderResource,
    StartRunAgentBindingResourceV2,
)
from atelier2.api.wire.resources import (
    CatalogNameResolutionResource,
    NodeRailResource,
    RunResourceV3,
    VersionedWorkflowRevisionPageResource,
    WorkflowRevisionSummaryResourceV2,
)
from atelier2.contracts.run_projections import NodeState
from atelier2.host import main
from atelier2.host.mcp_command import (
    JSONRPC_METHOD_NOT_FOUND,
    dispatch_message,
    read_message,
    serve_mcp,
    write_message,
)
from atelier2.host.mcp_tools import (
    JSONRPC_VERSION,
    MCP_PROTOCOL_VERSION,
    MCP_TOOL_HTTP_DOORS,
    METHOD_INITIALIZE,
    METHOD_INITIALIZED,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    McpToolName,
    tool_definitions,
)
from atelier2.host.run_command import (
    AgentRoleBinding,
    SuppliedOrder,
    start_request_body,
)
from tests.api.test_openapi import served_app

JSON_MEDIA_TYPE = "application/json"
PROBLEM_MEDIA_TYPE = "application/problem+json"

REVISION_HASH = "c" * 64
LINEAGE_ID = "d" * 64
BINDING_SET_HASH = "3" * 64
CONFIGURATION_HASH = "4" * 64
AGENT_CONFIGURATION_HASH = "b" * 64
PUBLIC_RUN_REFERENCE = "run1.dGVzdA"
WORKFLOW_NAME = "review-bounded-diff"

RUNS_PATH = API_PREFIX + "/runs"
RUN_PATH = f"{RUNS_PATH}/{PUBLIC_RUN_REFERENCE}"
ANSWERS_PATH = f"{RUN_PATH}/answers"
DESCRIBED_PATH = API_PREFIX + "/workflow-revisions?view=described"
BY_NAME_PATH = API_PREFIX + f"/workflow-revisions/by-name/{WORKFLOW_NAME}"
MISSING_NAME_PATH = API_PREFIX + "/workflow-revisions/by-name/no-such-workflow"


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
    """One real HTTP server answering the routes the MCP child uses."""

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


class StdioMcpSession:
    """A real stdio MCP client against one service URL."""

    def __init__(self, service_url: str) -> None:
        to_server_read, to_server_write = os.pipe()
        from_server_read, from_server_write = os.pipe()
        self._stdin = os.fdopen(to_server_read, "rb")
        self._stdout = os.fdopen(from_server_write, "wb")
        self._writer = os.fdopen(to_server_write, "wb")
        self._reader = os.fdopen(from_server_read, "rb")
        self._next_id = 1
        self._thread = Thread(
            target=serve_mcp,
            args=(service_url, self._stdin, self._stdout),
            daemon=True,
        )
        self._thread.start()
        opened = self.request(
            METHOD_INITIALIZE,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "atelier2-test", "version": "0"},
            },
        )
        result = opened["result"]
        assert isinstance(result, dict)
        assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
        write_message(
            self._writer,
            {"jsonrpc": JSONRPC_VERSION, "method": METHOD_INITIALIZED},
        )

    def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        message_id = self._next_id
        self._next_id += 1
        write_message(
            self._writer,
            {
                "jsonrpc": JSONRPC_VERSION,
                "id": message_id,
                "method": method,
                "params": {} if params is None else params,
            },
        )
        reply = read_message(self._reader)
        assert isinstance(reply, dict)
        return reply

    def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        reply = self.request(METHOD_TOOLS_CALL, {"name": name, "arguments": arguments})
        result = reply["result"]
        assert isinstance(result, dict)
        content = result["content"]
        assert isinstance(content, list)
        first = content[0]
        assert isinstance(first, dict)
        payload = json.loads(str(first["text"]))
        assert isinstance(payload, dict)
        return payload, bool(result["isError"])

    def close(self) -> None:
        self._writer.close()
        self._thread.join(timeout=5)
        self._reader.close()
        self._stdin.close()
        self._stdout.close()


@pytest.fixture
def session() -> Iterator[tuple[ScriptedService, StdioMcpSession]]:
    with ScriptedService(catalog_and_run_answers()) as service:
        client = StdioMcpSession(service.url)
        try:
            yield service, client
        finally:
            client.close()


def unrouted_answer() -> Answer:
    return problem_answer("run-not-found")


def problem_answer(code: str) -> Answer:
    problem = problem_resource(code)
    return Answer(
        problem.model_dump_json().encode(),
        status=HTTPStatus(problem.status),
        media_type=PROBLEM_MEDIA_TYPE,
    )


def catalog_resolution() -> CatalogNameResolutionResource:
    return CatalogNameResolutionResource(
        display_name=WORKFLOW_NAME,
        lineage_id=LINEAGE_ID,
        revision_hash=REVISION_HASH,
        revision_number=1,
    )


def described_page() -> VersionedWorkflowRevisionPageResource:
    return VersionedWorkflowRevisionPageResource(
        items=(
            WorkflowRevisionSummaryResourceV2(
                revision_hash=REVISION_HASH,
                format_version=3,
                executable=True,
                not_executable_reason=None,
                name=WORKFLOW_NAME,
                description=None,
            ),
        ),
        next_after_revision_hash=None,
    )


def started_run() -> RunResourceV3:
    return RunResourceV3(
        workflow_format_version=3,
        run_id="named-run",
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        agent_binding_set_hash=BINDING_SET_HASH,
        run_configuration_revision_hash=CONFIGURATION_HASH,
        agent_bindings=(),
        state_version=1,
        state="STARTED",
        current_node_id="implement",
        node_rail=(
            NodeRailResource(
                node_id="implement", state=NodeState.WORKING, attempt=None
            ),
        ),
        terminal_hash=None,
        latest_event_cursor=None,
    )


def catalog_and_run_answers() -> dict[tuple[str, str], list[Answer]]:
    return {
        ("GET", DESCRIBED_PATH): [Answer(described_page().model_dump_json().encode())],
        ("GET", BY_NAME_PATH): [
            Answer(catalog_resolution().model_dump_json().encode())
        ],
        ("GET", MISSING_NAME_PATH): [problem_answer("catalog-name-not-found")],
        ("POST", RUNS_PATH): [
            Answer(
                started_run().model_dump_json().encode(),
                status=HTTPStatus.CREATED,
            )
        ],
        ("GET", RUN_PATH): [Answer(started_run().model_dump_json().encode())],
        ("POST", ANSWERS_PATH): [problem_answer("invalid-request")],
        (
            "GET",
            API_PREFIX + "/runs/run1.bWlzc2luZw",
        ): [problem_answer("run-not-found")],
    }


def http_json(
    method: str, url: str, body: bytes | None = None
) -> tuple[int, dict[str, Any]]:
    request = Request(
        url,
        data=body,
        method=method,
        headers={"accept": JSON_MEDIA_TYPE, "content-type": JSON_MEDIA_TYPE},
    )
    try:
        with urlopen(request, timeout=5) as response:
            status = response.status
            body = json.loads(response.read())
    except HTTPError as refusal:
        status = refusal.status
        body = json.loads(refusal.read())
    assert status is not None
    assert isinstance(body, dict)
    return status, body


def test_tools_list_names_the_four_doors_from_the_one_owner(
    session: tuple[ScriptedService, StdioMcpSession],
) -> None:
    _service, client = session

    reply = client.request(METHOD_TOOLS_LIST)
    listed = reply["result"]
    assert isinstance(listed, dict)
    tools = listed["tools"]
    assert isinstance(tools, list)
    names = [tool["name"] for tool in tools if isinstance(tool, dict)]

    assert names == [tool.value for tool in McpToolName]
    assert names == [definition["name"] for definition in tool_definitions()]


@pytest.mark.proves("mcp-tools-are-the-published-http-doors")
def test_each_tool_is_bound_to_paths_the_published_openapi_still_serves() -> None:
    """A removed or renamed HTTP door must turn this adapter red, not drift."""

    published = served_app().openapi()
    paths = published["paths"]
    for tool, doors in MCP_TOOL_HTTP_DOORS.items():
        assert doors, tool
        for method, path in doors:
            assert path in paths, f"{tool} names {path}, which OpenAPI does not publish"
            assert method in paths[path], f"{tool} names {method} {path}"


@pytest.mark.proves("mcp-tools-are-the-published-http-doors")
def test_start_run_schema_references_the_published_grammar_instead_of_copying_it() -> (
    None
):
    start = next(
        definition
        for definition in tool_definitions()
        if definition["name"] == McpToolName.START_RUN.value
    )
    orders = start["inputSchema"]["properties"]["orders"]
    bindings = start["inputSchema"]["properties"]["agent_bindings"]

    assert WORKFLOW_DOCUMENT_COMPONENT in start["description"]
    assert WORKFLOW_DOCUMENT_COMPONENT in orders["description"]
    assert orders["items"] == InlineOrderResource.model_json_schema()
    assert bindings["items"] == StartRunAgentBindingResourceV2.model_json_schema()


@pytest.mark.proves("mcp-and-http-never-diverge")
def test_an_unadmitted_name_is_the_same_problem_on_both_paths(
    session: tuple[ScriptedService, StdioMcpSession],
) -> None:
    """If the MCP path rewrites the problem type, this assertion dies."""

    service, client = session
    http_status, http_body = http_json("GET", service.url + MISSING_NAME_PATH)
    payload, is_error = client.call_tool(
        McpToolName.START_RUN.value,
        {"name": "no-such-workflow", "run_id": "unused"},
    )

    assert is_error
    assert http_status == 404
    assert payload["type"] == http_body["type"]
    assert payload["title"] == http_body["title"]
    assert payload["status"] == http_body["status"]
    assert payload["detail"] == http_body["detail"]
    assert payload["type"].endswith("catalog-name-not-found")


@pytest.mark.proves("mcp-and-http-never-diverge")
def test_a_missing_run_is_the_same_problem_on_both_paths(
    session: tuple[ScriptedService, StdioMcpSession],
) -> None:
    service, client = session
    missing = API_PREFIX + "/runs/run1.bWlzc2luZw"
    http_status, http_body = http_json("GET", service.url + missing)
    payload, is_error = client.call_tool(
        McpToolName.RUN_STATUS.value,
        {"public_run_reference": "run1.bWlzc2luZw"},
    )

    assert is_error
    assert http_status == 404
    assert payload == http_body
    assert payload["type"].endswith("run-not-found")


@pytest.mark.proves("mcp-and-http-never-diverge")
def test_an_unanswerable_wait_is_the_same_problem_on_both_paths(
    session: tuple[ScriptedService, StdioMcpSession],
) -> None:
    service, client = session
    body = json.dumps(
        {
            "revision_hash": REVISION_HASH,
            "node_id": "waiting",
            "answer_base64": "Ng==",
        }
    ).encode()
    http_status, http_body = http_json("POST", service.url + ANSWERS_PATH, body)
    payload, is_error = client.call_tool(
        McpToolName.ANSWER_WAIT.value,
        {
            "public_run_reference": PUBLIC_RUN_REFERENCE,
            "revision_hash": REVISION_HASH,
            "node_id": "waiting",
            "answer_base64": "Ng==",
        },
    )

    assert is_error
    assert http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert payload == http_body
    assert payload["type"].endswith("invalid-request")


@pytest.mark.proves("mcp-and-http-never-diverge")
def test_list_workflows_answers_the_same_catalog_resolution_http_would(
    session: tuple[ScriptedService, StdioMcpSession],
) -> None:
    service, client = session
    _status, http_body = http_json("GET", service.url + BY_NAME_PATH)
    payload, is_error = client.call_tool(McpToolName.LIST_WORKFLOWS.value, {})

    assert not is_error
    assert payload == {"items": [http_body]}
    assert http_body == catalog_resolution().model_dump(mode="json")


@pytest.mark.proves("mcp-and-http-never-diverge")
@pytest.mark.parametrize(
    ("arguments", "bindings", "orders"),
    (
        ({"name": WORKFLOW_NAME, "run_id": "named-run"}, (), ()),
        (
            {
                "name": WORKFLOW_NAME,
                "run_id": "named-run",
                "agent_bindings": [
                    {
                        "role": "builder",
                        "agent_configuration_revision_hash": AGENT_CONFIGURATION_HASH,
                    }
                ],
            },
            (AgentRoleBinding("builder", AGENT_CONFIGURATION_HASH),),
            (),
        ),
        (
            {
                "name": WORKFLOW_NAME,
                "run_id": "named-run",
                "agent_bindings": [
                    {
                        "role": "builder",
                        "agent_configuration_revision_hash": AGENT_CONFIGURATION_HASH,
                    }
                ],
                "orders": [{"name": "order", "value": '{"portions": 2}'}],
            },
            (AgentRoleBinding("builder", AGENT_CONFIGURATION_HASH),),
            (SuppliedOrder("order", b'{"portions": 2}'),),
        ),
    ),
    ids=("v1-bare", "v2-bindings", "v3-orders"),
)
def test_start_run_posts_the_same_start_body_the_http_door_already_owns(
    session: tuple[ScriptedService, StdioMcpSession],
    arguments: dict[str, Any],
    bindings: tuple[AgentRoleBinding, ...],
    orders: tuple[SuppliedOrder, ...],
) -> None:
    service, client = session
    payload, is_error = client.call_tool(McpToolName.START_RUN.value, arguments)

    posted = json.loads(
        next(call.body for call in service.calls if call.path == RUNS_PATH)
    )
    owned = json.loads(start_request_body("named-run", REVISION_HASH, bindings, orders))
    _status, http_body = http_json("GET", service.url + RUN_PATH)

    assert not is_error
    assert payload == http_body
    assert posted == owned


@pytest.mark.proves("a-jsonrpc-notification-gets-no-reply")
def test_a_notification_produces_no_reply_frame() -> None:
    assert (
        dispatch_message(
            "http://127.0.0.1:1",
            {
                "jsonrpc": JSONRPC_VERSION,
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            },
        )
        is None
    )


@pytest.mark.proves("a-jsonrpc-notification-gets-no-reply")
def test_an_unknown_request_keeps_the_id_it_arrived_with() -> None:
    reply = dispatch_message(
        "http://127.0.0.1:1",
        {"jsonrpc": JSONRPC_VERSION, "id": 7, "method": "notifications/cancelled"},
    )

    assert reply is not None
    assert reply["id"] == 7
    assert reply["error"]["code"] == JSONRPC_METHOD_NOT_FOUND


@pytest.mark.proves("a-jsonrpc-notification-gets-no-reply")
def test_a_cancelled_notification_does_not_steal_the_next_reply(
    session: tuple[ScriptedService, StdioMcpSession],
) -> None:
    """The witness reproduction: cancelled must not emit {id: null, error}."""
    _service, client = session
    write_message(
        client._writer,
        {
            "jsonrpc": JSONRPC_VERSION,
            "method": "notifications/cancelled",
            "params": {"requestId": 1},
        },
    )
    listed = client.request(METHOD_TOOLS_LIST)

    assert "result" in listed
    assert "error" not in listed


@pytest.mark.proves("a-non-loopback-mcp-service-is-refused-without-inventing-auth")
def test_a_non_loopback_service_is_refused_instead_of_inventing_auth(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["mcp", "--service", "http://203.0.113.1:8422"])

    printed = capsys.readouterr()
    assert exit_code == 1
    assert printed.out == ""
    assert "loopback" in printed.err
    assert "authentication" in printed.err
