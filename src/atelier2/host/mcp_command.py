"""stdio MCP server: four tools, each a client of the public HTTP API.

This module is a third door onto the same product, not a second way into it.
Every effect travels through the published HTTP API of a service someone is
already serving. There is no listener, no port, no token, and no caller
identity: the API has no authentication concept for callers, #82 is human
OIDC, and ADR 0009 (machine credentials) is not landed. The child therefore
has the same trust as the browser on loopback, and it refuses any other
service address rather than inventing a credential.

The official MCP SDK is a multi-transport server (SSE, HTTP sessions,
resources, prompts). This slice is stdio JSON-RPC for four tools. A
dependency would add a second HTTP stack beside FastAPI without removing a
hard problem this door has. Protocol tokens live with the tools; framing
lives here.

A billed executor composed on the service is the item's stop condition for
any future non-loopback exposure. This process cannot listen, so that
exposure is not this slice.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import IO, Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from pydantic import TypeAdapter, ValidationError

from atelier2.api.openapi import API_PREFIX
from atelier2.api.wire.requests import (
    InlineOrderResource,
    RevisionListingView,
    StartRunAgentBindingResourceV2,
    StartRunRequestResource,
    StartRunRequestResourceV2,
    StartRunRequestResourceV3,
)
from atelier2.api.wire.resources import (
    AnyRunResource,
    CatalogNameResolutionResource,
    ProblemResource,
    VersionedWorkflowRevisionPageResource,
)
from atelier2.host.address import (
    ADDRESSABLE_SCHEMES,
    DEFAULT_SERVICE_URL,
    is_loopback_service_url,
)
from atelier2.host.mcp_tools import (
    JSONRPC_VERSION,
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    METHOD_INITIALIZE,
    METHOD_INITIALIZED,
    METHOD_PING,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    McpToolName,
    catalog_listing_resource,
    problem_payload,
    tool_definitions,
)
from atelier2.host.run_command import (
    DEFAULT_CATALOG_POSITION,
    JSON_MEDIA_TYPE,
    REQUEST_TIMEOUT_SECONDS,
    RUN_PATH,
    WORKFLOW_REVISION_PATH,
    NameOrder,
    ServiceRefused,
    ServiceUnreachable,
    UnreadableServiceAnswer,
    UnusableRunOrder,
    resolve_published_name,
)

_run_resource = TypeAdapter[AnyRunResource](AnyRunResource)
_catalog_name_resolution = TypeAdapter(CatalogNameResolutionResource)
_described_page = TypeAdapter(VersionedWorkflowRevisionPageResource)

HEADER_TERMINATOR = b"\r\n\r\n"
MAXIMUM_HEADER_BYTES = 8_192
MAXIMUM_MESSAGE_BYTES = 1_048_576
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

ToolHandler = Callable[[str, Mapping[str, Any]], dict[str, Any]]


class McpServiceRefusal(Exception):
    """The operator named a service this process will not speak to."""


@dataclass(frozen=True)
class _JsonRpcRequest:
    message_id: object
    method: str
    params: Mapping[str, Any]


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
    """Read framed JSON-RPC from stdin until it closes. Write each reply."""

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
    if parsed.method == METHOD_INITIALIZED:
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
    """One Content-Length framed JSON-RPC message, or None at end of input."""

    headers = bytearray()
    while not headers.endswith(HEADER_TERMINATOR):
        chunk = stream.read(1)
        if not chunk:
            if not headers:
                return None
            raise UnreadableServiceAnswer("the stream ended mid-header")
        headers.extend(chunk)
        if len(headers) > MAXIMUM_HEADER_BYTES:
            raise UnreadableServiceAnswer(
                "the MCP header is larger than this server reads"
            )
    declared = _content_length(bytes(headers))
    body = stream.read(declared)
    if len(body) != declared:
        raise UnreadableServiceAnswer("the MCP body ended before Content-Length")
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise UnreadableServiceAnswer(f"the MCP body is not JSON: {error}") from error


def write_message(stream: IO[bytes], message: Mapping[str, Any]) -> None:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    stream.flush()


def _content_length(headers: bytes) -> int:
    for line in headers.split(b"\r\n"):
        name, separator, value = line.partition(b":")
        if separator and name.lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError as error:
                raise UnreadableServiceAnswer(
                    "Content-Length is not an integer"
                ) from error
            if not 0 < length <= MAXIMUM_MESSAGE_BYTES:
                raise UnreadableServiceAnswer(
                    "Content-Length is outside what this server reads"
                )
            return length
    raise UnreadableServiceAnswer("the MCP header names no Content-Length")


def _jsonrpc_request(raw: object) -> _JsonRpcRequest | dict[str, Any]:
    if not isinstance(raw, dict):
        return _error(None, JSONRPC_INVALID_REQUEST, "a JSON-RPC message is an object")
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
    if "id" not in raw:
        return _JsonRpcRequest(None, method, params)
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
            _start_request(run_id, resolution.revision_hash, bindings, orders),
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
    return ended.model_dump(mode="json")


def answer_wait(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    reference = _required_text(arguments, "public_run_reference")
    revision_hash = _required_text(arguments, "revision_hash")
    node_id = _required_text(arguments, "node_id")
    answer_base64 = _required_text(arguments, "answer_base64")
    answered = _decoded(
        _run_resource,
        _post(
            f"{_api_url(service_url)}{RUN_PATH}/{quote(reference, safe='')}/answers",
            json.dumps(
                {
                    "revision_hash": revision_hash,
                    "node_id": node_id,
                    "answer_base64": answer_base64,
                }
            ).encode(),
        ),
        "a run",
    )
    return answered.model_dump(mode="json")


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


def _orders(raw: object) -> tuple[InlineOrderResource, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise UnusableRunOrder("orders must be an array")
    try:
        return tuple(InlineOrderResource.model_validate(item) for item in raw)
    except ValidationError as error:
        raise UnusableRunOrder(
            f"orders is not the start request's inline-order shape: {error}"
        ) from error


def _start_request(
    run_id: str,
    revision_hash: str,
    bindings: tuple[StartRunAgentBindingResourceV2, ...],
    orders: tuple[InlineOrderResource, ...],
) -> bytes:
    """The same three POST /runs shapes `run` already writes, from the wire owner."""

    if orders:
        requested: (
            StartRunRequestResource
            | StartRunRequestResourceV2
            | StartRunRequestResourceV3
        ) = StartRunRequestResourceV3(
            workflow_format_version=3,
            run_id=run_id,
            workflow_revision_hash=revision_hash,
            agent_bindings=bindings,
            orders=orders,
        )
    elif bindings:
        requested = StartRunRequestResourceV2(
            workflow_format_version=2,
            run_id=run_id,
            workflow_revision_hash=revision_hash,
            agent_bindings=bindings,
        )
    else:
        requested = StartRunRequestResource(
            run_id=run_id, workflow_revision_hash=revision_hash
        )
    return requested.model_dump_json().encode()


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
