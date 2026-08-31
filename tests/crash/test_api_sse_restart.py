"""A subscriber resumes one run's event stream across a real API process death.

The line is a format-3 one, because that is the only family the API projects:
two agent nodes, the person who approves what they made, and one more node that
runs on the approval. The API is killed while the run stands on its Wait, so the
resume has to cross both a dead server and events that were written after it.
"""

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

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import run_events, runs
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.references import encode_event_cursor, encode_public_run_reference
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectDestination,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.runs import start_published_v3_run, submit_wait_answer
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

PROVIDER_OUTPUT = b'"the exact provider bytes"'
APPROVAL = b'"approved"'
RUN = RunId("v3/crash-sse")
DOCUMENT = (
    b"""format_version: 3
name: Two agents, a person, and what follows the approval
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
    + b"""  - id: approve
    type: wait
    prompt: Approve this candidate, or name the blocking defect.
    depends_on: [review]
"""
    + declared_output(ANY_JSON_SCHEMA, "approval")
    + b"""  - id: apply
    type: agent
    role: builder
    mode: headless
    instruction: Do what the approval asked for.
    depends_on: [approve]
"""
    + declared_output()
)
REVISION = WorkflowRevision(DOCUMENT)
PERSISTED_HISTORY = (
    "AGENT_COMPLETED",
    "AGENT_COMPLETED",
    "WAITING_INPUT",
    "WAIT_ANSWERED",
    "AGENT_COMPLETED",
)
"""Every event the line persists, in order; the answer is what splits it in two."""

EVENTS_BEFORE_THE_ANSWER = PERSISTED_HISTORY.index("WAIT_ANSWERED")


class ReceivedEventData(TypedDict):
    sequence: int
    event: str


class ReceivedEvent(TypedDict):
    id: str
    data: ReceivedEventData


def _open_runtime(database_path: Path, external_path: Path) -> DbosRuntime:
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            database_path,
            "api-crash-tests",
            agent_scratch_root=agent_scratch_root(database_path.parent),
        ),
        LoopbackEffectAdapterFactory(
            external_path,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (
            RecordingAgentExecutorFactoryV2(
                "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
            ),
        ),
    )
    runtime.initialize_storage()
    return runtime


def _await_events(runtime: DbosRuntime, expected: int) -> None:
    deadline = time.monotonic() + 15
    observed = 0
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = int(
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(run_events)
                    .where(run_events.c.run_id == RUN.value)
                )
                or 0
            )
        if observed >= expected:
            return
        time.sleep(0.025)
    raise AssertionError(f"the line persisted {observed} events, expected {expected}")


def _seed_events_before_the_answer(database_path: Path, external_path: Path) -> None:
    """Run the line until it stands on its Wait, then let its process die."""

    runtime = _open_runtime(database_path, external_path)
    try:
        published = DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published
        start_published_v3_run(
            runtime.engine,
            runtime.settings,
            RUN,
            REVISION,
            runtime.agent_executor_registry,
        )
        runtime.launch()
        _await_events(runtime, EVENTS_BEFORE_THE_ANSWER)
    finally:
        runtime.close()


def _append_events_after_the_answer(database_path: Path, external_path: Path) -> str:
    """Answer the Wait in a second process and let the recovered line finish."""

    runtime = _open_runtime(database_path, external_path)
    try:
        runtime.launch()
        submit_wait_answer(
            runtime.engine,
            runtime.settings.application_version,
            SubmitWaitAnswerRequest(
                RUN,
                REVISION.revision_hash,
                "approve",
                NodeExecutionId.for_node(RUN, REVISION.revision_hash, "approve"),
                WaitAnswerActor.OPERATOR,
                APPROVAL,
            ),
        )
        _await_events(runtime, len(PERSISTED_HISTORY))
        with runtime.engine.connect() as connection:
            terminal_hash = connection.scalar(
                sa.select(runs.c.terminal_hash).where(runs.c.run_id == RUN.value)
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
    _seed_events_before_the_answer(database_path, external_path)
    reference = encode_public_run_reference(RUN)
    path = f"/atelier/api/v1/runs/{reference}/events"

    with _bound_listener() as first_listener:
        first_port = int(first_listener.getsockname()[1])
        first_process = _serve(database_path, first_listener)
        with _killed(first_process):
            before_crash = _read_events(
                first_port, path, stop_after=EVENTS_BEFORE_THE_ANSWER
            )
            assert [event["data"]["sequence"] for event in before_crash] == [1, 2, 3]
            acknowledged_cursor = str(before_crash[-1]["id"])
            replay_cursor = str(before_crash[-2]["id"])
            first_process.kill()
            first_process.wait(timeout=5)

    terminal_hash = _append_events_after_the_answer(database_path, external_path)

    with _bound_listener() as second_listener:
        second_port = int(second_listener.getsockname()[1])
        second_process = _serve(database_path, second_listener)
        with _killed(second_process):
            resumed = _read_events(second_port, path, last_event_id=acknowledged_cursor)
            replayed = _read_events(second_port, path, last_event_id=replay_cursor)

            assert [event["data"]["sequence"] for event in resumed] == [4, 5]
            assert [event["data"]["sequence"] for event in replayed] == [3, 4, 5]
            assert [event["data"]["event"] for event in replayed] == list(
                PERSISTED_HISTORY[2:]
            )
            connection = http.client.HTTPConnection("127.0.0.1", second_port, timeout=5)
            connection.request("GET", f"/atelier/api/v1/runs/{reference}")
            run_response = connection.getresponse()
            run_resource = json.loads(run_response.read())
            connection.close()
            assert run_resource["terminal_hash"] == terminal_hash
            assert run_resource["latest_event_cursor"] == encode_event_cursor(
                RUN, len(PERSISTED_HISTORY)
            )
