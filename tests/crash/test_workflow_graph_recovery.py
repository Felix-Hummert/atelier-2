from __future__ import annotations

import base64
import os
import pickle
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from atelier2.adapters.dbos.names import (
    ACTION_CHECKPOINT_STEP_NAME,
    ANSWER_COMMIT_STEP_NAME,
    COMMIT_STEP_NAME,
    NODE_BINDING_STEP_NAME,
    WAIT_COMMIT_STEP_NAME,
)
from atelier2.adapters.dbos.workflow_ids import (
    action_continuation_workflow_id_for,
    answer_workflow_id_for,
    effect_workflow_id_for,
    node_workflow_id_for,
)
from atelier2.contracts.agents import AgentConfigurationRevisionFormatVersion
from atelier2.contracts.executions import (
    NodeExecutionId,
    logical_effect_key_for,
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, WorkflowRevision
from tests.crash.workflow_graph_harness import OPEN_PR_GRANT, OPEN_PR_OPERATION
from tests.scenarios.workflows import declared_output

CRASHED = 86
HARNESS = Path(__file__).with_name("workflow_graph_harness.py")
VERSION = "executor-A"
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
V3_ACTION_DOCUMENT = (
    b"""format_version: 3
name: An agent hands its candidate to a platform action
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Write the candidate this chain hands on.
"""
    + declared_output()
    + f"""  - id: publish
    type: action
    operation:
      ref: open-pr
      revision: {OPEN_PR_OPERATION.revision_hash.value}
    depends_on: [implement]
    inputs:
      - name: body
        from: {{node: implement, output: result}}
""".encode()
)
V3_AGENT_OPEN_PR_DOCUMENT = (
    b"""format_version: 3
name: An agent opens a pull request before it advances
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Open the pull request this node earned.
    tools:
      - {ref: open-pr, revision: """
    + OPEN_PR_GRANT.revision_hash.value.encode()
    + b"""}
"""
    + declared_output()
)
V3_RUN_ID = "v3/two-agents/recovery"
V3_ACTION_RUN_ID = "v3/agent-then-action/recovery"
V3_AGENT_OPEN_PR_RUN_ID = "v3/agent-open-pr/recovery"
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


def initialize_and_seed_v3(
    root: Path,
    configuration_format: AgentConfigurationRevisionFormatVersion = (
        AgentConfigurationRevisionFormatVersion.V2
    ),
) -> None:
    child(root, "initialize")
    child(root, "seed-v3", V3_RUN_ID, V3_DOCUMENT.hex(), str(int(configuration_format)))


def v3_node_workflow_id(node_id: str) -> str:
    revision = WorkflowRevision(V3_DOCUMENT)
    return node_workflow_id_for(
        NodeExecutionId.for_node(RunId(V3_RUN_ID), revision.revision_hash, node_id)
    )


def initialize_and_seed_v3_action(root: Path) -> None:
    child(root, "initialize")
    child(
        root,
        "seed-v3",
        V3_ACTION_RUN_ID,
        V3_ACTION_DOCUMENT.hex(),
        str(int(AgentConfigurationRevisionFormatVersion.V2)),
    )


def initialize_and_seed_v3_agent_open_pr(root: Path) -> None:
    child(root, "initialize")
    child(
        root,
        "seed-v3",
        V3_AGENT_OPEN_PR_RUN_ID,
        V3_AGENT_OPEN_PR_DOCUMENT.hex(),
        str(int(AgentConfigurationRevisionFormatVersion.V2)),
    )


def v3_action_node_execution(node_id: str) -> NodeExecutionId:
    return NodeExecutionId.for_node(
        RunId(V3_ACTION_RUN_ID),
        WorkflowRevision(V3_ACTION_DOCUMENT).revision_hash,
        node_id,
    )


def v3_action_run_row(root: Path) -> tuple[object, ...]:
    return database_row(
        root,
        "atelier.sqlite",
        "SELECT state,current_node_id FROM runs WHERE run_id=?",
        (V3_ACTION_RUN_ID,),
    )


def workflow_statuses(root: Path, workflow_id: str) -> tuple[str, ...]:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        return tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT status FROM workflow_status WHERE workflow_uuid=?",
                (workflow_id,),
            )
        )


def v3_wait_node_execution(node_id: str) -> NodeExecutionId:
    return NodeExecutionId.for_node(
        RunId(V3_WAIT_RUN_ID), WorkflowRevision(V3_WAIT_DOCUMENT).revision_hash, node_id
    )


def initialize_and_seed_v3_wait(root: Path) -> None:
    child(root, "initialize")
    child(
        root,
        "seed-v3",
        V3_WAIT_RUN_ID,
        V3_WAIT_DOCUMENT.hex(),
        str(int(AgentConfigurationRevisionFormatVersion.V2)),
    )


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


def strip_the_configuration_contract(root: Path, workflow_id: str) -> frozenset[str]:
    """Rewrite one recorded binding into the shape written before the contract existed.

    The row is read, changed and written back through DBOS' own recorded
    encoding rather than a hand-built payload, so what the restart is handed is
    a row this engine could have written -- an agent binding with neither
    `revision_format_version` nor `requested_capability`.
    """
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        recorded = connection.execute(
            "SELECT output FROM operation_outputs "
            "WHERE workflow_uuid=? AND function_name=?",
            (workflow_id, NODE_BINDING_STEP_NAME),
        ).fetchone()
        assert recorded is not None
        binding = dict(pickle.loads(base64.b64decode(str(recorded[0]))))
        removed = frozenset(binding) & {
            "revision_format_version",
            "requested_capability",
        }
        for key in removed:
            del binding[key]
        connection.execute(
            "UPDATE operation_outputs SET output=? "
            "WHERE workflow_uuid=? AND function_name=?",
            (
                base64.b64encode(pickle.dumps(binding)).decode("ascii"),
                workflow_id,
                NODE_BINDING_STEP_NAME,
            ),
        )
    return removed


@pytest.mark.proves("a-legacy-node-binding-row-replays-into-the-typed-decision")
def test_a_legacy_binding_row_replays_without_a_second_provider_call(
    tmp_path: Path,
) -> None:
    """The one durable shape older than the contract still drives its own run home."""
    initialize_and_seed_v3(tmp_path, AgentConfigurationRevisionFormatVersion.V1)

    child(
        tmp_path,
        "execute-v3-until-complete",
        V3_RUN_ID,
        str(tmp_path / "v3-successor-crash"),
        "start-successor:review",
        expected=CRASHED,
    )

    assert v3_cardinalities(tmp_path) == (1, 1, 1, 1)
    assert strip_the_configuration_contract(
        tmp_path, v3_node_workflow_id("implement")
    ) == frozenset({"revision_format_version", "requested_capability"})

    child(tmp_path, "execute-v3-until-complete", V3_RUN_ID, "NONE", "NONE")

    assert v3_cardinalities(tmp_path) == (2, 2, 2, 2)
    assert database_row(
        tmp_path,
        "atelier.sqlite",
        "SELECT state,current_node_id,last_event_sequence FROM runs WHERE run_id=?",
        (V3_RUN_ID,),
    ) == ("COMPLETED", "review", 2)


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


def test_a_v3_answer_commit_crash_recovers_one_answer_and_the_heir(
    tmp_path: Path,
) -> None:
    """A crash inside the answer's own commit neither loses nor doubles it.

    The process dies inside `durable_answer`'s commit step, after the
    transaction that applied the answer but before the step result is
    recorded -- the last shared window a restart can tear. Recovery must
    accept the applied answer it finds rather than apply a second one, start
    the heir exactly once, and reach the terminal hash of an uninterrupted
    line.
    """
    initialize_and_seed_v3_wait(tmp_path)
    child(tmp_path, "execute-v3-until-wait", V3_WAIT_RUN_ID, "NONE", "NONE")
    child(tmp_path, "answer", V3_WAIT_RUN_ID, "approve", V3_ANSWER)
    marker = tmp_path / "v3-answer-crash"

    child(
        tmp_path,
        "execute-v3-until-complete",
        V3_WAIT_RUN_ID,
        str(marker),
        ANSWER_COMMIT_STEP_NAME,
        expected=CRASHED,
    )

    assert marker.read_text() == f"{ANSWER_COMMIT_STEP_NAME}:before-record"
    assert scalar(tmp_path, "SELECT state FROM wait_answers") == "APPLIED"
    assert event_kinds(tmp_path) == (
        "AGENT_COMPLETED",
        "WAITING_INPUT",
        "WAIT_ANSWERED",
    )
    assert scalar(tmp_path, "SELECT terminal_hash FROM runs") is None
    assert (
        workflow_identity_rows(
            tmp_path, node_workflow_id_for(v3_wait_node_execution("review"))
        )
        == ()
    )

    child(tmp_path, "execute-v3-until-complete", V3_WAIT_RUN_ID, "NONE", "NONE")

    with sqlite3.connect(tmp_path / "atelier.sqlite", timeout=30) as connection:
        event_hashes = tuple(
            Sha256Hash(str(row[0]))
            for row in connection.execute(
                "SELECT event_hash FROM run_events ORDER BY event_sequence"
            )
        )
    assert event_kinds(tmp_path) == (
        "AGENT_COMPLETED",
        "WAITING_INPUT",
        "WAIT_ANSWERED",
        "AGENT_COMPLETED",
    )
    assert wait_run_row(tmp_path) == (
        "COMPLETED",
        "review",
        4,
        terminal_hash_for(
            WorkflowRevision(V3_WAIT_DOCUMENT).revision_hash, event_hashes
        ).value,
    )
    assert v3_cardinalities(tmp_path) == (2, 2, 4, 2)
    answer_identity = answer_workflow_id_for(v3_wait_node_execution("approve"))
    assert workflow_identity_rows(tmp_path, answer_identity) == (
        (answer_identity, "atelier2_wait_answer"),
    )


def test_a_v3_action_in_flight_survives_the_death_of_its_process(
    tmp_path: Path,
) -> None:
    """A live effect workflow is a driver, so the restart must not end this run.

    The process is killed inside the effect's commit, while `durable_effect`
    still owes the run the continuation that carries it off the Action node.
    What the restart finds is a run STARTED on a node that has no attempt, will
    never have one, and whose own node workflow returned to SUCCESS the moment
    it started the effect -- the exact shape the serve-start gap inventory
    reads as dead. Reading only node workflows failed this healthy run before
    it could finish (#645). What the restart must produce is the run the crash
    interrupted: one ACTION_COMPLETED over one external effect, and a
    COMPLETED terminal.
    """

    initialize_and_seed_v3_action(tmp_path)
    marker = tmp_path / "v3-action-crash"

    child(
        tmp_path,
        "execute-v3-until-complete",
        V3_ACTION_RUN_ID,
        str(marker),
        COMMIT_STEP_NAME,
        expected=CRASHED,
    )

    assert marker.read_text() == f"{COMMIT_STEP_NAME}:before-record"
    action = v3_action_node_execution("publish")
    continuation = action_continuation_workflow_id_for(logical_effect_key_for(action))
    assert v3_action_run_row(tmp_path) == ("STARTED", "publish")
    assert event_kinds(tmp_path) == ("AGENT_COMPLETED",)
    assert workflow_statuses(tmp_path, node_workflow_id_for(action)) == ("SUCCESS",)
    assert workflow_statuses(tmp_path, continuation) == ()
    assert workflow_statuses(
        tmp_path, effect_workflow_id_for(logical_effect_key_for(action))
    ) == ("PENDING",)

    child(tmp_path, "execute-v3-until-complete", V3_ACTION_RUN_ID, "NONE", "NONE")

    assert v3_action_run_row(tmp_path) == ("COMPLETED", "publish")
    assert event_kinds(tmp_path) == ("AGENT_COMPLETED", "ACTION_COMPLETED")
    assert scalar(tmp_path, "SELECT state FROM effect_intents") == "CONFIRMED"
    assert scalar(tmp_path, "SELECT COUNT(*) FROM effect_receipts") == 1
    assert database_row(
        tmp_path, "external.sqlite", "SELECT COUNT(*) FROM loopback_effect_calls"
    ) == (1,)


def test_an_agent_effect_checkpoint_recovers_after_its_continuation_commit(
    tmp_path: Path,
) -> None:
    """A replay accepts the effect-confirmed run state it already advanced."""

    initialize_and_seed_v3_agent_open_pr(tmp_path)
    marker = tmp_path / "agent-effect-checkpoint-crash"

    child(
        tmp_path,
        "execute-v3-until-complete",
        V3_AGENT_OPEN_PR_RUN_ID,
        str(marker),
        ACTION_CHECKPOINT_STEP_NAME,
        expected=CRASHED,
    )

    assert marker.read_text() == f"{ACTION_CHECKPOINT_STEP_NAME}:before-record"
    assert database_row(
        tmp_path,
        "atelier.sqlite",
        "SELECT state,current_node_id,last_event_sequence FROM runs WHERE run_id=?",
        (V3_AGENT_OPEN_PR_RUN_ID,),
    ) == ("COMPLETED", "implement", 2)
    assert scalar(tmp_path, "SELECT COUNT(*) FROM effect_receipts") == 1

    child(
        tmp_path,
        "execute-v3-until-complete",
        V3_AGENT_OPEN_PR_RUN_ID,
        "NONE",
        "NONE",
    )

    assert event_kinds(tmp_path) == ("AGENT_COMPLETED", "ACTION_COMPLETED")
    assert scalar(tmp_path, "SELECT COUNT(*) FROM effect_receipts") == 1
    assert database_row(
        tmp_path, "external.sqlite", "SELECT COUNT(*) FROM loopback_effect_calls"
    ) == (1,)
