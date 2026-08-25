from __future__ import annotations

import pytest

from atelier2.adapters.dbos.node_binding_codec import (
    decode_node_binding,
    encode_node_binding,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.bind_node import (
    agent_execution_request,
    agent_execution_request_v2,
    bind_node,
    pinned_project,
    require_the_run_stands_on,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.node_bindings import (
    ActionNodeBinding,
    AgentNodeBinding,
    AgentNodeBindingV2,
    NodeBinding,
    SubworkflowNodeBinding,
    WaitNodeBinding,
)
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.run_bindings import AnyRun, RunBindingConflict, RunV2
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    Run,
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.contracts.tool_grants_v3 import DeclaredToolGrant, ToolGrantCapability
from tests.scenarios.agents import resolved_agent_binding
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN_ID = RunId("bindings/one-run")
NODE_ID = "build"
A_PIN = ProjectSourcePin("a1" * 20, "b2" * 20)
A_LATER_ROUND = 3
A_GRANT = DeclaredToolGrant(
    ANY_JSON_SCHEMA.revision_hash, ToolGrantCapability.RUN_PROJECT_VERIFICATION
)
A_SCHEMA_DOCUMENT = ANY_JSON_SCHEMA.document.decode("utf-8")
V1_DOCUMENT = b"""format_version: 1
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: pause, type: wait, answer_type: integer, next: done}
  - {id: act, type: action, next: pause}
  - {id: build, type: agent, job: build it, output: the draft, next: act}
"""
V2_DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: pause, type: wait, answer_type: integer, next: done}
  - {id: act, type: action, next: pause}
  - {id: build, type: agent, role: builder, job: build it, next: act}
"""
V3_DOCUMENT = b"""format_version: 3
name: One agent in a line
nodes:
  - id: build
    type: agent
    role: builder
    mode: headless
    instruction: build it
""" + declared_output()
V3_ACTION_DOCUMENT = (
    V3_DOCUMENT
    + b"""  - id: act
    type: action
    operation: {ref: open-pr, revision: """
    + (b"a1" * 32)
    + b"""}
    depends_on: [build]
"""
)


def v1_run(node_id: str = NODE_ID, state: RunState = RunState.STARTED) -> Run:
    return Run(
        RUN_ID, WorkflowRevision(V1_DOCUMENT).revision_hash, state, node_id, 0, 0
    )


def v2_run(
    document: bytes = V2_DOCUMENT,
    node_id: str = NODE_ID,
    role: str = "builder",
    round_ordinal: int = FIRST_ROUND_ORDINAL,
) -> RunV2:
    resolved = resolved_agent_binding(role)
    return RunV2(
        RUN_ID,
        WorkflowRevision(document).revision_hash,
        AgentBindingSet(
            (AgentBinding(resolved.role, resolved.configuration.revision_hash),)
        ).binding_set_hash,
        (resolved,),
        RunState.STARTED,
        node_id,
        0,
        0,
        current_round_ordinal=round_ordinal,
    )


def bound(
    run: AnyRun,
    document: bytes,
    node_id: str = NODE_ID,
    *,
    orders: tuple[RunInput, ...] = (),
    tool_grant: DeclaredToolGrant | None = None,
    project_source: ProjectSourcePin | None = None,
    maximum_assistant_turns: int | None = None,
) -> NodeBinding:
    """The binding the durable step would record, in the order the step reads it."""
    require_the_run_stands_on(run, run.revision_hash, node_id)
    node = parse_workflow_document(document).node(node_id)
    return bind_node(
        run,
        node,
        orders=orders,
        tool_grant=tool_grant,
        project_source=project_source,
        maximum_assistant_turns=maximum_assistant_turns,
    )


@pytest.mark.parametrize(
    ("run", "document", "node_id", "expected"),
    (
        (
            v1_run("build"),
            V1_DOCUMENT,
            "build",
            AgentNodeBinding("build it", "the draft"),
        ),
        (v1_run("act"), V1_DOCUMENT, "act", ActionNodeBinding()),
        (v1_run("pause"), V1_DOCUMENT, "pause", WaitNodeBinding()),
        (v1_run("done"), V1_DOCUMENT, "done", SubworkflowNodeBinding((2, 3))),
        (v2_run(node_id="act"), V2_DOCUMENT, "act", ActionNodeBinding()),
        (v2_run(node_id="pause"), V2_DOCUMENT, "pause", WaitNodeBinding()),
        (v2_run(node_id="done"), V2_DOCUMENT, "done", SubworkflowNodeBinding((2, 3))),
        (
            v2_run(V3_ACTION_DOCUMENT, node_id="act"),
            V3_ACTION_DOCUMENT,
            "act",
            ActionNodeBinding(),
        ),
    ),
    ids=[
        "V1 agent",
        "V1 action",
        "V1 wait",
        "V1 subworkflow",
        "V2 action",
        "V2 wait",
        "V2 subworkflow",
        "V3 action",
    ],
)
@pytest.mark.proves("a-node-binding-is-decided-where-no-store-can-be-reached")
def test_each_node_kind_binds_its_own_form_without_a_store(
    run: AnyRun, document: bytes, node_id: str, expected: NodeBinding
) -> None:
    assert bound(run, document, node_id) == expected


@pytest.mark.proves("a-node-binding-is-decided-where-no-store-can-be-reached")
def test_a_wait_node_binds_the_round_its_run_is_turning() -> None:
    """The pause carries the round, so a recovery replays the execution that paused."""
    run = v2_run(node_id="pause", round_ordinal=A_LATER_ROUND)

    assert bound(run, V2_DOCUMENT, "pause") == WaitNodeBinding(A_LATER_ROUND)


@pytest.mark.proves("a-node-binding-is-decided-where-no-store-can-be-reached")
def test_a_v2_agent_node_binds_the_role_matrix_its_run_was_started_with() -> None:
    binding = bound(v2_run(), V2_DOCUMENT, project_source=A_PIN)

    assert binding == AgentNodeBindingV2(
        resolved_agent_binding(), "build it", None, A_PIN
    )


@pytest.mark.proves("a-node-binding-is-decided-where-no-store-can-be-reached")
def test_a_v3_agent_node_binds_through_that_same_form_with_its_material_composed() -> (
    None
):
    order = RunInput("brief", ANY_JSON_SCHEMA.revision_hash, b'"ship it"')

    binding = bound(
        v2_run(V3_DOCUMENT),
        V3_DOCUMENT,
        orders=(order,),
        tool_grant=A_GRANT,
        project_source=A_PIN,
    )

    assert binding == AgentNodeBindingV2(
        resolved_agent_binding(),
        'build it\n\n--- order: brief ---\n\n"ship it"',
        A_GRANT,
        A_PIN,
    )


@pytest.mark.parametrize(
    ("run", "document", "node_id"),
    (
        (v1_run(), V1_DOCUMENT, "act"),
        (v1_run(node_id="pause", state=RunState.WAITING_INPUT), V1_DOCUMENT, "pause"),
        (v1_run(), V2_DOCUMENT, NODE_ID),
    ),
    ids=["another node", "not started", "another revision"],
)
@pytest.mark.proves("a-node-binding-is-decided-where-no-store-can-be-reached")
def test_a_node_the_run_does_not_stand_on_refuses_before_anything_is_read(
    run: AnyRun, document: bytes, node_id: str
) -> None:
    with pytest.raises(RunBindingConflict, match="does not own current STARTED node"):
        require_the_run_stands_on(
            run, WorkflowRevision(document).revision_hash, node_id
        )


@pytest.mark.proves("a-node-binding-is-decided-where-no-store-can-be-reached")
def test_an_agent_node_on_a_v1_run_refuses_rather_than_binding_no_role() -> None:
    run = Run(
        RUN_ID,
        WorkflowRevision(V2_DOCUMENT).revision_hash,
        RunState.STARTED,
        NODE_ID,
        0,
        0,
    )

    with pytest.raises(RunBindingConflict, match="belongs to a V1 run"):
        bound(run, V2_DOCUMENT)


@pytest.mark.proves("a-node-binding-is-decided-where-no-store-can-be-reached")
def test_a_role_the_run_never_bound_refuses_by_the_binding_it_is_missing() -> None:
    with pytest.raises(RunBindingConflict, match="no durable binding"):
        bound(v2_run(role="reviewer"), V2_DOCUMENT)


def test_a_grant_bound_without_a_pinned_source_is_not_a_binding_at_all() -> None:
    """A grant is redeemed against a tree, so a grant with no tree redeems nothing."""
    with pytest.raises(RunBindingConflict, match="pinned none"):
        AgentNodeBindingV2(resolved_agent_binding(), "build it", A_GRANT, None)


@pytest.mark.parametrize(
    "binding",
    (
        AgentNodeBinding("build it", "the draft"),
        AgentNodeBindingV2(resolved_agent_binding(), "build it"),
        AgentNodeBindingV2(
            resolved_agent_binding(), "build it", A_GRANT, A_PIN, A_SCHEMA_DOCUMENT
        ),
        AgentNodeBindingV2(
            resolved_agent_binding(), "build it", round_ordinal=A_LATER_ROUND
        ),
        AgentNodeBindingV2(
            resolved_agent_binding(), "build it", maximum_assistant_turns=8
        ),
        ActionNodeBinding(),
        WaitNodeBinding(),
        WaitNodeBinding(A_LATER_ROUND),
        SubworkflowNodeBinding((2, 3)),
    ),
    ids=[
        "agent",
        "agent-v2",
        "agent-v2 pinned",
        "agent-v2 in a later round",
        "agent-v2 with a turn bound",
        "action",
        "wait",
        "wait in a later round",
        "subworkflow",
    ],
)
@pytest.mark.proves(
    "a-durable-binding-row-is-read-back-only-in-a-shape-this-engine-writes"
)
def test_every_binding_survives_the_round_trip_through_its_durable_form(
    binding: NodeBinding,
) -> None:
    assert decode_node_binding(encode_node_binding(binding)) == binding


@pytest.mark.proves(
    "a-durable-binding-row-is-read-back-only-in-a-shape-this-engine-writes"
)
def test_the_durable_form_of_a_pinned_agent_node_is_the_row_already_written() -> None:
    """The literal row, so a field renamed here cannot silently rewrite storage."""
    resolved = resolved_agent_binding()

    assert encode_node_binding(
        AgentNodeBindingV2(resolved, "build it", A_GRANT, A_PIN, A_SCHEMA_DOCUMENT)
    ) == {
        "type": "agent-v2",
        "role": "builder",
        "job": "build it",
        "round_ordinal": FIRST_ROUND_ORDINAL,
        "configuration_hash": resolved.configuration.revision_hash.value,
        "auth_hash": resolved.auth_profile.revision_hash.value,
        "profile_id": "max",
        "revision_number": 1,
        "provider_id": "anthropic",
        "auth_mode": "subscription",
        "model": "opus",
        "executor_revision": "claude-cli/v1",
        "revision_format_version": 2,
        "requested_capability": "headless",
        "tool_revision_hash": ANY_JSON_SCHEMA.revision_hash.value,
        "tool_capability": "run-project-verification",
        "output_schema_document": A_SCHEMA_DOCUMENT,
        "project_commit": "a1" * 20,
        "project_tree": "b2" * 20,
    }


ABSENT = object()


def written(**changes: object) -> dict[str, object]:
    """A row this codec writes today, changed only in what a case is about."""
    encoded = dict(
        encode_node_binding(AgentNodeBindingV2(resolved_agent_binding(), "build it"))
    )
    for key, value in changes.items():
        if value is ABSENT:
            encoded.pop(key)
        else:
            encoded[key] = value
    return encoded


def legacy_row() -> dict[str, object]:
    """The exact shape written before the configuration contract existed."""
    encoded = dict(
        encode_node_binding(
            AgentNodeBindingV2(
                resolved_agent_binding(
                    revision_format_version=AgentConfigurationRevisionFormatVersion.V1
                ),
                "build it",
            )
        )
    )
    del encoded["revision_format_version"]
    del encoded["requested_capability"]
    return encoded


@pytest.mark.proves(
    "a-durable-binding-row-is-read-back-only-in-a-shape-this-engine-writes"
)
def test_the_one_legacy_row_still_means_headless_under_the_first_format() -> None:
    binding = decode_node_binding(legacy_row())

    assert binding == AgentNodeBindingV2(
        resolved_agent_binding(
            revision_format_version=AgentConfigurationRevisionFormatVersion.V1
        ),
        "build it",
    )


@pytest.mark.proves(
    "a-durable-binding-row-is-read-back-only-in-a-shape-this-engine-writes"
)
def test_a_wait_row_written_before_rounds_existed_still_means_the_first_round() -> None:
    """The exact shape written while a pause could stand in one round only."""
    assert decode_node_binding({"type": "wait"}) == WaitNodeBinding(FIRST_ROUND_ORDINAL)


@pytest.mark.parametrize("round_ordinal", [0, -1], ids=["zero", "negative"])
@pytest.mark.proves("a-node-binding-is-decided-where-no-store-can-be-reached")
def test_a_pause_cannot_be_bound_to_a_round_no_run_can_stand_in(
    round_ordinal: int,
) -> None:
    """The contract that owns the ordinal is what refuses it, not the store's CHECK.

    A binding is built long before any row is written, so a round the schema
    would reject has to be unconstructible here; otherwise the first thing to
    notice would be an integrity error naming a column nobody chose.
    """
    with pytest.raises(ValueError, match="a whole count from 1"):
        WaitNodeBinding(round_ordinal)


@pytest.mark.parametrize("round_ordinal", [0, -1], ids=["zero", "negative"])
@pytest.mark.proves("a-node-binding-is-decided-where-no-store-can-be-reached")
def test_an_agent_node_cannot_be_bound_to_a_round_no_run_can_stand_in(
    round_ordinal: int,
) -> None:
    """The Agent form's sibling of the Wait refusal above, for the same reason."""
    with pytest.raises(ValueError, match="a whole count from 1"):
        AgentNodeBindingV2(
            resolved_agent_binding(), "build it", round_ordinal=round_ordinal
        )


@pytest.mark.parametrize(
    ("encoded", "refusal"),
    (
        ({"type": "agent-v4"}, "names no form this adapter writes"),
        ({"job": "build it"}, "names no form this adapter writes"),
        ({"type": "wait", "answer": 3}, "a key its form does not declare"),
        (
            {"type": "wait", "round_ordinal": 0},
            "a round no run can stand in",
        ),
        (
            {"type": "wait", "round_ordinal": -1},
            "a round no run can stand in",
        ),
        (
            {"type": "wait", "round_ordinal": "2"},
            "round_ordinal as a value of the wrong type",
        ),
        ({"type": "subworkflow", "left": 2}, "a key its form declares"),
        ({"type": "subworkflow", "left": 2, "right": "3"}, "value of the wrong type"),
        (written(requested_capability=ABSENT), "contract is only partly encoded"),
        (written(revision_format_version=ABSENT), "contract is only partly encoded"),
        (written(revision_format_version=True), "value of the wrong type"),
        (written(requested_capability=2), "value of the wrong type"),
        (written(revision_format_version=3), "contract carries an unknown value"),
        (
            written(requested_capability="telepathic"),
            "contract carries an unknown value",
        ),
        (written(auth_mode="handshake"), "auth profile carries an unknown value"),
        (written(model="haiku"), "configuration fields differ from their durable hash"),
        (written(profile_id="nobody"), "auth fields differ from their durable hash"),
        (written(unexpected="whatever"), "a key its form does not declare"),
        (
            written(output_schema_document={"type": "string"}),
            "output schema document carries a value of the wrong type",
        ),
        (
            written(maximum_assistant_turns="8"),
            "maximum_assistant_turns carries a value of the wrong type",
        ),
        (written(role=ABSENT), "a key its form declares"),
        (written(round_ordinal=0), "a round no run can stand in"),
        (written(round_ordinal=-1), "a round no run can stand in"),
    ),
    ids=[
        "unknown form",
        "no form at all",
        "extra key on a closed form",
        "wait round of zero",
        "wait round below zero",
        "wait round of the wrong type",
        "missing key",
        "operand of the wrong type",
        "capability without its version",
        "version without its capability",
        "version of the wrong type",
        "capability of the wrong type",
        "unknown version",
        "unknown capability",
        "unknown auth mode",
        "configuration hash mismatch",
        "auth hash mismatch",
        "unknown key",
        "schema document of the wrong type",
        "turn bound of the wrong type",
        "missing role",
        "agent round of zero",
        "agent round below zero",
    ],
)
@pytest.mark.proves(
    "a-durable-binding-row-is-read-back-only-in-a-shape-this-engine-writes"
)
def test_a_durable_row_the_codec_cannot_answer_for_refuses_by_name(
    encoded: dict[str, object], refusal: str
) -> None:
    with pytest.raises(RunBindingConflict, match=refusal):
        decode_node_binding(encoded)


@pytest.mark.proves("a-node-binding-is-decided-where-no-store-can-be-reached")
def test_a_capability_the_running_executor_does_not_attest_reaches_no_provider() -> (
    None
):
    binding = AgentNodeBindingV2(
        resolved_agent_binding(
            requested_capability=AgentExecutionCapability.INTERACTIVE
        ),
        "build it",
    )

    with pytest.raises(RunBindingConflict, match="runtime executor lacks"):
        agent_execution_request_v2(
            binding,
            RUN_ID,
            WorkflowRevision(V2_DOCUMENT).revision_hash,
            NODE_ID,
            AgentExecutorOperationalIdentity("executor/headless-only"),
            frozenset({AgentExecutionCapability.HEADLESS}),
        )


@pytest.mark.proves("a-pinned-budget-turn-bound-is-the-tool-attempt-ceiling")
def test_a_binding_that_pinned_a_turn_bound_puts_it_on_the_request() -> None:
    binding = bound(v2_run(V3_DOCUMENT), V3_DOCUMENT, maximum_assistant_turns=8)
    unbound = bound(v2_run(V3_DOCUMENT), V3_DOCUMENT)
    assert isinstance(binding, AgentNodeBindingV2)
    assert isinstance(unbound, AgentNodeBindingV2)

    request = agent_execution_request_v2(
        binding,
        RUN_ID,
        WorkflowRevision(V3_DOCUMENT).revision_hash,
        NODE_ID,
        AgentExecutorOperationalIdentity("executor/headless"),
        frozenset({AgentExecutionCapability.HEADLESS}),
    )
    default_request = agent_execution_request_v2(
        unbound,
        RUN_ID,
        WorkflowRevision(V3_DOCUMENT).revision_hash,
        NODE_ID,
        AgentExecutorOperationalIdentity("executor/headless"),
        frozenset({AgentExecutionCapability.HEADLESS}),
    )

    assert binding.maximum_assistant_turns == 8
    assert request.maximum_assistant_turns == 8
    assert unbound.maximum_assistant_turns is None
    assert default_request.maximum_assistant_turns is None
    assert decode_node_binding(encode_node_binding(binding)) == binding
    assert "maximum_assistant_turns" not in encode_node_binding(unbound)


def test_a_v1_binding_composes_the_request_its_exact_output_contract_names() -> None:
    request = agent_execution_request(
        AgentNodeBinding("build it", "the draft"),
        RUN_ID,
        WorkflowRevision(V1_DOCUMENT).revision_hash,
        NODE_ID,
    )

    assert (request.job_bytes, request.exact_output.output_bytes) == (
        b"build it",
        b"the draft",
    )


def test_a_runtime_given_no_project_answers_for_a_binding_that_pinned_none() -> None:
    binding = AgentNodeBindingV2(resolved_agent_binding(), "build it")

    assert pinned_project(binding, None) is None
