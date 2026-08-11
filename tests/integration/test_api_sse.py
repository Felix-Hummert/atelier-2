from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import cast

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.advancer import DbosDurableRunAdvancer, graph_action_intent
from atelier2.adapters.dbos.effect_store import commit_resolution, encode_found
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import (
    DbosWaitAnswerer,
    commit_action_completed,
    commit_agent_completed,
    commit_subworkflow_completed,
    commit_wait_answered,
    commit_waiting_input,
    load_wait_answer,
)
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
    canonical_write_transaction,
)
from atelier2.adapters.dbos.schema import effect_intents
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.app import ApiPorts, create_app
from atelier2.api.models import run_event_resource
from atelier2.api.references import encode_event_cursor, encode_public_run_reference
from atelier2.application.advance_run import advance_run
from atelier2.contracts.effects import (
    AdapterRevision,
    ConfirmationSource,
    EffectDestination,
    EffectId,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectResult,
    OperatorFoundEffect,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.executions import SubmitWaitAnswerRequest
from atelier2.contracts.runs import RunId, StartRunRequest, WorkflowRevision
from atelier2.ports.run_events import RunEventPage

DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: wait, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: wait}
  - {id: agent, type: agent, job: test, output: request, next: action}
"""


def _runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    runtime = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "sse-tests"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    runtime.initialize_storage()
    try:
        yield runtime
    finally:
        runtime.close()


def _complete_history(runtime: DbosRuntime) -> tuple[RunId, WorkflowRevision]:
    run_id = RunId("sse/run")
    revision = WorkflowRevision(DOCUMENT)
    DbosDurableRunStarter(runtime.engine, runtime.settings).start(
        StartRunRequest(run_id, revision)
    )
    with canonical_write_transaction(runtime.engine) as connection:
        commit_agent_completed(
            connection, run_id, revision.revision_hash, "agent", b"request"
        )
        intent = graph_action_intent(
            connection,
            run_id,
            revision.revision_hash,
            runtime.effect_adapter_binding,
        )
    advance_run(
        intent,
        DbosDurableRunAdvancer(
            runtime.engine, runtime.settings, runtime.effect_adapter_binding
        ),
    )
    with canonical_write_transaction(runtime.engine) as connection:
        connection.execute(
            effect_intents.update().values(
                state=EffectIntentState.WAITING_RECONCILIATION.value,
                state_version=1,
            )
        )
        from atelier2.adapters.dbos.run_store import commit_reconciliation_required

        commit_reconciliation_required(
            connection,
            run_id,
            revision.revision_hash,
            "action",
            intent.request.payload,
        )
    command = ReconcileCommand(
        ReconcileCommandId("command"),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator"),
        "inspected exact request",
        OperatorFoundEffect(EffectId("effect"), EffectResult(b"result")),
    )
    DbosEffectReconcileCommander(runtime.engine, runtime.settings).submit(command)
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
    with canonical_write_transaction(runtime.engine) as connection:
        commit_action_completed(
            connection, intent.binding.logical_key, revision.revision_hash
        )
        commit_waiting_input(connection, run_id, revision.revision_hash, "wait")
    answerer = DbosWaitAnswerer(runtime.engine, runtime.settings.application_version)
    answerer.submit_result(
        SubmitWaitAnswerRequest(run_id, revision.revision_hash, "wait", b"17")
    )
    with canonical_write_transaction(runtime.engine) as connection:
        answer = load_wait_answer(connection, run_id, revision.revision_hash, "wait")
        commit_wait_answered(connection, answer.answer)
        commit_subworkflow_completed(
            connection, run_id, revision.revision_hash, "final", 5
        )
    return run_id, revision


def _client(runtime: DbosRuntime, page_size: int = 2) -> TestClient:
    queries = DbosQueries(runtime.engine)
    app = create_app(
        source_commit="commit",
        source_tree="tree",
        ports=ApiPorts(
            DbosWorkflowRevisionPublisher(runtime.engine),
            DbosDurableRunStarter(runtime.engine, runtime.settings),
            DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
            DbosEffectReconcileCommander(runtime.engine, runtime.settings),
            queries,
            queries,
            queries,
        ),
        event_page_size=page_size,
        event_poll_delay_seconds=0.01,
    )
    return TestClient(app)


def _parse_events(body: str) -> list[dict[str, object]]:
    parsed: list[dict[str, object]] = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", maxsplit=1) for line in block.splitlines())
        parsed.append(
            {
                "id": fields["id"],
                "event": fields["event"],
                "data": json.loads(fields["data"]),
            }
        )
    return parsed


def test_sse_emits_all_seven_persisted_events_and_resumes_after_cursor(
    tmp_path: Path,
) -> None:
    for runtime in _runtime(tmp_path):
        run_id, _revision = _complete_history(runtime)
        queries = DbosQueries(runtime.engine)
        page = queries.read_run_event_page(run_id, 0, 100)
        assert isinstance(page, RunEventPage)
        expected = [
            run_event_resource(item).model_dump(mode="json") for item in page.events
        ]
        client = _client(runtime)
        path = f"/atelier/api/v1/runs/{encode_public_run_reference(run_id)}/events"

        first = _parse_events(client.get(path).text)
        resumed = _parse_events(
            client.get(
                path, headers={"last-event-id": encode_event_cursor(run_id, 3)}
            ).text
        )
        second_reader = _parse_events(client.get(path).text)

        assert [item["data"] for item in first] == expected
        assert [item["event"] for item in first] == [item["event"] for item in expected]
        assert [item["id"] for item in first] == [item["cursor"] for item in expected]
        assert [item["data"] for item in resumed] == expected[3:]
        assert second_reader == first
        assert [item["sequence"] for item in expected] == list(range(1, 8))


def test_two_concurrent_readers_receive_the_same_durable_order(
    tmp_path: Path,
) -> None:
    for runtime in _runtime(tmp_path):
        run_id, _revision = _complete_history(runtime)
        path = f"/atelier/api/v1/runs/{encode_public_run_reference(run_id)}/events"
        barrier = Barrier(2)

        def read(
            _: int,
            rendezvous: Barrier = barrier,
            configured_runtime: DbosRuntime = runtime,
            request_path: str = path,
        ) -> list[dict[str, object]]:
            rendezvous.wait(timeout=5)
            return _parse_events(_client(configured_runtime).get(request_path).text)

        with ThreadPoolExecutor(max_workers=2) as pool:
            histories = list(pool.map(read, range(2)))

        assert histories[0] == histories[1]
        assert [
            cast(dict[str, object], event["data"]).get("sequence")
            for event in histories[0]
        ] == list(range(1, 8))
