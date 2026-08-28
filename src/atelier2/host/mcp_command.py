"""stdio MCP server: five tools, each a client of the public HTTP API.

This module is a third door onto the same product, not a second way into it.
Every effect travels through the published HTTP API of a service someone is
already serving. There is no listener, no port, no token, and no caller
identity: the API has no authentication concept for callers, #82 is human
OIDC, and ADR 0009 (machine credentials) is not landed. The child therefore
has the same trust as the browser on loopback, and it refuses any other
service address rather than inventing a credential.

The official MCP SDK is a multi-transport server (SSE, HTTP sessions,
resources, prompts). This slice is stdio JSON-RPC for five tools. A
dependency would add a second HTTP stack beside FastAPI without removing a
hard problem this door has. Protocol tokens live with the tools; the
newline-delimited JSON-RPC line protocol lives here.

A billed executor composed on the service is the item's stop condition for
any future non-loopback exposure. This process cannot listen, so that
exposure is not this slice.
"""

from __future__ import annotations

import base64
import binascii
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import IO, Any, assert_never
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from pydantic import TypeAdapter, ValidationError

from atelier2.api.openapi import API_PREFIX
from atelier2.api.wire.requests import (
    ArtifactOrderResource,
    RevisionListingView,
    StartRunAgentBindingResourceV2,
    WorkItemOrderResource,
)
from atelier2.api.wire.resources import (
    AnyRunResource,
    ArtifactResource,
    CatalogNameResolutionResource,
    ProblemResource,
    RunResourceV3,
    VersionedWorkflowRevisionPageResource,
)
from atelier2.contracts.executions import WaitAnswerActor
from atelier2.host.address import (
    ADDRESSABLE_SCHEMES,
    DEFAULT_SERVICE_URL,
    is_loopback_service_url,
)
from atelier2.host.mcp_tools import (
    JSONRPC_VERSION,
    MAXIMUM_MCP_ARTIFACT_BYTES,
    MAXIMUM_MCP_INPUT_LINE_BYTES,
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    METHOD_INITIALIZE,
    METHOD_PING,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    McpToolName,
    catalog_listing_resource,
    problem_payload,
    tool_definitions,
)
from atelier2.host.run_command import (
    ARTIFACT_PATH,
    DEFAULT_CATALOG_POSITION,
    JSON_MEDIA_TYPE,
    OCTET_STREAM_MEDIA_TYPE,
    REQUEST_TIMEOUT_SECONDS,
    RUN_PATH,
    WORKFLOW_REVISION_PATH,
    AgentRoleBinding,
    NameOrder,
    ServiceRefused,
    ServiceUnreachable,
    SuppliedArtifactOrder,
    SuppliedStartOrder,
    SuppliedWorkItemOrder,
    UnreadableServiceAnswer,
    UnusableRunOrder,
    resolve_published_name,
    start_request_body,
)

_run_resource = TypeAdapter[AnyRunResource](AnyRunResource)
_catalog_name_resolution = TypeAdapter(CatalogNameResolutionResource)
_described_page = TypeAdapter(VersionedWorkflowRevisionPageResource)
_artifact_resource = TypeAdapter(ArtifactResource)
_start_run_order = TypeAdapter(ArtifactOrderResource | WorkItemOrderResource)

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

ToolHandler = Callable[[str, Mapping[str, Any]], dict[str, Any]]


class McpServiceRefusal(Exception):
    """The operator named a service this process will not speak to."""


class McpArtifactPayloadTooLarge(UnusableRunOrder):
    """The decoded artifact cannot fit the bounded MCP JSON-RPC request."""

    def __init__(self, decoded_bytes: int) -> None:
        super().__init__(
            "mcp-artifact-payload-too-large: "
            f"{decoded_bytes} decoded bytes exceeds {MAXIMUM_MCP_ARTIFACT_BYTES}"
        )


class McpStartRunOrderRefusal(UnusableRunOrder):
    """An MCP start order is outside this door's artifact/work-item subset."""

    def __init__(self) -> None:
        super().__init__(
            "mcp-start-run-order-invalid: each order must be exactly "
            "{name, artifact_hash} or {name, work_item}"
        )


@dataclass(frozen=True)
class _JsonRpcRequest:
    message_id: object
    method: str
    params: Mapping[str, Any]
    notification: bool = False


def execute_mcp(service_url: str, stdin: IO[bytes], stdout: IO[bytes]) -> int:
    """Speak MCP on the given streams against one loopback service."""

    try:
        _require_loopback_service(service_url)
    except McpServiceRefusal as refusal:
        print(refusal, file=sys.stderr)
        return 1
    serve_mcp(service_url, stdin, stdout)
    return 0


def serve_mcp(service_url: str, stdin: IO[bytes], stdout: IO[bytes]) -> None:
    """Read newline-delimited JSON-RPC from stdin until it closes. Write each reply."""

    while True:
        try:
            raw = read_message(stdin)
        except UnreadableServiceAnswer as refusal:
            write_message(stdout, _error(None, JSONRPC_PARSE_ERROR, str(refusal)))
            continue
        if raw is None:
            return
        reply = dispatch_message(service_url, raw)
        if reply is not None:
            write_message(stdout, reply)


def dispatch_message(service_url: str, raw: object) -> dict[str, Any] | None:
    """One JSON-RPC message to at most one reply. Notifications return None."""

    parsed = _jsonrpc_request(raw)
    if isinstance(parsed, dict):
        return parsed
    if parsed.notification:
        return None
    if parsed.method == METHOD_INITIALIZE:
        return _result(parsed.message_id, _initialize_result())
    if parsed.method == METHOD_PING:
        return _result(parsed.message_id, {})
    if parsed.method == METHOD_TOOLS_LIST:
        return _result(parsed.message_id, {"tools": list(tool_definitions())})
    if parsed.method == METHOD_TOOLS_CALL:
        return _result(parsed.message_id, _call_tool(service_url, parsed.params))
    return _error(
        parsed.message_id,
        JSONRPC_METHOD_NOT_FOUND,
        f"method {parsed.method!r} is not this server",
    )


def read_message(stream: IO[bytes]) -> object | None:
    """One newline-delimited JSON-RPC message, or None at end of input."""

    line = stream.readline(MAXIMUM_MCP_INPUT_LINE_BYTES + 1)
    if not line:
        return None
    if len(line) > MAXIMUM_MCP_INPUT_LINE_BYTES and not line.endswith(b"\n"):
        raise UnreadableServiceAnswer("the MCP line is larger than this server reads")
    try:
        return json.loads(line)
    except json.JSONDecodeError as error:
        raise UnreadableServiceAnswer(f"the MCP line is not JSON: {error}") from error


def write_message(stream: IO[bytes], message: Mapping[str, Any]) -> None:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
    stream.write(body + b"\n")
    stream.flush()


def _jsonrpc_request(raw: object) -> _JsonRpcRequest | dict[str, Any]:
    if not isinstance(raw, dict):
        return _error(None, JSONRPC_INVALID_REQUEST, "a JSON-RPC message is an object")
    if "id" not in raw:
        method = raw.get("method")
        params = raw.get("params") or {}
        return _JsonRpcRequest(
            None,
            method if isinstance(method, str) else "",
            params if isinstance(params, dict) else {},
            notification=True,
        )
    message_id = raw.get("id")
    if raw.get("jsonrpc") != JSONRPC_VERSION:
        return _error(message_id, JSONRPC_INVALID_REQUEST, "jsonrpc must be 2.0")
    method = raw.get("method")
    if not isinstance(method, str) or not method:
        return _error(message_id, JSONRPC_INVALID_REQUEST, "method must be a string")
    params = raw.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(message_id, JSONRPC_INVALID_PARAMS, "params must be an object")
    return _JsonRpcRequest(message_id, method, params)


def _initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
    }


def _call_tool(service_url: str, params: Mapping[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str) or not name:
        return _tool_error({"error": "tools/call requires a name"})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _tool_error({"error": "tools/call arguments must be an object"})
    try:
        tool = McpToolName(name)
    except ValueError:
        return _tool_error({"error": f"unknown tool {name!r}"})
    try:
        payload = _tool_handlers()[tool](service_url, arguments)
    except ServiceRefused as refused:
        if refused.problem is None:
            return _tool_error({"error": str(refused)})
        return _tool_error(problem_payload(refused.problem))
    except (ServiceUnreachable, UnreadableServiceAnswer, UnusableRunOrder) as refusal:
        return _tool_error({"error": str(refusal)})
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": False,
    }


def _tool_handlers() -> dict[McpToolName, ToolHandler]:
    return {
        McpToolName.LIST_WORKFLOWS: list_workflows,
        McpToolName.START_RUN: start_run,
        McpToolName.RUN_STATUS: run_status,
        McpToolName.ANSWER_WAIT: answer_wait,
        McpToolName.PUBLISH_ARTIFACT: publish_artifact,
    }


def list_workflows(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    del arguments
    api = _api_url(service_url)
    items: list[CatalogNameResolutionResource] = []
    seen: set[str] = set()
    after: str | None = None
    while True:
        query = {"view": RevisionListingView.DESCRIBED.value}
        if after is not None:
            query["after_revision_hash"] = after
        page = _decoded(
            _described_page,
            _get(f"{api}{WORKFLOW_REVISION_PATH}?{urlencode(query)}"),
            "a described revision page",
        )
        for revision in page.items:
            name = revision.name
            if name is None or name in seen:
                continue
            seen.add(name)
            resolved = _resolve_listed_name(api, name)
            if resolved is not None:
                items.append(resolved)
        if page.next_after_revision_hash is None:
            break
        after = page.next_after_revision_hash
    return catalog_listing_resource(tuple(items))


def start_run(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    name = _required_text(arguments, "name")
    run_id = _required_text(arguments, "run_id")
    position = arguments.get("position", DEFAULT_CATALOG_POSITION)
    if not isinstance(position, str) or not position:
        raise UnusableRunOrder("position must be a nonempty string")
    bindings = _bindings(arguments.get("agent_bindings"))
    orders = _orders(arguments.get("orders"))
    resolution = resolve_published_name(NameOrder(service_url, name, position))
    started = _decoded(
        _run_resource,
        _post(
            _api_url(service_url) + RUN_PATH,
            start_request_body(
                run_id,
                resolution.revision_hash,
                tuple(
                    AgentRoleBinding(
                        binding.role, binding.agent_configuration_revision_hash
                    )
                    for binding in bindings
                ),
                orders,
            ),
        ),
        "a run",
    )
    return started.model_dump(mode="json")


def run_status(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    reference = _required_text(arguments, "public_run_reference")
    ended = _decoded(
        _run_resource,
        _get(f"{_api_url(service_url)}{RUN_PATH}/{quote(reference, safe='')}"),
        "a run",
    )
    resource = ended.model_dump(mode="json")
    resource["answerable_wait"] = _answerable_wait(ended)
    return resource


def _answerable_wait(run: AnyRunResource) -> dict[str, str] | None:
    """The exact fence MCP can write back to the answer door, or no open wait."""

    execution_id: str | None = None
    if isinstance(run, RunResourceV3) and run.state == "WAITING_INPUT":
        execution_id = run.cancellation.target_node_execution_id
    if execution_id is None:
        return None
    return {
        "actor": WaitAnswerActor.OPERATOR.value,
        "expected_node_execution_id": execution_id,
    }


def answer_wait(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    reference = _required_text(arguments, "public_run_reference")
    workflow_revision_hash = _required_text(arguments, "workflow_revision_hash")
    node_id = _required_text(arguments, "node_id")
    expected_node_execution_id = _required_text(arguments, "expected_node_execution_id")
    actor = _required_text(arguments, "actor")
    answer_base64 = _required_text(arguments, "answer_base64")
    answered = _decoded(
        _run_resource,
        _post(
            f"{_api_url(service_url)}{RUN_PATH}/{quote(reference, safe='')}/answers",
            json.dumps(
                {
                    "workflow_revision_hash": workflow_revision_hash,
                    "node_id": node_id,
                    "expected_node_execution_id": expected_node_execution_id,
                    "actor": actor,
                    "answer_base64": answer_base64,
                }
            ).encode(),
        ),
        "a run",
    )
    return answered.model_dump(mode="json")


def publish_artifact(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    content = _artifact_bytes(arguments.get("content_base64"))
    published = _decoded(
        _artifact_resource,
        _post_octet_stream(_api_url(service_url) + ARTIFACT_PATH, content),
        "an artifact",
    )
    return published.model_dump(mode="json")


def _resolve_listed_name(api: str, name: str) -> CatalogNameResolutionResource | None:
    """Resolve one listed title. An unadmitted name is not a catalog member."""

    asked = f"{api}{WORKFLOW_REVISION_PATH}/by-name/{quote(name, safe='')}"
    try:
        return _decoded(_catalog_name_resolution, _get(asked), "a catalog name")
    except ServiceRefused as refused:
        if refused.problem is not None and refused.problem.type.endswith(
            "catalog-name-not-found"
        ):
            return None
        raise


def _bindings(raw: object) -> tuple[StartRunAgentBindingResourceV2, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise UnusableRunOrder("agent_bindings must be an array")
    try:
        return tuple(
            StartRunAgentBindingResourceV2.model_validate(item) for item in raw
        )
    except ValidationError as error:
        raise UnusableRunOrder(
            f"agent_bindings is not the start request's binding shape: {error}"
        ) from error


def _orders(raw: object) -> tuple[SuppliedStartOrder, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise McpStartRunOrderRefusal()
    try:
        return tuple(
            _supplied_start_order(_start_run_order.validate_python(item))
            for item in raw
        )
    except ValidationError as error:
        raise McpStartRunOrderRefusal() from error


def _supplied_start_order(
    order: ArtifactOrderResource | WorkItemOrderResource,
) -> SuppliedStartOrder:
    match order:
        case ArtifactOrderResource(name=name, artifact_hash=artifact_hash):
            return SuppliedArtifactOrder(name, artifact_hash)
        case WorkItemOrderResource(name=name, work_item=work_item):
            return SuppliedWorkItemOrder(name, work_item)
        case _ as unreachable:
            assert_never(unreachable)


def _artifact_bytes(raw: object) -> bytes:
    # MCP JSON cannot carry octet-stream; the HTTP door still takes the bytes.
    if not isinstance(raw, str):
        raise UnusableRunOrder("content_base64 must be a string")
    try:
        content = base64.b64decode(raw, validate=True)
    except binascii.Error as error:
        raise UnusableRunOrder(
            f"content_base64 is not standard Base64 of the exact bytes: {error}"
        ) from error
    if len(content) > MAXIMUM_MCP_ARTIFACT_BYTES:
        raise McpArtifactPayloadTooLarge(len(content))
    return content


def _required_text(arguments: Mapping[str, Any], field: str) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value:
        raise UnusableRunOrder(f"{field} must be a nonempty string")
    return value


def _require_loopback_service(service_url: str) -> None:
    address = urlsplit(service_url)
    if address.scheme not in ADDRESSABLE_SCHEMES or not address.netloc:
        raise McpServiceRefusal(
            f"{service_url!r} is not the address of a served Atelier API; "
            f"name one as {DEFAULT_SERVICE_URL!r}"
        )
    if not is_loopback_service_url(service_url):
        raise McpServiceRefusal(
            f"{service_url!r} is not a loopback Atelier API: this MCP child "
            "has no caller authentication, so it speaks only to the same "
            "machine the browser already trusts"
        )


def _api_url(service_url: str) -> str:
    address = urlsplit(service_url)
    if address.scheme not in ADDRESSABLE_SCHEMES or not address.netloc:
        raise UnusableRunOrder(
            f"{service_url!r} is not the address of a served Atelier API; "
            f"name one as {DEFAULT_SERVICE_URL!r}"
        )
    return service_url.rstrip("/") + API_PREFIX


def _post(url: str, payload: bytes) -> bytes:
    return _read(
        Request(
            url,
            data=payload,
            method="POST",
            headers={"content-type": JSON_MEDIA_TYPE, "accept": JSON_MEDIA_TYPE},
        )
    )


def _post_octet_stream(url: str, payload: bytes) -> bytes:
    return _read(
        Request(
            url,
            data=payload,
            method="POST",
            headers={
                "content-type": OCTET_STREAM_MEDIA_TYPE,
                "accept": JSON_MEDIA_TYPE,
            },
        )
    )


def _get(url: str) -> bytes:
    return _read(Request(url, method="GET", headers={"accept": JSON_MEDIA_TYPE}))


def _read(request: Request) -> bytes:
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except HTTPError as refusal:
        raise _service_refused(refusal) from refusal
    except URLError as unreachable:
        raise ServiceUnreachable(
            f"no Atelier service answered at {request.full_url}: {unreachable.reason}"
        ) from unreachable


def _service_refused(refusal: HTTPError) -> ServiceRefused:
    answered = refusal.read()
    try:
        problem = ProblemResource.model_validate_json(answered)
    except ValidationError:
        return ServiceRefused(
            f"{refusal.url} answered {refusal.status} {refusal.reason}"
        )
    return ServiceRefused(
        f"{refusal.url} refused this: {problem.status} {problem.title} "
        f"[{problem.type}] {problem.detail}",
        problem,
    )


def _decoded[Resource](
    adapter: TypeAdapter[Resource], answered: bytes, subject: str
) -> Resource:
    try:
        return adapter.validate_json(answered)
    except ValidationError as error:
        raise UnreadableServiceAnswer(
            f"the service answered something this command cannot read as "
            f"{subject}: {error}"
        ) from error


def _result(message_id: object, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "result": result}


def _error(message_id: object, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": message_id,
        "error": {"code": code, "message": message},
    }


def _tool_error(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": True,
    }
