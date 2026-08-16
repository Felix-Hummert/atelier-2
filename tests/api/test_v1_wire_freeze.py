from __future__ import annotations

import hashlib
import json
from typing import cast

from pydantic import BaseModel, TypeAdapter

from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.api.app import create_app
from atelier2.api.projection.runs import run_resource
from atelier2.api.projection.workflows import workflow_revision_detail_resource
from atelier2.api.wire.events import RunEventResource
from atelier2.api.wire.requests import StartRunRequestResource
from atelier2.api.wire.resources import (
    RunPageResource,
    RunResource,
    VersionedRunPageResource,
    WorkflowRevisionPageResource,
    WorkflowRevisionSummaryResource,
)
from atelier2.contracts.run_projections import (
    RunProjection,
)
from atelier2.contracts.runs import Run, RunId, RunState, WorkflowRevision
from atelier2.contracts.workflow_projections import (
    WorkflowRevisionProjection,
)
from tests.api.test_openapi import empty_ports
from tests.scenarios.api import SSE_COMPLETE_HISTORY, api_limits, event_poll_backoff

_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: wait, type: wait, answer_type: integer, next: final}
  - {id: agent, type: agent, job: test, output: payload, next: wait}
"""
_REVISION_HASH = "23ed14762efa2750a3f4997c44b80e79de0ba1d77c0e800abad3b85b812775a3"

_DETAIL = b'{"revision_hash":"23ed14762efa2750a3f4997c44b80e79de0ba1d77c0e800abad3b85b812775a3","document_base64":"Zm9ybWF0X3ZlcnNpb246IDEKc3RhcnQ6IGFnZW50Cm5vZGVzOgogIC0ge2lkOiBmaW5hbCwgdHlwZTogc3Vid29ya2Zsb3csIG9wZXJhdGlvbjogYWRkLCBvcGVyYW5kczogWzIsIDNdLCBuZXh0OiBudWxsfQogIC0ge2lkOiB3YWl0LCB0eXBlOiB3YWl0LCBhbnN3ZXJfdHlwZTogaW50ZWdlciwgbmV4dDogZmluYWx9CiAgLSB7aWQ6IGFnZW50LCB0eXBlOiBhZ2VudCwgam9iOiB0ZXN0LCBvdXRwdXQ6IHBheWxvYWQsIG5leHQ6IHdhaXR9Cg==","graph":{"format_version":1,"start_node_id":"agent","nodes":[{"type":"agent","node_id":"agent","job":"test","output":"payload","next_node_id":"wait"},{"type":"subworkflow","node_id":"final","operation":"add","operands":[2,3],"next_node_id":null},{"type":"wait","node_id":"wait","answer_type":"integer","next_node_id":"final"}]}}'
_PAGE = b'{"items":[{"revision_hash":"23ed14762efa2750a3f4997c44b80e79de0ba1d77c0e800abad3b85b812775a3"}],"next_after_revision_hash":null}'
_START = b'{"run_id":"run","workflow_revision_hash":"23ed14762efa2750a3f4997c44b80e79de0ba1d77c0e800abad3b85b812775a3"}'
_RUN = b'{"run_id":"run","public_run_reference":"run1.cnVu","workflow_revision_hash":"23ed14762efa2750a3f4997c44b80e79de0ba1d77c0e800abad3b85b812775a3","state_version":0,"state":"STARTED","current_node":{"type":"agent","node_id":"agent","job":"test","output":"payload","next_node_id":"wait"},"waiting":{"type":"NONE"},"terminal_hash":null,"latest_event_cursor":null}'
_RUN_PAGE = b'{"items":[{"run_id":"run","public_run_reference":"run1.cnVu","workflow_revision_hash":"23ed14762efa2750a3f4997c44b80e79de0ba1d77c0e800abad3b85b812775a3","state_version":0,"state":"STARTED","current_node":{"type":"agent","node_id":"agent","job":"test","output":"payload","next_node_id":"wait"},"waiting":{"type":"NONE"},"terminal_hash":null,"latest_event_cursor":null}],"next_after":null}'

_OPENAPI_COMPONENT_HASHES = {
    "ActionNodeResource": "34f56da2c841de563ec5de5af70f78444426b43c7e7d3ac533bd63a15b0ee0f1",
    "AgentNodeResource": "05c759950ebf8a290f91623f2b5f2ba1c4e12578c4255672a5081c16199f0608",
    "RunEventResource": "8efab00ff6b627d261bac1b812c3a771990ace42c68069afafb77fe998103e83",
    "RunPageResource": "723426649bc3c5965a38be1c8133d0011707e102b9185f138fdcd1987ef621c9",
    "RunResource": "19dddf38abce9d8ee136d18ae4f8500febdd030f2d9d2af8ec1e914199158ca5",
    "StartRunRequestResource": "4ecc26974cdabc9139f707ef4c499292ffe0556926bd9d986ce980e08dba2477",
    "SubworkflowNodeResource": "2df2bceb718fe14f0ae9a5c7a35ce163267124c91fe8d39b254f04320e149bca",
    "WaitNodeResource": "05d8408dabf5024819527496bbbb4fa327dc5ce679d7c0880d3e8b32edae617f",
    "WorkflowGraphResource": "b36d8c8e5c8257572d71aa7aed952fa966dd737e75ab99a14bf365ed77e4b92e",
}


def _wire(model: BaseModel) -> bytes:
    return model.model_dump_json().encode("utf-8")


def test_v1_workflow_and_run_resources_keep_their_exact_raw_bytes() -> None:
    revision = WorkflowRevision(_DOCUMENT)
    graph = parse_executable_workflow_document(_DOCUMENT)
    detail = workflow_revision_detail_resource(
        WorkflowRevisionProjection(revision, graph)
    )
    page = WorkflowRevisionPageResource(
        items=(WorkflowRevisionSummaryResource(revision_hash=_REVISION_HASH),),
        next_after_revision_hash=None,
    )
    start = StartRunRequestResource(run_id="run", workflow_revision_hash=_REVISION_HASH)
    run = cast(
        RunResource,
        run_resource(
            RunProjection(
                Run(
                    RunId("run"),
                    revision.revision_hash,
                    RunState.STARTED,
                    "agent",
                    0,
                    0,
                ),
                graph,
                None,
            )
        ),
    )

    assert _wire(detail) == _DETAIL
    assert _wire(page) == _PAGE
    assert _wire(start) == _START
    assert _wire(run) == _RUN
    assert _wire(RunPageResource(items=(run,), next_after=None)) == _RUN_PAGE
    assert _wire(VersionedRunPageResource(items=(run,), next_after=None)) == _RUN_PAGE


def test_all_seven_v1_sse_data_resources_keep_their_exact_raw_bytes() -> None:
    adapter = TypeAdapter(RunEventResource)

    encoded = tuple(
        _wire(adapter.validate_python(event["data"])) for event in SSE_COMPLETE_HISTORY
    )

    assert encoded == tuple(
        json.dumps(event["data"], separators=(",", ":")).encode("utf-8")
        for event in SSE_COMPLETE_HISTORY
    )


def test_v1_openapi_components_remain_byte_identical_while_v2_is_added() -> None:
    schemas = create_app(
        source_commit="commit",
        source_tree="tree",
        ports=empty_ports(),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
    ).openapi()["components"]["schemas"]

    actual = {
        name: hashlib.sha256(
            json.dumps(schemas[name], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for name in _OPENAPI_COMPONENT_HASHES
    }

    assert actual == _OPENAPI_COMPONENT_HASHES


def test_b02_leaves_v1_json_and_openapi_components_byte_exact() -> None:
    test_v1_workflow_and_run_resources_keep_their_exact_raw_bytes()
    test_all_seven_v1_sse_data_resources_keep_their_exact_raw_bytes()
    test_v1_openapi_components_remain_byte_identical_while_v2_is_added()
