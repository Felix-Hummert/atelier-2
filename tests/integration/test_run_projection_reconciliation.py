"""A run parked on its own node's effect stays readable at every door.

An agent node holding an effect grant finishes its attempt and then waits for
the operator to reconcile that effect (decision 0010): the attempt is SUCCEEDED
while the run stands on the same node in `WAITING_RECONCILIATION`. Every read of
such a run answered `durable-state-corrupt` until #1041, which left the operator
without the door where the authorization is given.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
import sqlalchemy as sa
import uvicorn
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents, runs
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectReadback,
    EffectUnknownOutcome,
    OperatorAuthoritativeAbsence,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.run_projections import NodeState, PublicAgentAttemptState
from atelier2.contracts.runs import RunId, RunState
from atelier2.ports.effects import EffectAdapter
from atelier2.ports.run_events import StreamReady
from atelier2.ports.run_queries import QueryDurableStateCorrupt, RunFound
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.api import (
    durable_api_client,
    durable_asgi_app,
    durable_queries,
    event_poll_backoff,
)
from tests.scenarios.open_pr_agent import (
    PR_SPEC,
    create_open_pr_agent_run,
    open_pr_agent_executor_factory,
    publish_open_pr_agent_run,
)
from tests.scenarios.run_waiting import wait_for_run_state
from tests.scenarios.runs import submit_reconcile_command

RUN = RunId("v3/agent-effect-awaits-reconciliation")
NODE_ID = "implement"
_SSE_DATA_PREFIX = "data: "
_SERVER_START_TIMEOUT_SECONDS = 5.0
_SERVER_START_POLL_SECONDS = 0.01
_STREAM_TIMEOUT_SECONDS = 15.0
_LINES_A_PARKED_STREAM_MAY_SERVE = 64
"""How far a reader follows a stream that stays open because the run has not ended.

Far enough for the parked run's whole history and the keep-alive comments around
it, near enough that a stream withholding that history fails instead of hanging.
"""


class _UnprovableEffectAdapter:
    """An adapter that can neither see its effect nor prove its absence.

    That is what parks a run: the store may not open the pull request blindly,
    so it asks the operator, which is the standing this module reads.
    """

    def readback(self, intent: EffectIntent) -> EffectReadback:
        return EffectUnknownOutcome(intent.reference)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        raise AssertionError(f"{intent.reference} was performed without the operator")

    def close(self) -> None:
        return None


class _UnprovableEffectAdapterFactory:
    def __init__(self) -> None:
        self.binding = EffectAdapterBinding(
            AdapterRevision("unprovable-open-pr-v1"),
            EffectDestination("platform"),
            AdapterOperationalIdentity("test-unprovable-github"),
        )

    @property
    def proves_absence(self) -> bool:
        return False

    def open(self) -> EffectAdapter:
        return _UnprovableEffectAdapter()


@contextmanager
def _runtime(database: Path) -> Iterator[DbosRuntime]:
    """One lease on this store, storage ready and nothing driving it yet."""
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            database,
            "run-projection-reconciliation",
            agent_scratch_root=agent_scratch_root(database.parent),
        ),
        _UnprovableEffectAdapterFactory(),
        (open_pr_agent_executor_factory(PR_SPEC),),
    )
    runtime.initialize_storage()
    try:
        yield runtime
    finally:
        runtime.close()


def _park_on_the_effect(runtime: DbosRuntime) -> None:
    """Drive the granted agent node to its success and let the run stop there."""
    workflow, bindings = publish_open_pr_agent_run(runtime, granted=True)
    create_open_pr_agent_run(runtime, RUN, workflow, bindings)
    runtime.launch()
    wait_for_run_state(runtime.engine, RUN, RunState.WAITING_RECONCILIATION)


def _standing_intent(runtime: DbosRuntime) -> EffectIntentSnapshot:
    """The effect this run stands on, read the way the reconciliation door reads it."""
    read = durable_queries(runtime.engine).get_run(RUN)
    assert isinstance(read, RunFound), read
    reconciliation = read.projection.reconciliation
    assert reconciliation is not None
    return reconciliation.intent


@pytest.fixture
def parked_runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    """One run standing on the effect of the agent node that just succeeded."""
    with _runtime(tmp_path / "atelier.sqlite") as runtime:
        _park_on_the_effect(runtime)
        yield runtime


@pytest.fixture
def reconciling_runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    """The same run with the operator's word taken and not yet applied.

    The command is submitted on a lease that never launched, so the
    reconciliation it enqueues stays enqueued: the intent stands in
    `RECONCILING` for the whole read instead of racing it.
    """
    database = tmp_path / "atelier.sqlite"
    with _runtime(database) as driving:
        _park_on_the_effect(driving)
    with _runtime(database) as reopened:
        waiting = _standing_intent(reopened)
        submit_reconcile_command(
            reopened.engine,
            reopened.settings,
            ReconcileCommand(
                ReconcileCommandId("operator-authorizes-the-effect"),
                waiting.intent.reference,
                waiting.state_version,
                ReconcileActor("operator"),
                "inspected the destination and the exact request",
                OperatorAuthoritativeAbsence(),
            ),
        )
        yield reopened


@contextmanager
def _live_server(runtime: DbosRuntime) -> Iterator[int]:
    """A real socket in front of the runtime, because the parked stream never ends.

    The in-process test client answers only once a response is complete, and a
    run that has not ended holds its event stream open by design; only a real
    connection can be told the status and then let go.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(
                durable_asgi_app(runtime, poll_backoff=event_poll_backoff()),
                host="127.0.0.1",
                port=0,
                log_level="critical",
                access_log=False,
                lifespan="off",
            )
        )
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + _SERVER_START_TIMEOUT_SECONDS
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(_SERVER_START_POLL_SECONDS)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=_SERVER_START_TIMEOUT_SECONDS)
            raise AssertionError("no server answered for the parked run's stream")
        try:
            yield port
        finally:
            server.should_exit = True
            thread.join(timeout=_SERVER_START_TIMEOUT_SECONDS)


def _served_history(
    port: int, path: str, last: RunEventKind
) -> tuple[int, tuple[dict[str, Any], ...]]:
    """The stream's status and the frames it serves up to the parked run's last event."""
    connection = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=_STREAM_TIMEOUT_SECONDS
    )
    try:
        connection.request("GET", path, headers={"Accept": "text/event-stream"})
        response = connection.getresponse()
        if response.status != HTTPStatus.OK:
            return response.status, ()
        frames: list[dict[str, Any]] = []
        for _ in range(_LINES_A_PARKED_STREAM_MAY_SERVE):
            line = response.readline().decode("utf-8").rstrip("\n")
            if not line.startswith(_SSE_DATA_PREFIX):
                continue
            frames.append(json.loads(line.removeprefix(_SSE_DATA_PREFIX)))
            if frames[-1].get("event") == last.value:
                return response.status, tuple(frames)
        raise AssertionError(f"{path} never served {last.value}")
    finally:
        connection.close()


@dataclass(frozen=True)
class _EveryDoor:
    """What each way into one run answered, so a test can judge them together."""

    statuses: tuple[int, ...]
    run: dict[str, Any]
    listed: dict[str, Any]
    streamed: tuple[dict[str, Any], ...]


def _every_door(runtime: DbosRuntime) -> _EveryDoor:
    client = durable_api_client(runtime)
    public_ref = encode_public_run_reference(RUN)
    run = client.get(f"{API_PREFIX}/runs/{public_ref}")
    node = client.get(f"{API_PREFIX}/runs/{public_ref}/nodes/{NODE_ID}")
    listed = client.get(
        f"{API_PREFIX}/runs", params={"state": RunState.WAITING_RECONCILIATION.value}
    )
    with _live_server(runtime) as port:
        streamed_status, streamed = _served_history(
            port,
            f"{API_PREFIX}/runs/{public_ref}/events",
            RunEventKind.ACTION_RECONCILIATION_REQUIRED,
        )
    assert run.status_code == HTTPStatus.OK, run.text
    assert node.status_code == HTTPStatus.OK, node.text
    assert listed.status_code == HTTPStatus.OK, listed.text
    return _EveryDoor(
        (run.status_code, node.status_code, listed.status_code, streamed_status),
        run.json(),
        listed.json(),
        streamed,
    )


def _parked_node(read: dict[str, Any]) -> dict[str, Any]:
    """The rail entry of the node the run stands on, told as a success in progress."""
    assert read["state"] == RunState.WAITING_RECONCILIATION.value
    assert read["current_node_id"] == NODE_ID
    rail = {entry["node_id"]: entry for entry in read["node_rail"]}
    assert rail[NODE_ID]["attempt"] == {
        "ordinal": 1,
        "state": PublicAgentAttemptState.SUCCEEDED.value,
    }
    return rail[NODE_ID]


def test_every_read_of_a_run_awaiting_its_own_effect_answers(
    parked_runtime: DbosRuntime,
) -> None:
    doors = _every_door(parked_runtime)

    assert doors.statuses == (HTTPStatus.OK,) * 4
    assert _parked_node(doors.run)["state"] == NodeState.NEEDS_YOU.value
    assert [item["run"]["run_id"] for item in doors.listed["items"]] == [RUN.value]
    assert [frame["event"] for frame in doors.streamed] == [
        RunEventKind.AGENT_COMPLETED.value,
        RunEventKind.ACTION_RECONCILIATION_REQUIRED.value,
    ]


def test_every_read_stays_answerable_while_the_operators_word_is_applied(
    reconciling_runtime: DbosRuntime,
) -> None:
    """The submitted authorization is a moment, not a different run.

    `RECONCILING` is the operator's word taken and not yet folded in. A reader
    who loses the run there loses it exactly while acting on it. The node reads
    as working rather than owed, because the move it was waiting for was made.
    """
    standing = _standing_intent(reconciling_runtime)

    doors = _every_door(reconciling_runtime)

    assert standing.state is EffectIntentState.RECONCILING
    assert doors.statuses == (HTTPStatus.OK,) * 4
    assert _parked_node(doors.run)["state"] == NodeState.WORKING.value
    assert [item["run"]["run_id"] for item in doors.listed["items"]] == [RUN.value]
    assert [frame["event"] for frame in doors.streamed] == [
        RunEventKind.AGENT_COMPLETED.value,
        RunEventKind.ACTION_RECONCILIATION_REQUIRED.value,
    ]


def test_a_success_the_run_no_longer_parks_on_is_still_durable_state_corrupt(
    parked_runtime: DbosRuntime,
) -> None:
    """The invariant survives: nothing else may carry a succeeded current attempt.

    The store refuses to lose the intent itself, so the shape a reader can still
    meet is the run standing where that success cannot be the current one. Its
    history stays honest either way: the event pre-flight reads the intent, not
    the run's standing, so the success still ended nothing.
    """
    with (
        pytest.raises(IntegrityError, match="effect intents are immutable"),
        parked_runtime.engine.begin() as connection,
    ):
        connection.execute(
            effect_intents.delete().where(effect_intents.c.run_id == RUN.value)
        )
    with parked_runtime.engine.begin() as connection:
        connection.execute(
            sa.update(runs)
            .where(runs.c.run_id == RUN.value)
            .values(state=RunState.STARTED.value)
        )

    queries = durable_queries(parked_runtime.engine)

    assert isinstance(queries.get_run(RUN), QueryDurableStateCorrupt)
    assert queries.prepare_run_event_stream(RUN, 0) == StreamReady(2, False, 0)
