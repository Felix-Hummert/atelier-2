from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from atelier2.adapters.dbos.names import (
    COMMIT_STEP_NAME,
    OBSERVE_STEP_NAME,
    RESOLVE_STEP_NAME,
)
from atelier2.adapters.dbos.workflow_ids import (
    action_continuation_workflow_id_for,
    effect_workflow_id_for,
    node_workflow_id_for,
)
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.executions import NodeExecutionId, logical_effect_key_for
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, WorkflowRevision
from tests.crash.effect_harness import PROVIDER_OUTPUT
from tests.scenarios.effect_requests import open_pull_request_request_for_output
from tests.scenarios.workflows import (
    V3_EFFECT_LINE_ACTION_NODE_ID,
    V3_EFFECT_LINE_AGENT_NODE_ID,
    V3_EFFECT_LINE_DOCUMENT,
    V3_EFFECT_LINE_WAIT_NODE_ID,
)

CRASHED = 86
ADAPTER_EXECUTE_AFTER_COMMIT = "adapter/execute-after-commit"
HARNESS = Path(__file__).with_name("effect_harness.py")
VERSION = "executor-A"
DOCUMENT = V3_EFFECT_LINE_DOCUMENT
ACTION_REQUEST = open_pull_request_request_for_output(PROVIDER_OUTPUT)
ACTION_REQUEST_HASH = Sha256Hash.of(ACTION_REQUEST).value


def child(
    root: Path,
    command: str,
    *arguments: str,
    expected: int = 0,
    timeout: float = 20,
) -> None:
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
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (
                    str(Path(__file__).parents[2]),
                    str(Path(__file__).parents[2] / "src"),
                )
            ),
        },
        text=True,
        timeout=timeout,
    )
    assert result.returncode == expected, result.stderr


def scalar(
    root: Path, database: str, statement: str, parameters: tuple[str, ...] = ()
) -> object:
    with sqlite3.connect(root / database, timeout=30) as connection:
        row = connection.execute(statement, parameters).fetchone()
    return None if row is None else row[0]


def row(
    root: Path, database: str, statement: str, parameters: tuple[str, ...] = ()
) -> tuple[object, ...]:
    with sqlite3.connect(root / database, timeout=30) as connection:
        record = connection.execute(statement, parameters).fetchone()
    assert record is not None
    return tuple(record)


def event_rows(root: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        return tuple(
            tuple(record)
            for record in connection.execute(
                "SELECT run_id,revision_hash,event_sequence,node_id,"
                "node_execution_id,event_kind,payload,payload_hash,"
                "receipt_logical_key,receipt_result_hash,event_hash "
                "FROM run_events ORDER BY event_sequence"
            )
        )


def event_kinds(root: Path) -> tuple[object, ...]:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        return tuple(
            record[0]
            for record in connection.execute(
                "SELECT event_kind FROM run_events ORDER BY event_sequence"
            )
        )


def c1_expected_event_rows() -> tuple[tuple[object, ...], ...]:
    return (
        (
            "observe-0",
            "da486bf0a9d4af874410964455824ee6077afa3bf9e196f8ec3829a43d611c44",
            1,
            V3_EFFECT_LINE_AGENT_NODE_ID,
            "99553e1bbd3cdf4b91398a797d9fc230221765846908704f9e388687c99884aa",
            "AGENT_COMPLETED",
            PROVIDER_OUTPUT,
            "10ba077a13378ab8b5b338a47169e9e0d9bbf1f746c2f45cdd9461896c0c0bb2",
            None,
            None,
            "a6ebe03064f64c1ec637ec768f2917fbf6160771d31562283a406236ad8a9467",
        ),
        (
            "observe-0",
            "da486bf0a9d4af874410964455824ee6077afa3bf9e196f8ec3829a43d611c44",
            2,
            V3_EFFECT_LINE_ACTION_NODE_ID,
            "fe6396a3939fce65d3e2804354909b705eca900fd8fd9a9ab2547354baeeb29e",
            "ACTION_COMPLETED",
            ACTION_REQUEST,
            ACTION_REQUEST_HASH,
            "atelier2-node-effect-f2c4269d36ed2a1260bd6d959aa5de5dd3252b9fe670308b69e0ae4b26d8a0cb",
            ACTION_REQUEST_HASH,
            "919e174385f9ccaa324bd8af5c0ddb0e2995ba8b7f612308a682978564d7cf1c",
        ),
        (
            "observe-0",
            "da486bf0a9d4af874410964455824ee6077afa3bf9e196f8ec3829a43d611c44",
            3,
            V3_EFFECT_LINE_WAIT_NODE_ID,
            "cf04409711609f05707d161c70a6c9d7f42e43a3eb5dabb7594704f2f0370698",
            "WAITING_INPUT",
            b"",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            None,
            None,
            "f02614d821928f12d387556916f55d5642c7042e29e7c693528618096a998bf4",
        ),
    )


def c2_expected_event_rows() -> tuple[tuple[object, ...], ...]:
    return (
        (
            "c2-after-external-commit",
            "da486bf0a9d4af874410964455824ee6077afa3bf9e196f8ec3829a43d611c44",
            1,
            V3_EFFECT_LINE_AGENT_NODE_ID,
            "3d2f3b2b439c0e7e09efd1d63ea12070f2c25daebc9e350a4b41e9481d1ba154",
            "AGENT_COMPLETED",
            PROVIDER_OUTPUT,
            "10ba077a13378ab8b5b338a47169e9e0d9bbf1f746c2f45cdd9461896c0c0bb2",
            None,
            None,
            "83439e78b8620dd06c52c8189c754894d9f82882f02f16aecdc6dd3333058aaa",
        ),
        (
            "c2-after-external-commit",
            "da486bf0a9d4af874410964455824ee6077afa3bf9e196f8ec3829a43d611c44",
            2,
            V3_EFFECT_LINE_ACTION_NODE_ID,
            "1ebd520e67a6eab641f5c489c8ad2964e0e5f3016ed9e3dd9d00bfefe382f2a9",
            "ACTION_COMPLETED",
            ACTION_REQUEST,
            ACTION_REQUEST_HASH,
            "atelier2-node-effect-e2665bb584d6511191603a42f28a81ac5e5b8f02f939d841491d32f15dfaa485",
            ACTION_REQUEST_HASH,
            "6a8bece85dd2189a0132fd0fb1e7f6d178ca318e8222f8db166cb3102d86a149",
        ),
        (
            "c2-after-external-commit",
            "da486bf0a9d4af874410964455824ee6077afa3bf9e196f8ec3829a43d611c44",
            3,
            V3_EFFECT_LINE_WAIT_NODE_ID,
            "5df777e1d1449bd48a4206be27945623e0c04faef06385b272e738cfe42b8be5",
            "WAITING_INPUT",
            b"",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            None,
            None,
            "21a3c71efc798d61678b731085de30a2587b581fe2a203152282ba6735ae27de",
        ),
    )


def seed(root: Path, run_id: str) -> None:
    child(root, "initialize")
    child(root, "seed", run_id, DOCUMENT.hex())


def crash(
    root: Path,
    run_id: str,
    operation: str,
    timing: str,
) -> Path:
    marker = root / f"crash-{operation.replace('/', '-')}-{timing}"
    child(
        root,
        "execute",
        run_id,
        str(marker),
        operation,
        timing,
        "8",
        expected=CRASHED,
    )
    assert marker.read_text() == f"{operation}:{timing}"
    return marker


def resume(root: Path, run_id: str) -> None:
    child(root, "execute", run_id, "NONE", "NONE", "before-record", "8")


def logical_key_for(run_id: str) -> str:
    revision = WorkflowRevision(DOCUMENT)
    execution = NodeExecutionId.for_node(
        RunId(run_id), revision.revision_hash, V3_EFFECT_LINE_ACTION_NODE_ID
    )
    return logical_effect_key_for(execution).value


def assert_effect_once(
    root: Path,
    run_id: str,
    confirmation_source: str = "ADAPTER_EXECUTION",
    reconcile_command_id: str | None = None,
) -> None:
    logical_key = logical_key_for(run_id)
    revision_hash = WorkflowRevision(DOCUMENT).revision_hash.value
    request = ACTION_REQUEST
    request_hash = ACTION_REQUEST_HASH
    expected_effect_id = hashlib.sha256(logical_key.encode()).hexdigest()
    operational_identity = str((root / "external.sqlite").resolve())
    expected_intent_version = 3 if reconcile_command_id is not None else 1
    assert row(
        root,
        "atelier.sqlite",
        "SELECT logical_key,run_id,canonical_request,request_hash,"
        "workflow_revision_hash,adapter_revision,destination_identity,"
        "adapter_operational_identity,state,state_version,"
        "reconciliation_owner_command_id FROM effect_intents",
    ) == (
        logical_key,
        run_id,
        request,
        request_hash,
        revision_hash,
        "loopback-v1",
        "loopback-crash-test",
        operational_identity,
        "CONFIRMED",
        expected_intent_version,
        None,
    )
    external_effect = row(
        root,
        "external.sqlite",
        "SELECT canonical_request,request_hash,effect_id,result,result_hash "
        "FROM loopback_effects WHERE logical_key=?",
        (logical_key,),
    )
    assert external_effect == (
        request,
        request_hash,
        expected_effect_id,
        request,
        request_hash,
    )
    assert row(
        root,
        "atelier.sqlite",
        "SELECT logical_key,run_id,canonical_request,request_hash,"
        "workflow_revision_hash,adapter_revision,destination_identity,"
        "adapter_operational_identity,effect_id,result,result_hash,confirmation_source,"
        "reconcile_command_id FROM effect_receipts",
    ) == (
        logical_key,
        run_id,
        request,
        request_hash,
        revision_hash,
        "loopback-v1",
        "loopback-crash-test",
        operational_identity,
        expected_effect_id,
        request,
        request_hash,
        confirmation_source,
        reconcile_command_id,
    )
    assert row(
        root,
        "external.sqlite",
        "SELECT calls FROM loopback_effect_calls WHERE logical_key=?",
        (logical_key,),
    ) == (1,)
    assert scalar(root, "atelier.sqlite", "SELECT COUNT(*) FROM effect_intents") == 1
    assert scalar(root, "atelier.sqlite", "SELECT COUNT(*) FROM effect_receipts") == 1
    if reconcile_command_id is None:
        assert (
            scalar(root, "atelier.sqlite", "SELECT COUNT(*) FROM reconcile_commands")
            == 0
        )
    else:
        assert row(
            root,
            "atelier.sqlite",
            "SELECT command_id,logical_key,expected_intent_version,determination,"
            "actor,evidence,found_effect_id,found_result,found_result_hash,state "
            "FROM reconcile_commands",
        ) == (
            reconcile_command_id,
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


def assert_pre_confirmation_state(
    root: Path, run_id: str, *, external_effect_exists: bool
) -> None:
    logical_key = logical_key_for(run_id)
    revision_hash = WorkflowRevision(DOCUMENT).revision_hash.value
    request = ACTION_REQUEST
    request_hash = ACTION_REQUEST_HASH
    expected_effect_id = hashlib.sha256(logical_key.encode()).hexdigest()
    assert row(
        root,
        "atelier.sqlite",
        "SELECT logical_key,run_id,canonical_request,request_hash,"
        "workflow_revision_hash,adapter_revision,destination_identity,"
        "adapter_operational_identity,state,state_version,"
        "reconciliation_owner_command_id FROM effect_intents",
    ) == (
        logical_key,
        run_id,
        request,
        request_hash,
        revision_hash,
        "loopback-v1",
        "loopback-crash-test",
        str((root / "external.sqlite").resolve()),
        "PREPARED",
        0,
        None,
    )
    assert scalar(root, "atelier.sqlite", "SELECT COUNT(*) FROM effect_intents") == 1
    assert scalar(root, "atelier.sqlite", "SELECT COUNT(*) FROM effect_receipts") == 0
    assert (
        scalar(root, "atelier.sqlite", "SELECT COUNT(*) FROM reconcile_commands") == 0
    )
    expected_count = 1 if external_effect_exists else 0
    assert (
        scalar(root, "external.sqlite", "SELECT COUNT(*) FROM loopback_effects")
        == expected_count
    )
    assert (
        scalar(root, "external.sqlite", "SELECT COUNT(*) FROM loopback_effect_calls")
        == expected_count
    )
    if external_effect_exists:
        assert row(
            root,
            "external.sqlite",
            "SELECT canonical_request,request_hash,effect_id,result,result_hash "
            "FROM loopback_effects WHERE logical_key=?",
            (logical_key,),
        ) == (
            request,
            request_hash,
            expected_effect_id,
            request,
            request_hash,
        )


def assert_action_continued_once(
    root: Path,
    run_id: str,
    confirmation_source: str = "ADAPTER_EXECUTION",
    reconcile_command_id: str | None = None,
) -> None:
    assert_effect_once(root, run_id, confirmation_source, reconcile_command_id)
    revision = WorkflowRevision(DOCUMENT)
    action_execution = NodeExecutionId.for_node(
        RunId(run_id), revision.revision_hash, V3_EFFECT_LINE_ACTION_NODE_ID
    )
    continuation_id = action_continuation_workflow_id_for(
        logical_effect_key_for(action_execution)
    )
    wait_id = node_workflow_id_for(
        NodeExecutionId.for_node(
            RunId(run_id), revision.revision_hash, V3_EFFECT_LINE_WAIT_NODE_ID
        )
    )
    assert row(
        root,
        "atelier.sqlite",
        "SELECT workflow_uuid FROM workflow_status "
        "WHERE name='atelier2_action_continuation'",
    ) == (continuation_id,)
    assert scalar(root, "external.sqlite", "SELECT COUNT(*) FROM loopback_effects") == 1
    assert (
        scalar(root, "external.sqlite", "SELECT COUNT(*) FROM loopback_effect_calls")
        == 1
    )
    assert (
        scalar(
            root,
            "atelier.sqlite",
            "SELECT COUNT(*) FROM workflow_status "
            "WHERE name='atelier2_action_continuation'",
        )
        == 1
    )
    assert row(
        root,
        "atelier.sqlite",
        "SELECT workflow_uuid FROM workflow_status "
        "WHERE name='atelier2_graph_node' AND workflow_uuid=?",
        (wait_id,),
    ) == (wait_id,)
    assert (
        scalar(
            root,
            "atelier.sqlite",
            "SELECT COUNT(*) FROM runs WHERE run_id=? AND state='WAITING_INPUT'",
            (run_id,),
        )
        == 1
    )


def assert_exact_waiting_input_vector(
    root: Path,
    run_id: str,
    expected_events: tuple[tuple[object, ...], ...],
) -> None:
    assert row(
        root,
        "atelier.sqlite",
        "SELECT state,current_node_id,state_version,last_event_sequence,terminal_hash "
        "FROM runs WHERE run_id=?",
        (run_id,),
    ) == ("WAITING_INPUT", V3_EFFECT_LINE_WAIT_NODE_ID, 3, 3, None)
    assert event_rows(root) == expected_events


@pytest.mark.parametrize(
    ("operation", "timing", "outer_results_before"),
    [
        pytest.param(OBSERVE_STEP_NAME, "after-record", 2, id="C1-after-observe"),
        pytest.param(COMMIT_STEP_NAME, "before-record", 3, id="post-confirm"),
    ],
)
def test_named_crash_boundaries_recover_without_duplicate_effect(
    tmp_path: Path,
    operation: str,
    timing: str,
    outer_results_before: int,
) -> None:
    run_id = operation.replace("/", "-")
    seed(tmp_path, run_id)

    crash(tmp_path, run_id, operation, timing)

    workflow_id = effect_workflow_id_for(LogicalEffectKey(logical_key_for(run_id)))
    assert (
        scalar(
            tmp_path,
            "atelier.sqlite",
            "SELECT COUNT(*) FROM operation_outputs WHERE workflow_uuid=?",
            (workflow_id,),
        )
        == outer_results_before
    )
    if operation == OBSERVE_STEP_NAME:
        assert_pre_confirmation_state(tmp_path, run_id, external_effect_exists=False)
    if operation == COMMIT_STEP_NAME:
        assert_effect_once(tmp_path, run_id)

    resume(tmp_path, run_id)

    assert_action_continued_once(tmp_path, run_id)
    if operation == OBSERVE_STEP_NAME:
        assert_exact_waiting_input_vector(tmp_path, run_id, c1_expected_event_rows())
    assert (
        scalar(
            tmp_path,
            "atelier.sqlite",
            "SELECT status FROM workflow_status WHERE workflow_uuid=?",
            (workflow_id,),
        )
        == "SUCCESS"
    )


def test_c2_crash_after_external_commit_before_adapter_return_recovers_once(
    tmp_path: Path,
) -> None:
    run_id = "c2-after-external-commit"
    seed(tmp_path, run_id)

    crash(
        tmp_path,
        run_id,
        ADAPTER_EXECUTE_AFTER_COMMIT,
        "after-external-commit",
    )

    assert_pre_confirmation_state(tmp_path, run_id, external_effect_exists=True)
    workflow_id = effect_workflow_id_for(LogicalEffectKey(logical_key_for(run_id)))
    assert (
        scalar(
            tmp_path,
            "atelier.sqlite",
            "SELECT COUNT(*) FROM datasource_outputs WHERE workflow_id=? AND step_id=3",
            (workflow_id,),
        )
        == 0
    )
    assert (
        scalar(
            tmp_path,
            "atelier.sqlite",
            "SELECT COUNT(*) FROM operation_outputs "
            "WHERE workflow_uuid=? AND function_name=?",
            (workflow_id, RESOLVE_STEP_NAME),
        )
        == 0
    )

    resume(tmp_path, run_id)

    assert_action_continued_once(tmp_path, run_id)
    assert_exact_waiting_input_vector(tmp_path, run_id, c2_expected_event_rows())


def test_unknown_commit_replays_waiting_even_after_provider_availability_changes(
    tmp_path: Path,
) -> None:
    run_id = "unknown-provider-change"
    seed(tmp_path, run_id)
    (tmp_path / "force-unknown").touch()

    crash(tmp_path, run_id, COMMIT_STEP_NAME, "before-record")

    assert (
        scalar(tmp_path, "atelier.sqlite", "SELECT state FROM effect_intents")
        == "WAITING_RECONCILIATION"
    )
    assert (
        scalar(tmp_path, "atelier.sqlite", "SELECT COUNT(*) FROM effect_receipts") == 0
    )
    assert (
        scalar(
            tmp_path, "external.sqlite", "SELECT COUNT(*) FROM loopback_effect_calls"
        )
        == 0
    )
    (tmp_path / "force-unknown").unlink()

    resume(tmp_path, run_id)

    assert (
        scalar(tmp_path, "atelier.sqlite", "SELECT state FROM effect_intents")
        == "WAITING_RECONCILIATION"
    )
    assert (
        scalar(
            tmp_path, "external.sqlite", "SELECT COUNT(*) FROM loopback_effect_calls"
        )
        == 0
    )
    child(tmp_path, "submit-absence", run_id)
    child(tmp_path, "execute", run_id, "NONE", "NONE", "before-record", "8")
    assert_action_continued_once(
        tmp_path,
        run_id,
        "OPERATOR_AUTHORIZED_EXECUTION",
        f"{run_id}/reconcile-1",
    )
    assert event_kinds(tmp_path) == (
        "AGENT_COMPLETED",
        "ACTION_RECONCILIATION_REQUIRED",
        "ACTION_RECONCILIATION_RESOLVED",
        "ACTION_COMPLETED",
        "WAITING_INPUT",
    )


def test_node_workflow_that_dies_before_the_enqueue_reaches_the_operator_door(
    tmp_path: Path,
) -> None:
    """#646: the intent is committed, the effect workflow was never enqueued.

    The Action node workflow ends terminally between the two, so no
    effect-workflow row exists for the #628 sweep to read and nothing is ever
    going to drive the intent. The next serve start must still put it behind
    the operator door instead of leaving the run STARTED forever.
    """

    run_id = "node-dead-before-enqueue"
    seed(tmp_path, run_id)

    child(tmp_path, "strand", "8")

    logical_key = logical_key_for(run_id)
    revision = WorkflowRevision(DOCUMENT)
    action_node_workflow = node_workflow_id_for(
        NodeExecutionId.for_node(
            RunId(run_id), revision.revision_hash, V3_EFFECT_LINE_ACTION_NODE_ID
        )
    )
    assert row(
        tmp_path, "atelier.sqlite", "SELECT state,state_version FROM effect_intents"
    ) == ("PREPARED", 0)
    assert (
        scalar(
            tmp_path,
            "atelier.sqlite",
            "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=?",
            (effect_workflow_id_for(LogicalEffectKey(logical_key)),),
        )
        == 0
    )
    assert (
        scalar(
            tmp_path,
            "atelier.sqlite",
            "SELECT status FROM workflow_status WHERE workflow_uuid=?",
            (action_node_workflow,),
        )
        == "ERROR"
    )
    assert (
        scalar(
            tmp_path,
            "atelier.sqlite",
            "SELECT state FROM runs WHERE run_id=?",
            (run_id,),
        )
        == "STARTED"
    )

    child(tmp_path, "converge")

    assert row(
        tmp_path, "atelier.sqlite", "SELECT state,state_version FROM effect_intents"
    ) == ("WAITING_RECONCILIATION", 1)
    assert (
        scalar(
            tmp_path,
            "atelier.sqlite",
            "SELECT state FROM runs WHERE run_id=?",
            (run_id,),
        )
        == "WAITING_RECONCILIATION"
    )
    assert event_kinds(tmp_path) == (
        "AGENT_COMPLETED",
        "ACTION_RECONCILIATION_REQUIRED",
    )
    assert (
        scalar(
            tmp_path, "external.sqlite", "SELECT COUNT(*) FROM loopback_effect_calls"
        )
        == 0
    )

    child(tmp_path, "submit-absence", run_id)

    assert row(
        tmp_path,
        "atelier.sqlite",
        "SELECT state,reconciliation_owner_command_id FROM effect_intents",
    ) == ("RECONCILING", f"{run_id}/reconcile-1")
    assert row(
        tmp_path, "atelier.sqlite", "SELECT command_id,state FROM reconcile_commands"
    ) == (f"{run_id}/reconcile-1", "PENDING")


def test_two_matching_recovery_processes_converge_after_c2(tmp_path: Path) -> None:
    run_id = "concurrent-recovery"
    seed(tmp_path, run_id)
    crash(
        tmp_path,
        run_id,
        ADAPTER_EXECUTE_AFTER_COMMIT,
        "after-external-commit",
    )
    command = [
        sys.executable,
        str(HARNESS),
        "execute",
        str(tmp_path / "atelier.sqlite"),
        str(tmp_path / "external.sqlite"),
        VERSION,
        str(tmp_path / "force-unknown"),
        run_id,
        "NONE",
        "NONE",
        "before-record",
        "8",
    ]

    processes = [
        subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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
        for _ in range(2)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, f"{stdout}\n{stderr}"

    assert_action_continued_once(tmp_path, run_id)
