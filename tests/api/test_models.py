from __future__ import annotations

from typing import Literal

import pytest
from pydantic import TypeAdapter, ValidationError

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.projection.workflows import graph_resource
from atelier2.api.wire.events import (
    AgentCompletedEventResourceV3,
    RunEventResourceV3,
)
from atelier2.api.wire.resources import (
    AgentConfigurationRevisionListItemResource,
    NodeRailResource,
    RunCancellabilityResource,
    RunResourceV3,
    WaitAnswerSchemaResourceV3,
    WorkflowDeclaredSchemaResourceV3,
    WorkflowGraphResourceV3,
    WorkflowLoopVerdictResourceV3,
    WorkflowNodePreviewResourceV3,
)
from atelier2.application.read_workflow_revisions import WaitAnswerClassification
from atelier2.contracts.run_projections import NodeState
from tests.scenarios.workflows import (
    V3_DOCUMENT,
    VERDICT_LOOP_DOCUMENT,
    VERDICT_LOOP_MAXIMUM_ROUNDS,
)

HASH = "0" * 64


def _wait_preview(node_id: str) -> WorkflowNodePreviewResourceV3:
    return WorkflowNodePreviewResourceV3(
        id=node_id, kind="wait", role=None, instruction_start=None, depends_on=()
    )


def _wait_answer_schema(
    node_id: str, kind: Literal["boolean", "enum", "free"] = "free"
) -> WaitAnswerSchemaResourceV3:
    return WaitAnswerSchemaResourceV3(
        node_id=node_id,
        schema=WorkflowDeclaredSchemaResourceV3(
            ref="decision", revision="schema-decision"
        ),
        kind=kind,
        string_typed=False,
        values=("true", "false") if kind == "enum" else None,
    )


EXECUTION = "1" * 64


def test_listed_agent_configuration_requires_an_honest_startability_pair() -> None:
    item = {
        "model": "model",
        "auth_profile_revision_hash": HASH,
        "executor_revision": "executor/v1",
        "provider_id": "provider",
        "auth_mode": "subscription",
        "requested_capability": "headless",
        "agent_configuration_revision_hash": EXECUTION,
    }

    assert AgentConfigurationRevisionListItemResource.model_validate(
        {
            **item,
            "startable": True,
            "structurally_startable": True,
            "not_startable_reason": None,
        }
    ).startable
    assert not AgentConfigurationRevisionListItemResource.model_validate(
        {
            **item,
            "startable": False,
            "structurally_startable": False,
            "not_startable_reason": "agent-executor-binding-unavailable",
        }
    ).startable
    assert not AgentConfigurationRevisionListItemResource.model_validate(
        {
            **item,
            "startable": False,
            "structurally_startable": True,
            "not_startable_reason": "provider-probe-receipt-missing",
        }
    ).startable
    for startable, structurally_startable, reason in (
        # startable without its own structural answer is dishonest even if
        # the reason field is otherwise well-formed.
        (True, False, None),
        # startable claims a reason anyway.
        (True, True, "agent-executor-binding-unavailable"),
        # unstartable with no reason at all.
        (False, False, None),
        # structurally ready but naming the executor reason, not the
        # receipt one -- the exact confusion the split exists to prevent.
        (False, True, "agent-executor-binding-unavailable"),
        # structurally unavailable but naming the receipt reason instead.
        (False, False, "provider-probe-receipt-missing"),
    ):
        with pytest.raises(ValidationError):
            AgentConfigurationRevisionListItemResource.model_validate(
                {
                    **item,
                    "startable": startable,
                    "structurally_startable": structurally_startable,
                    "not_startable_reason": reason,
                }
            )


def a_run(**overrides: object) -> dict[str, object]:
    """One started run's fields, so a case names only what it changes."""

    return {
        "workflow_format_version": 3,
        "run_id": "run",
        "public_run_reference": "run1.cnVu",
        "workflow_revision_hash": HASH,
        "workflow_name": "workflow",
        "agent_binding_set_hash": HASH,
        "run_configuration_revision_hash": HASH,
        "agent_bindings": (),
        "orders": (),
        "state_version": 2,
        "state": "STARTED",
        "current_node_id": "implement",
        "current_node_execution_id": EXECUTION,
        "node_rail": (
            NodeRailResource(
                node_id="implement", state=NodeState.WORKING, attempt=None
            ),
        ),
        "cancellation": RunCancellabilityResource(
            cancellable=False, reason="between-nodes", target_node_execution_id=None
        ),
        "terminal_hash": None,
        "latest_event_cursor": None,
        **overrides,
    }


@pytest.mark.parametrize(
    ("state", "terminal_hash"),
    [
        ("STARTED", None),
        ("WAITING_INPUT", None),
        ("WAITING_RECONCILIATION", None),
        ("COMPLETED", HASH),
        ("FAILED", HASH),
        ("CANCELLED", HASH),
    ],
)
def test_run_resource_accepts_each_complete_state_shape(
    state: str, terminal_hash: str | None
) -> None:
    resource = RunResourceV3.model_validate(
        a_run(state=state, terminal_hash=terminal_hash)
    )

    assert resource.state == state


@pytest.mark.parametrize(
    ("state", "terminal_hash"),
    [
        ("STARTED", HASH),
        ("WAITING_INPUT", HASH),
        ("COMPLETED", None),
        ("FAILED", None),
        ("CANCELLED", None),
    ],
)
def test_run_resource_rejects_a_terminal_hash_its_state_disagrees_with(
    state: str, terminal_hash: str | None
) -> None:
    """A terminal hash exists exactly when the run has ended, and never before."""

    with pytest.raises(ValidationError):
        RunResourceV3.model_validate(a_run(state=state, terminal_hash=terminal_hash))


def _agent_preview(
    node_id: str,
    *,
    role: str = "builder",
    depends_on: tuple[str, ...] = (),
) -> WorkflowNodePreviewResourceV3:
    return WorkflowNodePreviewResourceV3(
        id=node_id,
        kind="agent",
        role=role,
        instruction_start="Do the one thing this chain is for.",
        depends_on=depends_on,
    )


def test_v3_node_preview_carries_the_authored_depends_on() -> None:
    preview = _agent_preview("review", depends_on=("implement",))

    assert preview.depends_on == ("implement",)


def test_v3_graph_accepts_depends_on_that_names_a_sibling_preview() -> None:
    resource = WorkflowGraphResourceV3(
        workflow_format_version=3,
        executable=True,
        not_executable_reason=None,
        node_count=2,
        agent_roles=("builder",),
        orders=(),
        wait_answer_schemas=(),
        node_previews=(
            _agent_preview("implement"),
            _agent_preview("review", depends_on=("implement",)),
        ),
        loops=(),
        name="Two agents in a line",
        description=None,
    )

    assert resource.node_previews[1].depends_on == ("implement",)


def test_v3_graph_accepts_an_entry_preview_with_no_edges() -> None:
    resource = WorkflowGraphResourceV3(
        workflow_format_version=3,
        executable=True,
        not_executable_reason=None,
        node_count=1,
        agent_roles=("builder",),
        orders=(),
        wait_answer_schemas=(),
        node_previews=(_agent_preview("implement"),),
        loops=(),
        name="One agent",
        description=None,
    )

    assert resource.node_previews[0].depends_on == ()


def test_v3_graph_refuses_a_depends_on_that_names_no_preview() -> None:
    with pytest.raises(ValidationError, match="depends_on"):
        WorkflowGraphResourceV3(
            workflow_format_version=3,
            executable=True,
            not_executable_reason=None,
            node_count=1,
            agent_roles=("builder",),
            orders=(),
            wait_answer_schemas=(),
            node_previews=(_agent_preview("review", depends_on=("implement",)),),
            loops=(),
            name="Broken edge",
            description=None,
        )


def test_v3_graph_projection_carries_a_declared_loop_and_its_verdict() -> None:
    graph = parse_workflow_document(VERDICT_LOOP_DOCUMENT)

    resource = graph_resource(graph, None)

    assert isinstance(resource, WorkflowGraphResourceV3)
    assert len(resource.loops) == 1
    loop = resource.loops[0]
    assert loop.id == "until_reviewed"
    assert loop.member_node_ids == ("implement", "review")
    assert loop.maximum_rounds == VERDICT_LOOP_MAXIMUM_ROUNDS
    assert loop.repeat_while == WorkflowLoopVerdictResourceV3(
        node="review", verdict="revise"
    )


def test_v3_graph_projection_carries_no_loops_when_the_document_declares_none() -> None:
    graph = parse_workflow_document(V3_DOCUMENT)

    resource = graph_resource(graph, None)

    assert isinstance(resource, WorkflowGraphResourceV3)
    assert resource.loops == ()


def test_v3_graph_projection_names_a_wait_nodes_schema_hull_unresolved() -> None:
    """Reading the schema's own bytes to say `boolean` or `enum` needs a
    `PublishedRevisionResolver` read the API layer may not make itself by
    matching a port record (`api-port-record-problems`,
    `scripts/check_architecture.py`) -- `atelier2.application.read_workflow_revisions`
    does that read and hands this projection the plain verdict. A caller that
    supplies none (or one that names no entry for this node) gets the hull
    unresolved, exactly as `orders` already does, and classifies `free`."""
    document = b"""format_version: 3
name: Approve or send back
nodes:
  - id: approve
    type: wait
    prompt: Approve the candidate or send it back.
    outputs:
      - name: decision
        schema: {ref: decision, revision: schema-decision}
"""
    graph = parse_workflow_document(document)

    resource = graph_resource(graph, None)

    assert isinstance(resource, WorkflowGraphResourceV3)
    assert resource.wait_answer_schemas == (
        WaitAnswerSchemaResourceV3(
            node_id="approve",
            schema=WorkflowDeclaredSchemaResourceV3(
                ref="decision", revision="schema-decision"
            ),
            kind="free",
            string_typed=False,
            values=None,
        ),
    )


def test_v3_graph_projection_applies_a_supplied_wait_answer_classification() -> None:
    """The projection never resolves a schema itself; it only ever matches an
    already-resolved verdict by node id, one wait node classifying and the
    other -- not named by any supplied verdict -- falling back to `free`."""
    document = b"""format_version: 3
name: Ship or hold, or say why
nodes:
  - id: ship
    type: wait
    prompt: Ship it?
    outputs:
      - name: decision
        schema: {ref: decision, revision: schema-decision}
  - id: reason
    type: wait
    prompt: Say why, freely.
    outputs:
      - name: note
        schema: {ref: note, revision: schema-note}
"""
    graph = parse_workflow_document(document)

    resource = graph_resource(
        graph,
        None,
        (WaitAnswerClassification(node_id="ship", kind="boolean", string_typed=False),),
    )

    assert isinstance(resource, WorkflowGraphResourceV3)
    by_node_id = {entry.node_id: entry for entry in resource.wait_answer_schemas}
    assert by_node_id["ship"].kind == "boolean"
    assert by_node_id["ship"].values is None
    assert by_node_id["reason"].kind == "free"


def test_v3_graph_projection_applies_a_supplied_enum_classification_with_its_values() -> (
    None
):
    document = b"""format_version: 3
name: Approve or revise
nodes:
  - id: verdict
    type: wait
    prompt: Approve or revise?
    outputs:
      - name: decision
        schema: {ref: decision, revision: schema-decision}
"""
    graph = parse_workflow_document(document)

    resource = graph_resource(
        graph,
        None,
        (
            WaitAnswerClassification(
                node_id="verdict",
                kind="enum",
                string_typed=False,
                values=('"approve"', '"revise"'),
            ),
        ),
    )

    assert isinstance(resource, WorkflowGraphResourceV3)
    entry = resource.wait_answer_schemas[0]
    assert entry.kind == "enum"
    assert entry.string_typed is False
    assert entry.values == ('"approve"', '"revise"')


def test_v3_graph_projection_carries_a_string_typed_enums_raw_values_and_flag() -> None:
    """#1091 PR #1108 finding 1: a `type: string` schema's `enum` classifies
    with `string_typed=True` and raw (never JSON-quoted) `values` -- the
    projection passes both straight through from the application's own
    classification, exactly as it does for every other classified field."""
    document = b"""format_version: 3
name: Decide among named words
nodes:
  - id: verdict
    type: wait
    prompt: ja, nein, or zeig-mir?
    outputs:
      - name: decision
        schema: {ref: decision, revision: schema-decision}
"""
    graph = parse_workflow_document(document)

    resource = graph_resource(
        graph,
        None,
        (
            WaitAnswerClassification(
                node_id="verdict",
                kind="enum",
                string_typed=True,
                values=("ja", "nein", "zeig-mir"),
            ),
        ),
    )

    assert isinstance(resource, WorkflowGraphResourceV3)
    entry = resource.wait_answer_schemas[0]
    assert entry.kind == "enum"
    assert entry.string_typed is True
    assert entry.values == ("ja", "nein", "zeig-mir")


def test_wait_answer_schema_requires_values_exactly_for_an_enum_kind() -> None:
    with pytest.raises(ValidationError):
        WaitAnswerSchemaResourceV3(
            node_id="approve",
            schema=WorkflowDeclaredSchemaResourceV3(ref="decision", revision="hash"),
            kind="enum",
            string_typed=False,
            values=None,
        )
    with pytest.raises(ValidationError):
        WaitAnswerSchemaResourceV3(
            node_id="approve",
            schema=WorkflowDeclaredSchemaResourceV3(ref="decision", revision="hash"),
            kind="boolean",
            string_typed=False,
            values=("true",),
        )


def test_v3_graph_accepts_a_wait_preview_with_its_matching_answer_schema() -> None:
    resource = WorkflowGraphResourceV3(
        workflow_format_version=3,
        executable=False,
        not_executable_reason="waits for a runtime that binds waits",
        node_count=1,
        agent_roles=(),
        orders=(),
        wait_answer_schemas=(_wait_answer_schema("approve", kind="enum"),),
        node_previews=(_wait_preview("approve"),),
        loops=(),
        name="One wait",
        description=None,
    )

    assert resource.wait_answer_schemas[0].node_id == "approve"


def test_v3_graph_refuses_a_wait_answer_schema_naming_no_wait_preview() -> None:
    with pytest.raises(ValidationError, match="answer schema"):
        WorkflowGraphResourceV3(
            workflow_format_version=3,
            executable=True,
            not_executable_reason=None,
            node_count=1,
            agent_roles=("builder",),
            orders=(),
            wait_answer_schemas=(_wait_answer_schema("approve"),),
            node_previews=(_agent_preview("other"),),
            loops=(),
            name="Mismatched wait answer schema",
            description=None,
        )


def test_models_are_frozen_strict_and_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        WorkflowNodePreviewResourceV3.model_validate(
            {
                "id": "implement",
                "kind": "agent",
                "role": "builder",
                "instruction_start": "Do the one thing this chain is for.",
                "depends_on": (),
                "provider": "forbidden",
            }
        )
    with pytest.raises(ValidationError):
        RunResourceV3.model_validate(a_run(state_version=True))


def test_frozen_model_rejects_an_actual_assignment() -> None:
    resource = _agent_preview("implement")

    with pytest.raises(ValidationError, match="frozen_instance"):
        resource.role = "mutated"

    assert resource.role == "builder"


def an_agent_completion(**overrides: object) -> dict[str, object]:
    """One published completion event, so a case names only what it changes."""

    return {
        "workflow_format_version": 3,
        "event": "AGENT_COMPLETED",
        "cursor": "event1.cnVu.1",
        "sequence": 1,
        "public_run_reference": "run1.cnVu",
        "workflow_revision_hash": HASH,
        "node_id": "implement",
        "node_execution_id": EXECUTION,
        "event_hash": HASH,
        "node_rail": (
            NodeRailResource(
                node_id="implement", state=NodeState.WORKING, attempt=None
            ),
        ),
        "output_base64": "cGF5bG9hZA==",
        "output_hash": HASH,
        "attempt_id": HASH,
        "attempt_ordinal": 1,
        **overrides,
    }


def test_event_union_forbids_fields_from_another_variant() -> None:
    with pytest.raises(ValidationError):
        AgentCompletedEventResourceV3.model_validate(an_agent_completion(receipt={}))


@pytest.mark.parametrize("discriminator", [None, "UNKNOWN", 17])
def test_event_union_rejects_missing_unknown_and_non_string_discriminators(
    discriminator: object,
) -> None:
    candidate = an_agent_completion(event=discriminator)
    if discriminator is None:
        del candidate["event"]

    with pytest.raises(ValidationError):
        TypeAdapter(RunEventResourceV3).validate_python(candidate)
