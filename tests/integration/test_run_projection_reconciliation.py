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
from http import HTTPStatus
from pathlib import Path
from threading import Thread

import pytest
import sqlalchemy as sa
import uvicorn

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    EffectUnknownOutcome,
    PerformedEffect,
)
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.run_projections import NodeState, PublicAgentAttemptState
from atelier2.contracts.runs import RunId, RunState
from atelier2.ports.effects import EffectAdapter
from atelier2.ports.run_queries import QueryDurableStateCorrupt
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


@pytest.fixture
def parked_runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    """One run standing on the effect of the agent node that just succeeded."""
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "run-projection-reconciliation",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        _UnprovableEffectAdapterFactory(),
        (open_pr_agent_executor_factory(PR_SPEC),),
    )
    runtime.initialize_storage()
    try:
        workflow, bindings = publish_open_pr_agent_run(runtime, granted=True)
        create_open_pr_agent_run(runtime, RUN, workflow, bindings)
        runtime.launch()
        wait_for_run_state(runtime.engine, RUN, RunState.WAITING_RECONCILIATION)
        yield runtime
    finally:
        runtime.close()


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
) -> tuple[int, tuple[dict[str, object], ...]]:
    """The stream's status and the frames it serves up to the parked run's last event."""
    connection = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=_STREAM_TIMEOUT_SECONDS
    )
    try:
        connection.request("GET", path, headers={"Accept": "text/event-stream"})
        response = connection.getresponse()
        if response.status != HTTPStatus.OK:
            return response.status, ()
        frames: list[dict[str, object]] = []
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


def test_every_read_of_a_run_awaiting_its_own_effect_answers(
    parked_runtime: DbosRuntime,
) -> None:
    client = durable_api_client(parked_runtime)
    public_ref = encode_public_run_reference(RUN)

    run = client.get(f"{API_PREFIX}/runs/{public_ref}")
    node = client.get(f"{API_PREFIX}/runs/{public_ref}/nodes/{NODE_ID}")
    listed = client.get(
        f"{API_PREFIX}/runs", params={"state": RunState.WAITING_RECONCILIATION.value}
    )
    with _live_server(parked_runtime) as port:
        streamed_status, streamed = _served_history(
            port,
            f"{API_PREFIX}/runs/{public_ref}/events",
            RunEventKind.ACTION_RECONCILIATION_REQUIRED,
        )

    assert run.status_code == HTTPStatus.OK, run.text
    assert node.status_code == HTTPStatus.OK, node.text
    assert listed.status_code == HTTPStatus.OK, listed.text
    assert streamed_status == HTTPStatus.OK
    read = run.json()
    assert read["state"] == RunState.WAITING_RECONCILIATION.value
    assert read["current_node_id"] == NODE_ID
    rail = {entry["node_id"]: entry for entry in read["node_rail"]}
    assert rail[NODE_ID]["attempt"] == {
        "ordinal": 1,
        "state": PublicAgentAttemptState.SUCCEEDED.value,
    }
    assert rail[NODE_ID]["state"] == NodeState.NEEDS_YOU.value
    assert [item["run_id"] for item in listed.json()["items"]] == [RUN.value]
    assert [frame["event"] for frame in streamed] == [
        RunEventKind.AGENT_COMPLETED.value,
        RunEventKind.ACTION_RECONCILIATION_REQUIRED.value,
    ]


def test_a_succeeded_attempt_no_effect_waits_on_is_still_durable_state_corrupt(
    parked_runtime: DbosRuntime,
) -> None:
    """The invariant survives: nothing else may carry a succeeded current attempt.

    Moving the run off `WAITING_RECONCILIATION` leaves exactly the shape #1041
    refuses to widen — a success the store can name no successor for.
    """
    with parked_runtime.engine.begin() as connection:
        connection.execute(
            sa.update(runs)
            .where(runs.c.run_id == RUN.value)
            .values(state=RunState.STARTED.value)
        )

    read = durable_queries(parked_runtime.engine).get_run(RUN)

    assert isinstance(read, QueryDurableStateCorrupt)
