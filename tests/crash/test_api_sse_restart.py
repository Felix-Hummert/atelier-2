from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict, cast

import sqlalchemy as sa
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.effect_store import commit_resolution, encode_found
from atelier2.adapters.dbos.run_store import (
    DbosWaitAnswerer,
    commit_action_completed,
    commit_subworkflow_completed,
    commit_wait_answered,
    load_wait_answer,
)
from atelier2.adapters.dbos.run_transitions import (
    commit_reconciliation_required,
    commit_waiting_input,
)
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import effect_intents, runs
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.references import encode_event_cursor, encode_public_run_reference
from atelier2.contracts.effects import (
    AdapterRevision,
    ConfirmationSource,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectResult,
    LogicalEffectKey,
    OperatorFoundEffect,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from tests.scenarios.agents import commit_configured_agent
from tests.scenarios.runs import (
    prepare_and_launch_graph_action,
    start_published_v1_run,
    submit_reconcile_command,
)

DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: wait, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: wait}
  - {id: agent, type: agent, job: test, output: request, next: action}
"""


class ReceivedEventData(TypedDict):
    sequence: int
    event: str


class ReceivedEvent(TypedDict):
    id: str
    data: ReceivedEventData


def _open_runtime(database_path: Path, external_path: Path) -> DbosRuntime:
    runtime = DbosRuntime(
        DbosRuntimeSettings(database_path, "api-crash-tests"),
        LoopbackEffectAdapterFactory(
            external_path,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
    )
    runtime.initialize_storage()
    return runtime


def _seed_first_three_events(
    database_path: Path, external_path: Path
) -> tuple[RunId, WorkflowRevision, LogicalEffectKey]:
    runtime = _open_runtime(database_path, external_path)
    try:
        run_id = RunId("crash/sse")
        revision = WorkflowRevision(DOCUMENT)
        start_published_v1_run(runtime.engine, runtime.settings, run_id, revision)
        with canonical_write_transaction(runtime.engine) as connection:
            commit_configured_agent(connection, run_id, revision.revision_hash, "agent")
        intent = prepare_and_launch_graph_action(
            runtime.engine,
            runtime.settings,
            run_id,
            revision.revision_hash,
            runtime.effect_adapter_binding,
        )
        with canonical_write_transaction(runtime.engine) as connection:
            connection.execute(
                effect_intents.update().values(
                    state=EffectIntentState.WAITING_RECONCILIATION.value,
                    state_version=1,
                )
            )
            commit_reconciliation_required(
                connection,
                run_id,
                revision.revision_hash,
                "action",
                intent.request.payload,
            )
        command = _reconcile_command(intent)
        submit_reconcile_command(runtime.engine, runtime.settings, command)
        determination = command.determination
        assert isinstance(determination, OperatorFoundEffect)
        with Session(runtime.engine) as session, session.begin():
            commit_resolution(
                session,
                intent.binding.logical_key.value,
                revision.revision_hash.value,
                encode_found(
                    PerformedEffect(determination.effect_id, determination.result),
                    ConfirmationSource.OPERATOR_FOUND,
                    command.command_id,
                ),
                command.command_id,
            )
        return run_id, revision, intent.binding.logical_key
    finally:
        runtime.close()


def _reconcile_command(intent: EffectIntent) -> ReconcileCommand:
    return ReconcileCommand(
        ReconcileCommandId("crash-command"),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator"),
        "inspected exact request",
        OperatorFoundEffect(EffectId("effect"), EffectResult(b"result")),
    )


def _append_terminal_events(
    database_path: Path,
    external_path: Path,
    run_id: RunId,
    revision: WorkflowRevision,
    logical_key: LogicalEffectKey,
) -> str:
    runtime = _open_runtime(database_path, external_path)
    try:
        with canonical_write_transaction(runtime.engine) as connection:
            commit_action_completed(connection, logical_key, revision.revision_hash)
            commit_waiting_input(connection, run_id, revision.revision_hash, "wait")
        answerer = DbosWaitAnswerer(
            runtime.engine, runtime.settings.application_version
        )
        answerer.submit_result(
            SubmitWaitAnswerRequest(
                run_id,
                revision.revision_hash,
                "wait",
                NodeExecutionId.for_node(run_id, revision.revision_hash, "wait"),
                WaitAnswerActor.OPERATOR,
                b"17",
            )
        )
        with canonical_write_transaction(runtime.engine) as connection:
            answer = load_wait_answer(
                connection, run_id, revision.revision_hash, "wait"
            )
            commit_wait_answered(connection, answer.answer)
            commit_subworkflow_completed(
                connection, run_id, revision.revision_hash, "final", 5
            )
            terminal_hash = connection.scalar(
                sa.select(runs.c.terminal_hash).where(runs.c.run_id == run_id.value)
            )
        assert isinstance(terminal_hash, str)
        return terminal_hash
    finally:
        runtime.close()


@contextmanager
def _bound_listener() -> Iterator[socket.socket]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        yield listener


def _serve(database_path: Path, listener: socket.socket) -> subprocess.Popen[bytes]:
    repository_root = Path(__file__).parents[2]
    port = int(listener.getsockname()[1])
    environment = os.environ.copy()
    environment.update(
        {
            "ATELIER2_TEST_DATABASE": str(database_path),
            "ATELIER2_TEST_APP_VERSION": "api-crash-tests",
            "ATELIER2_TEST_SOURCE_COMMIT": "crash-commit",
            "ATELIER2_TEST_SOURCE_TREE": "crash-tree",
            "PYTHONPATH": os.pathsep.join(
                (str(repository_root / "src"), str(repository_root))
            ),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--app-dir",
            str(Path(__file__).parent),
            "api_sse_harness:app",
            "--fd",
            str(listener.fileno()),
            "--log-level",
            "warning",
        ],
        cwd=repository_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(listener.fileno(),),
    )
    _wait_until_serving(process, port)
    return process


def _wait_until_serving(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _raise_process_failure(process)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
            connection.request("GET", "/atelier/api/v1/health")
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return
        except OSError:
            time.sleep(0.02)
    process.kill()
    process.wait(timeout=5)
    raise AssertionError("Uvicorn did not become ready")


def _raise_process_failure(process: subprocess.Popen[bytes]) -> None:
    _stdout, stderr = process.communicate(timeout=1)
    raise AssertionError(
        f"Uvicorn exited before serving: {stderr.decode(errors='replace')}"
    )


def _read_events(
    port: int,
    path: str,
    *,
    last_event_id: str | None = None,
    stop_after: int | None = None,
) -> list[ReceivedEvent]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"accept": "text/event-stream"}
    if last_event_id is not None:
        headers["last-event-id"] = last_event_id
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    assert response.status == 200
    events: list[ReceivedEvent] = []
    fields: dict[str, str] = {}
    while True:
        raw_line = response.readline()
        if raw_line == b"":
            break
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if line:
            name, value = line.split(": ", maxsplit=1)
            fields[name] = value
            continue
        if fields and "data" in fields:
            assert set(fields) == {"id", "data"}
            data = json.loads(fields["data"])
            assert isinstance(data, dict)
            assert type(data.get("sequence")) is int
            assert isinstance(data.get("event"), str)
            events.append(
                {
                    "id": fields["id"],
                    "data": cast(ReceivedEventData, data),
                }
            )
            fields = {}
            if stop_after is not None and len(events) == stop_after:
                connection.close()
                return events
    connection.close()
    return events


@contextmanager
def _killed(process: subprocess.Popen[bytes]) -> Iterator[None]:
    try:
        yield
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_last_event_id_resumes_after_real_api_sigkill_and_allows_unacknowledged_replay(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    external_path = tmp_path / "external.sqlite"
    run_id, revision, logical_key = _seed_first_three_events(
        database_path, external_path
    )
    reference = encode_public_run_reference(run_id)
    path = f"/atelier/api/v1/runs/{reference}/events"

    with _bound_listener() as first_listener:
        first_port = int(first_listener.getsockname()[1])
        first_process = _serve(database_path, first_listener)
        with _killed(first_process):
            before_crash = _read_events(first_port, path, stop_after=3)
            assert [event["data"]["sequence"] for event in before_crash] == [1, 2, 3]
            acknowledged_cursor = str(before_crash[-1]["id"])
            replay_cursor = str(before_crash[-2]["id"])
            first_process.kill()
            first_process.wait(timeout=5)

    terminal_hash = _append_terminal_events(
        database_path, external_path, run_id, revision, logical_key
    )

    with _bound_listener() as second_listener:
        second_port = int(second_listener.getsockname()[1])
        second_process = _serve(database_path, second_listener)
        with _killed(second_process):
            resumed = _read_events(second_port, path, last_event_id=acknowledged_cursor)
            replayed = _read_events(second_port, path, last_event_id=replay_cursor)

            assert [event["data"]["sequence"] for event in resumed] == [4, 5, 6, 7]
            assert [event["data"]["sequence"] for event in replayed] == [3, 4, 5, 6, 7]
            assert resumed[-1]["data"]["event"] == "SUBWORKFLOW_COMPLETED"
            connection = http.client.HTTPConnection("127.0.0.1", second_port, timeout=5)
            connection.request("GET", f"/atelier/api/v1/runs/{reference}")
            run_response = connection.getresponse()
            run_resource = json.loads(run_response.read())
            connection.close()
            assert run_resource["terminal_hash"] == terminal_hash
            assert run_resource["latest_event_cursor"] == encode_event_cursor(run_id, 7)
