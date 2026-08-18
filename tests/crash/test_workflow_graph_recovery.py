from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from atelier2.adapters.dbos.names import (
    ACTION_CHECKPOINT_STEP_NAME,
    AGENT_COMMIT_STEP_NAME,
    ANSWER_COMMIT_STEP_NAME,
    COMMIT_STEP_NAME,
    WAIT_COMMIT_STEP_NAME,
)
from atelier2.adapters.dbos.workflow_ids import (
    action_continuation_workflow_id_for,
    answer_workflow_id_for,
    node_workflow_id_for,
    subworkflow_workflow_id_for,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    logical_effect_key_for,
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, WorkflowRevision
from tests.scenarios.workflows import declared_output

CRASHED = 86
HARNESS = Path(__file__).with_name("workflow_graph_harness.py")
VERSION = "executor-A"
DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: waiting, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: waiting}
  - {id: agent, type: agent, job: job-17, output: draft-17, next: action}
"""
V3_DOCUMENT = (
    b"""format_version: 3
name: Two agents in a line
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + declared_output()
    + b"""  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Check what the node before you did.
    depends_on: [implement]
"""
    + declared_output()
)
V3_WAIT_DOCUMENT = (
    b"""format_version: 3
name: A person approves between two agents
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + declared_output()
    + b"""  - id: approve
    type: wait
    prompt: Approve this candidate, or name the blocking defect.
    depends_on: [implement]
"""
    + declared_output(name="approval")
    + b"""  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Check what the person approved.
    depends_on: [approve]
"""
    + declared_output()
)
V3_RUN_ID = "v3/two-agents/recovery"
V3_WAIT_RUN_ID = "v3/a-person-approves/recovery"
V3_ANSWER = '"approved"'
V3_PROVIDER_OUTPUT = b'"the exact provider bytes"'


def child(
    root: Path,
    command: str,
    *arguments: str,
    expected: int = 0,
    timeout: float = 20,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            sys.executable,
            str(HARNESS),
            command,
            str(root / "atelier.sqlite"),
            str(root / "external.sqlite"),
            VERSION,
            str(root / "force-unknown"),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (
                    str(Path(__file__).parents[2]),
                    str(Path(__file__).parents[2] / "src"),
                )
            ),
        },
    )
    assert result.returncode == expected, result.stderr
    return result


def scalar(root: Path, statement: str, parameters: tuple[str, ...] = ()) -> object:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        row = connection.execute(statement, parameters).fetchone()
    return None if row is None else row[0]


def database_row(
    root: Path,
    database: str,
    statement: str,
    parameters: tuple[str, ...] = (),
) -> tuple[object, ...]:
    with sqlite3.connect(root / database, timeout=30) as connection:
        record = connection.execute(statement, parameters).fetchone()
    assert record is not None
    return tuple(record)


def workflow_identity_rows(root: Path, workflow_id: str) -> tuple[tuple[str, str], ...]:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        return tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT workflow_uuid,name FROM workflow_status WHERE workflow_uuid=?",
                (workflow_id,),
            )
        )


def event_rows(root: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        return tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT run_id,revision_hash,event_sequence,node_id,"
                "node_execution_id,event_kind,payload,payload_hash,"
                "receipt_logical_key,receipt_result_hash,event_hash "
                "FROM run_events ORDER BY event_sequence"
            )
        )


def event_kinds(root: Path) -> tuple[str, ...]:
    return tuple(str(record[5]) for record in event_rows(root))


def c3_expected_event_rows() -> tuple[tuple[object, ...], ...]:
    return (
        (
            "run-1",
            "d62603c276643f3424abd7ea37645aa817540acc37760ede332e8b38f7f0c4d9",
            1,
            "agent",
            "74e87b6f21afce4cc8948bd6d3c504b402a741f4f701e2a552e9f8d21b7c4f00",
            "AGENT_COMPLETED",
            b"draft-17",
            "5057270cddd80f8172eaa4efbb6affdcfe5039f50dd39c7259632337f4dfe5b8",
            None,
            None,
            "a5a39597461533a67385d3eddbfcf764e1efcc71a86ab1ce092d4b9c5b00a635",
        ),
        (
            "run-1",
            "d62603c276643f3424abd7ea37645aa817540acc37760ede332e8b38f7f0c4d9",
            2,
            "action",
            "4db8459b3ffc567337cae15227f49a0df16af6a03ff17020e720eaafa4b9c554",
            "ACTION_RECONCILIATION_REQUIRED",
            b"draft-17",
            "5057270cddd80f8172eaa4efbb6affdcfe5039f50dd39c7259632337f4dfe5b8",
            None,
            None,
            "043dfc754e71706f681f96b357214a767a23e4a16731d05507db4951d3e50382",
        ),
        (
            "run-1",
            "d62603c276643f3424abd7ea37645aa817540acc37760ede332e8b38f7f0c4d9",
            3,
            "action",
            "4db8459b3ffc567337cae15227f49a0df16af6a03ff17020e720eaafa4b9c554",
            "ACTION_RECONCILIATION_RESOLVED",
            b"draft-17",
            "5057270cddd80f8172eaa4efbb6affdcfe5039f50dd39c7259632337f4dfe5b8",
            "atelier2-node-effect-830e7443ae091c398ae0b0728123b4e1f75f39651a9fdc269c96c873df65490f",
            "5057270cddd80f8172eaa4efbb6affdcfe5039f50dd39c7259632337f4dfe5b8",
            "67c006d6c49ab1e9adeae21ea92a5b50264036240b24f860990a4312edb5957b",
        ),
        (
            "run-1",
            "d62603c276643f3424abd7ea37645aa817540acc37760ede332e8b38f7f0c4d9",
            4,
            "action",
            "4db8459b3ffc567337cae15227f49a0df16af6a03ff17020e720eaafa4b9c554",
            "ACTION_COMPLETED",
            b"draft-17",
            "5057270cddd80f8172eaa4efbb6affdcfe5039f50dd39c7259632337f4dfe5b8",
            "atelier2-node-effect-830e7443ae091c398ae0b0728123b4e1f75f39651a9fdc269c96c873df65490f",
            "5057270cddd80f8172eaa4efbb6affdcfe5039f50dd39c7259632337f4dfe5b8",
            "6b7d64e00966f12ad79e08c5843965b5231c67265a45c4d3dbf8213540094351",
        ),
        (
            "run-1",
            "d62603c276643f3424abd7ea37645aa817540acc37760ede332e8b38f7f0c4d9",
            5,
            "waiting",
            "82a3cb9e2cd441b01039e91775bb4a029a268fde87c29ab53c8fea904ce5a9a6",
            "WAITING_INPUT",
            b"",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            None,
            None,
            "b67d3eb6050c7ed4ab80ea28a09ebdcc84c89e357212fb84d29bbe034d957d75",
        ),
        (
            "run-1",
            "d62603c276643f3424abd7ea37645aa817540acc37760ede332e8b38f7f0c4d9",
            6,
            "waiting",
            "82a3cb9e2cd441b01039e91775bb4a029a268fde87c29ab53c8fea904ce5a9a6",
            "WAIT_ANSWERED",
            b"5",
            "ef2d127de37b942baad06145e54b0c619a1f22327b2ebbcfbec78f5564afe39d",
            None,
            None,
            "60982e515369ce96c40d5857eaddc841da72f12e033dbcc1175c5b9a244a0edc",
        ),
        (
            "run-1",
            "d62603c276643f3424abd7ea37645aa817540acc37760ede332e8b38f7f0c4d9",
            7,
            "final",
            "801bed90c38ea7f5852311bfd9e8e088eebe186ff1bf68aa570e75202d1dbe8e",
            "SUBWORKFLOW_COMPLETED",
            b"5",
            "ef2d127de37b942baad06145e54b0c619a1f22327b2ebbcfbec78f5564afe39d",
            None,
            None,
            "38447d90fe8b03244218c46a18cdce1b5185873d198edde08ae2734252f478d7",
        ),
    )


def assert_exact_c3_completed_vector(root: Path) -> None:
    logical_key = (
        "atelier2-node-effect-"
        "830e7443ae091c398ae0b0728123b4e1f75f39651a9fdc269c96c873df65490f"
    )
    result_hash = "5057270cddd80f8172eaa4efbb6affdcfe5039f50dd39c7259632337f4dfe5b8"
    assert database_row(
        root,
        "atelier.sqlite",
        "SELECT state,current_node_id,state_version,last_event_sequence,terminal_hash "
        "FROM runs WHERE run_id='run-1'",
    ) == (
        "COMPLETED",
        "final",
        7,
        7,
        "d7fefb302309bd78e2b7ee6d3f82e7563863587e8e1fa26b43de9a13a120932d",
    )
    assert event_rows(root) == c3_expected_event_rows()
    assert database_row(
        root,
        "atelier.sqlite",
        "SELECT logical_key,effect_id,result,result_hash,confirmation_source,"
        "reconcile_command_id FROM effect_receipts",
    ) == (
        logical_key,
        "9f87f9fd0daa7b7893eec06360d94afb0a195f77efd16494bfe7f6057d00fe96",
        b"draft-17",
        result_hash,
        "OPERATOR_AUTHORIZED_EXECUTION",
        "run-1/reconcile-1",
    )
    assert database_row(
        root,
        "atelier.sqlite",
        "SELECT command_id,logical_key,expected_intent_version,determination,"
        "actor,evidence,found_effect_id,found_result,found_result_hash,state "
        "FROM reconcile_commands",
    ) == (
        "run-1/reconcile-1",
        logical_key,
        1,
        "AUTHORITATIVE_NOT_FOUND",
        "operator",
        "inspected the exact external destination",
        None,
        None,
        None,
        "APPLIED",
    )
    assert database_row(
        root,
        "external.sqlite",
        "SELECT logical_key,canonical_request,request_hash,effect_id,result,result_hash "
        "FROM loopback_effects",
    ) == (
        logical_key,
        b"draft-17",
        result_hash,
        "9f87f9fd0daa7b7893eec06360d94afb0a195f77efd16494bfe7f6057d00fe96",
        b"draft-17",
        result_hash,
    )
    assert database_row(
        root,
        "external.sqlite",
        "SELECT logical_key,calls FROM loopback_effect_calls",
    ) == (logical_key, 1)
    continuation_id = expected_workflow_identities().continuation
    assert database_row(
        root,
        "atelier.sqlite",
        "SELECT workflow_uuid FROM workflow_status "
        "WHERE name='atelier2_action_continuation'",
    ) == (continuation_id,)
    assert scalar(root, "SELECT COUNT(*) FROM effect_receipts") == 1
    assert scalar(root, "SELECT COUNT(*) FROM agent_receipts") == 1
    assert scalar(root, "SELECT COUNT(*) FROM reconcile_commands") == 1
    assert (
        scalar(
            root,
            "SELECT COUNT(*) FROM workflow_status "
            "WHERE name='atelier2_action_continuation'",
        )
        == 1
    )
    assert database_row(
        root,
        "external.sqlite",
        "SELECT COUNT(*) FROM loopback_effects",
    ) == (1,)
    assert database_row(
        root,
        "external.sqlite",
        "SELECT COUNT(*) FROM loopback_effect_calls",
    ) == (1,)


@dataclass(frozen=True)
class ExpectedWorkflowIdentities:
    action_node: str
    continuation: str
    wait_node: str
    answer: str
    final_node: str
    subworkflow: str


def expected_workflow_identities(run_id: str = "run-1") -> ExpectedWorkflowIdentities:
    revision = WorkflowRevision(DOCUMENT)
    typed_run_id = RunId(run_id)
    action_execution = NodeExecutionId.for_node(
        typed_run_id, revision.revision_hash, "action"
    )
    wait_execution = NodeExecutionId.for_node(
        typed_run_id, revision.revision_hash, "waiting"
    )
    final_execution = NodeExecutionId.for_node(
        typed_run_id, revision.revision_hash, "final"
    )
    return ExpectedWorkflowIdentities(
        node_workflow_id_for(action_execution),
        action_continuation_workflow_id_for(logical_effect_key_for(action_execution)),
        node_workflow_id_for(wait_execution),
        answer_workflow_id_for(wait_execution),
        node_workflow_id_for(final_execution),
        subworkflow_workflow_id_for(final_execution),
    )


def initialize_and_seed(root: Path, run_id: str = "run-1") -> None:
    child(root, "initialize")
    child(root, "seed", run_id, DOCUMENT.hex())


def initialize_and_seed_v3(root: Path) -> None:
    child(root, "seed-v3", V3_RUN_ID, V3_DOCUMENT.hex())


def v3_node_workflow_id(node_id: str) -> str:
    revision = WorkflowRevision(V3_DOCUMENT)
    return node_workflow_id_for(
        NodeExecutionId.for_node(RunId(V3_RUN_ID), revision.revision_hash, node_id)
    )


def v3_wait_node_execution(node_id: str) -> NodeExecutionId:
    return NodeExecutionId.for_node(
        RunId(V3_WAIT_RUN_ID), WorkflowRevision(V3_WAIT_DOCUMENT).revision_hash, node_id
    )


def initialize_and_seed_v3_wait(root: Path) -> None:
    child(root, "initialize")
    child(root, "seed-v3", V3_WAIT_RUN_ID, V3_WAIT_DOCUMENT.hex())


def wait_run_row(root: Path) -> tuple[object, ...]:
    return database_row(
        root,
        "atelier.sqlite",
        "SELECT state,current_node_id,last_event_sequence,terminal_hash "
        "FROM runs WHERE run_id=?",
        (V3_WAIT_RUN_ID,),
    )


def provider_call_count(root: Path) -> int:
    counter = root / "provider-count"
    return 0 if not counter.exists() else len(counter.read_bytes())


def integer_scalar(root: Path, statement: str) -> int:
    value = scalar(root, statement)
    assert type(value) is int
    return value


def v3_cardinalities(root: Path) -> tuple[int, int, int, int]:
    return (
        integer_scalar(root, "SELECT COUNT(*) FROM agent_attempts"),
        integer_scalar(root, "SELECT COUNT(*) FROM agent_receipts_v2"),
        integer_scalar(root, "SELECT COUNT(*) FROM run_events"),
        provider_call_count(root),
    )


def drive_unknown_reconciliation_to_wait(root: Path, run_id: str = "run-1") -> None:
    identities = expected_workflow_identities(run_id)
    marker = root / "force-unknown"
    marker.touch()
    child(root, "execute-until-reconcile", run_id, "NONE", "NONE")
    assert scalar(root, "SELECT state FROM runs WHERE run_id=?", (run_id,)) == (
        "WAITING_RECONCILIATION"
    )
    assert event_kinds(root) == (
        "AGENT_COMPLETED",
        "ACTION_RECONCILIATION_REQUIRED",
    )
    assert workflow_identity_rows(root, identities.action_node) == (
        (identities.action_node, "atelier2_graph_node"),
    )
    assert workflow_identity_rows(root, identities.continuation) == ()
    assert workflow_identity_rows(root, identities.wait_node) == ()

    marker.unlink()
    child(root, "reconcile", run_id)
    child(root, "execute-until-wait", run_id, "NONE", "NONE")
    assert scalar(root, "SELECT state FROM runs WHERE run_id=?", (run_id,)) == (
        "WAITING_INPUT"
    )
    assert event_kinds(root) == (
        "AGENT_COMPLETED",
        "ACTION_RECONCILIATION_REQUIRED",
        "ACTION_RECONCILIATION_RESOLVED",
        "ACTION_COMPLETED",
        "WAITING_INPUT",
    )
    assert workflow_identity_rows(root, identities.continuation) == (
        (identities.continuation, "atelier2_action_continuation"),
    )
    assert workflow_identity_rows(root, identities.wait_node) == (
        (identities.wait_node, "atelier2_graph_node"),
    )


def test_answer_commit_crash_recovers_one_child_and_same_terminal_hash(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    crashed = tmp_path / "crashed"
    initialize_and_seed(control)
    drive_unknown_reconciliation_to_wait(control)
    child(control, "answer", "run-1", "waiting", "5")
    child(control, "execute-until-complete", "run-1", "NONE", "NONE")
    assert_exact_c3_completed_vector(control)
    expected_hash = scalar(
        control, "SELECT terminal_hash FROM runs WHERE run_id='run-1'"
    )

    initialize_and_seed(crashed)
    drive_unknown_reconciliation_to_wait(crashed)
    child(crashed, "answer", "run-1", "waiting", "5")
    identities = expected_workflow_identities()
    assert workflow_identity_rows(crashed, identities.answer) == (
        (identities.answer, "atelier2_wait_answer"),
    )
    assert workflow_identity_rows(crashed, identities.final_node) == ()
    assert workflow_identity_rows(crashed, identities.subworkflow) == ()
    marker = crashed / "answer-crash"
    child(
        crashed,
        "execute-until-complete",
        "run-1",
        str(marker),
        ANSWER_COMMIT_STEP_NAME,
        expected=CRASHED,
    )

    assert marker.read_text() == f"{ANSWER_COMMIT_STEP_NAME}:before-record"
    assert scalar(crashed, "SELECT state FROM wait_answers") == "APPLIED"
    assert (
        scalar(
            crashed, "SELECT COUNT(*) FROM run_events WHERE event_kind='WAIT_ANSWERED'"
        )
        == 1
    )
    assert (
        scalar(
            crashed,
            "SELECT COUNT(*) FROM run_events WHERE event_kind='SUBWORKFLOW_COMPLETED'",
        )
        == 0
    )
    assert scalar(crashed, "SELECT terminal_hash FROM runs") is None
    assert workflow_identity_rows(crashed, identities.final_node) == ()
    assert workflow_identity_rows(crashed, identities.subworkflow) == ()

    child(crashed, "execute-until-complete", "run-1", "NONE", "NONE")
    assert_exact_c3_completed_vector(crashed)

    assert (
        scalar(
            crashed, "SELECT COUNT(*) FROM run_events WHERE event_kind='WAIT_ANSWERED'"
        )
        == 1
    )
    assert (
        scalar(
            crashed,
            "SELECT COUNT(*) FROM run_events WHERE event_kind='SUBWORKFLOW_COMPLETED'",
        )
        == 1
    )
    assert scalar(crashed, "SELECT terminal_hash FROM runs") == expected_hash
    assert expected_hash is not None
    assert event_rows(crashed) == event_rows(control)
    assert len(event_rows(crashed)) == 7
    assert workflow_identity_rows(crashed, identities.answer) == (
        (identities.answer, "atelier2_wait_answer"),
    )
    assert workflow_identity_rows(crashed, identities.final_node) == (
        (identities.final_node, "atelier2_graph_node"),
    )
    assert workflow_identity_rows(crashed, identities.subworkflow) == (
        (identities.subworkflow, "atelier2_add_subworkflow"),
    )


def test_effect_commit_crash_recovers_one_action_continuation(tmp_path: Path) -> None:
    initialize_and_seed(tmp_path)
    marker = tmp_path / "effect-crash"

    child(
        tmp_path,
        "execute-until-wait",
        "run-1",
        str(marker),
        COMMIT_STEP_NAME,
        expected=CRASHED,
    )

    assert scalar(tmp_path, "SELECT COUNT(*) FROM effect_receipts") == 1
    assert (
        scalar(
            tmp_path,
            "SELECT COUNT(*) FROM run_events WHERE event_kind='ACTION_COMPLETED'",
        )
        == 0
    )
    identities = expected_workflow_identities()
    assert workflow_identity_rows(tmp_path, identities.continuation) == ()
    child(tmp_path, "execute-until-wait", "run-1", "NONE", "NONE")
    assert (
        scalar(
            tmp_path,
            "SELECT COUNT(*) FROM run_events WHERE event_kind='ACTION_COMPLETED'",
        )
        == 1
    )
    assert (
        scalar(
            tmp_path,
            "SELECT COUNT(*) FROM workflow_status WHERE name='atelier2_action_continuation'",
        )
        == 1
    )
    assert workflow_identity_rows(tmp_path, identities.continuation) == (
        (identities.continuation, "atelier2_action_continuation"),
    )


def test_agent_commit_crash_recovers_one_receipt_event_and_successor(
    tmp_path: Path,
) -> None:
    initialize_and_seed(tmp_path)
    marker = tmp_path / "agent-crash"

    child(
        tmp_path,
        "execute-until-wait",
        "run-1",
        str(marker),
        AGENT_COMMIT_STEP_NAME,
        expected=CRASHED,
    )

    assert marker.read_text() == f"{AGENT_COMMIT_STEP_NAME}:before-record"
    assert scalar(tmp_path, "SELECT COUNT(*) FROM agent_receipts") == 1
    assert (
        scalar(
            tmp_path,
            "SELECT COUNT(*) FROM run_events WHERE event_kind='AGENT_COMPLETED'",
        )
        == 1
    )
    child(tmp_path, "execute-until-wait", "run-1", "NONE", "NONE")
    assert scalar(tmp_path, "SELECT COUNT(*) FROM agent_receipts") == 1
    assert (
        scalar(
            tmp_path,
            "SELECT COUNT(*) FROM run_events WHERE event_kind='AGENT_COMPLETED'",
        )
        == 1
    )
    assert scalar(tmp_path, "SELECT COUNT(*) FROM effect_receipts") == 1
    assert scalar(tmp_path, "SELECT state FROM runs") == "WAITING_INPUT"


def test_action_checkpoint_crash_recovers_one_successor(tmp_path: Path) -> None:
    initialize_and_seed(tmp_path)
    marker = tmp_path / "action-crash"

    child(
        tmp_path,
        "execute-until-wait",
        "run-1",
        str(marker),
        ACTION_CHECKPOINT_STEP_NAME,
        expected=CRASHED,
    )

    assert (
        scalar(
            tmp_path,
            "SELECT COUNT(*) FROM run_events WHERE event_kind='ACTION_COMPLETED'",
        )
        == 1
    )
    assert (
        scalar(
            tmp_path, "SELECT COUNT(*) FROM run_events WHERE event_kind='WAITING_INPUT'"
        )
        == 0
    )
    identities = expected_workflow_identities()
    assert workflow_identity_rows(tmp_path, identities.wait_node) == ()
    child(tmp_path, "execute-until-wait", "run-1", "NONE", "NONE")
    assert (
        scalar(
            tmp_path, "SELECT COUNT(*) FROM run_events WHERE event_kind='WAITING_INPUT'"
        )
        == 1
    )
    assert (
        scalar(
            tmp_path,
            "SELECT COUNT(*) FROM workflow_status WHERE name='atelier2_graph_node'",
        )
        == 3
    )
    assert workflow_identity_rows(tmp_path, identities.wait_node) == (
        (identities.wait_node, "atelier2_graph_node"),
    )


def test_v3_completed_attempt_reentry_starts_its_heir_exactly_once(
    tmp_path: Path,
) -> None:
    initialize_and_seed_v3(tmp_path)
    marker = tmp_path / "v3-successor-crash"

    child(
        tmp_path,
        "execute-v3-until-complete",
        V3_RUN_ID,
        str(marker),
        "start-successor:review",
        expected=CRASHED,
    )

    assert marker.read_text() == "start-successor:before-record"
    assert database_row(
        tmp_path,
        "atelier.sqlite",
        "SELECT state,current_node_id,last_event_sequence,terminal_hash "
        "FROM runs WHERE run_id=?",
        (V3_RUN_ID,),
    ) == ("STARTED", "review", 1, None)
    assert v3_cardinalities(tmp_path) == (1, 1, 1, 1)
    assert database_row(
        tmp_path,
        "atelier.sqlite",
        "SELECT node_id,state FROM agent_attempts",
    ) == ("implement", "SUCCEEDED")
    assert workflow_identity_rows(tmp_path, v3_node_workflow_id("implement")) == (
        (v3_node_workflow_id("implement"), "atelier2_graph_node"),
    )
    assert workflow_identity_rows(tmp_path, v3_node_workflow_id("review")) == ()

    child(
        tmp_path,
        "execute-v3-until-complete",
        V3_RUN_ID,
        "NONE",
        "NONE",
    )

    assert v3_cardinalities(tmp_path) == (2, 2, 2, 2)
    with sqlite3.connect(tmp_path / "atelier.sqlite", timeout=30) as connection:
        attempts = tuple(
            connection.execute(
                "SELECT node_id,state FROM agent_attempts ORDER BY node_id"
            )
        )
        events = tuple(
            connection.execute(
                "SELECT event_sequence,node_id,event_kind,payload "
                "FROM run_events ORDER BY event_sequence"
            )
        )
        event_hashes = tuple(
            Sha256Hash(str(row[0]))
            for row in connection.execute(
                "SELECT event_hash FROM run_events ORDER BY event_sequence"
            )
        )
    assert attempts == (("implement", "SUCCEEDED"), ("review", "SUCCEEDED"))
    assert events == (
        (1, "implement", "AGENT_COMPLETED", V3_PROVIDER_OUTPUT),
        (2, "review", "AGENT_COMPLETED", V3_PROVIDER_OUTPUT),
    )
    assert workflow_identity_rows(tmp_path, v3_node_workflow_id("implement")) == (
        (v3_node_workflow_id("implement"), "atelier2_graph_node"),
    )
    assert workflow_identity_rows(tmp_path, v3_node_workflow_id("review")) == (
        (v3_node_workflow_id("review"), "atelier2_graph_node"),
    )
    terminal_hash = terminal_hash_for(
        WorkflowRevision(V3_DOCUMENT).revision_hash, event_hashes
    ).value
    assert database_row(
        tmp_path,
        "atelier.sqlite",
        "SELECT state,current_node_id,last_event_sequence,terminal_hash "
        "FROM runs WHERE run_id=?",
        (V3_RUN_ID,),
    ) == ("COMPLETED", "review", 2, terminal_hash)


@pytest.mark.parametrize("torn_head", ["implement", "not-in-the-graph"])
def test_v3_completed_attempt_reentry_refuses_a_torn_or_foreign_head(
    tmp_path: Path, torn_head: str
) -> None:
    initialize_and_seed_v3(tmp_path)
    marker = tmp_path / "v3-successor-crash"
    child(
        tmp_path,
        "execute-v3-until-complete",
        V3_RUN_ID,
        str(marker),
        "start-successor:review",
        expected=CRASHED,
    )
    before = v3_cardinalities(tmp_path)
    assert before == (1, 1, 1, 1)
    with sqlite3.connect(tmp_path / "atelier.sqlite", timeout=30) as connection:
        connection.execute(
            "UPDATE runs SET current_node_id=? WHERE run_id=?",
            (torn_head, V3_RUN_ID),
        )
        connection.commit()

    recovery = child(
        tmp_path,
        "execute-v3-until-complete",
        V3_RUN_ID,
        "NONE",
        "NONE",
        expected=1,
    )

    assert v3_cardinalities(tmp_path) == before
    assert workflow_identity_rows(tmp_path, v3_node_workflow_id("review")) == ()
    assert "RunTransitionConflict" in recovery.stderr
    expected_message = (
        "successful attempt has no exact successor transition"
        if torn_head == "implement"
        else "run current node is absent from its workflow graph"
    )
    assert expected_message in recovery.stderr


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_a_v3_wait_survives_the_death_of_the_process_that_reached_it(
    tmp_path: Path,
) -> None:
    """A stopped run is stopped across a restart, and one answer still ends it.

    The process is killed inside the transition that writes the pause, before its
    step result is recorded -- the worst moment, because a recovery that replayed
    it blindly would write the pause twice or drive the line past a person nobody
    asked. What the restart must produce is exactly one WAITING_INPUT, a run still
    standing on the wait node with no attempt started beyond it, and a queue with
    nothing left to run: waiting is a state, not a paused piece of work.

    Then the answer arrives, as it would have before the crash, and carries the
    line to its terminal hash. Durability that only survives until someone tries
    to use it is not durability, so the finish is part of the same case.
    """
    initialize_and_seed_v3_wait(tmp_path)
    marker = tmp_path / "v3-wait-crash"

    child(
        tmp_path,
        "execute-v3-until-wait",
        V3_WAIT_RUN_ID,
        str(marker),
        WAIT_COMMIT_STEP_NAME,
        expected=CRASHED,
    )

    assert marker.read_text() == f"{WAIT_COMMIT_STEP_NAME}:before-record"

    child(tmp_path, "execute-v3-until-wait", V3_WAIT_RUN_ID, "NONE", "NONE")

    assert wait_run_row(tmp_path) == ("WAITING_INPUT", "approve", 2, None)
    assert event_kinds(tmp_path) == ("AGENT_COMPLETED", "WAITING_INPUT")
    assert v3_cardinalities(tmp_path) == (1, 1, 2, 1)
    assert (
        workflow_identity_rows(
            tmp_path, node_workflow_id_for(v3_wait_node_execution("review"))
        )
        == ()
    )

    child(tmp_path, "answer", V3_WAIT_RUN_ID, "approve", V3_ANSWER)
    child(tmp_path, "execute-v3-until-complete", V3_WAIT_RUN_ID, "NONE", "NONE")

    with sqlite3.connect(tmp_path / "atelier.sqlite", timeout=30) as connection:
        events = tuple(
            connection.execute(
                "SELECT event_sequence,node_id,event_kind,payload "
                "FROM run_events ORDER BY event_sequence"
            )
        )
        event_hashes = tuple(
            Sha256Hash(str(row[0]))
            for row in connection.execute(
                "SELECT event_hash FROM run_events ORDER BY event_sequence"
            )
        )
    assert events == (
        (1, "implement", "AGENT_COMPLETED", V3_PROVIDER_OUTPUT),
        (2, "approve", "WAITING_INPUT", b""),
        (3, "approve", "WAIT_ANSWERED", V3_ANSWER.encode("utf-8")),
        (4, "review", "AGENT_COMPLETED", V3_PROVIDER_OUTPUT),
    )
    assert wait_run_row(tmp_path) == (
        "COMPLETED",
        "review",
        4,
        terminal_hash_for(
            WorkflowRevision(V3_WAIT_DOCUMENT).revision_hash, event_hashes
        ).value,
    )
    assert scalar(tmp_path, "SELECT state FROM wait_answers") == "APPLIED"
    answer_identity = answer_workflow_id_for(v3_wait_node_execution("approve"))
    assert workflow_identity_rows(tmp_path, answer_identity) == (
        (answer_identity, "atelier2_wait_answer"),
    )
