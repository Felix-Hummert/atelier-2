from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi.sse import ServerSentEvent
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.effect_store import commit_resolution, encode_found
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import (
    DbosWaitAnswerer,
    commit_action_completed,
    commit_subworkflow_completed,
    commit_wait_answered,
    commit_waiting_input,
    load_wait_answer,
)
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import effect_intents
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.references import encode_public_run_reference
from atelier2.api.stream import (
    BoundedQueryRunner,
    PreparedEventStream,
    stream_server_events,
)
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits
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
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.run_events import RunEventPage
from atelier2.ports.run_queries import RunFound
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_attempt_execution,
    commit_configured_agent,
)
from tests.scenarios.api import (
    SSE_COMPLETE_HISTORY,
    SSE_CURSOR_AFTER_THREE,
    SSE_PUBLIC_RUN_REFERENCE,
    api_limits,
    event_poll_backoff,
    stream_page_reader,
)
from tests.scenarios.runs import (
    prepare_and_launch_graph_action,
    start_published_v1_run,
    submit_reconcile_command,
)
from tests.scenarios.runtime import exact_output_runtime

DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: wait, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: wait}
  - {id: agent, type: agent, job: test, output: request, next: action}
"""


def _runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    runtime = exact_output_runtime(
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
            DbosDurableRunStarter(
                runtime.engine, runtime.settings, runtime.agent_executor_registry
            ),
            DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
            DbosEffectReconcileCommander(runtime.engine, runtime.settings),
            queries,
            queries,
            queries,
            parse_workflow_document,
            DbosAgentConfigurationCatalog(
                runtime.engine, runtime.agent_executor_registry
            ),
            DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version),
        ),
        limits=api_limits(event_page_size=page_size),
        event_poll_backoff=event_poll_backoff(),
    )
    return TestClient(app)


def _parse_events(body: str) -> list[dict[str, object]]:
    parsed: list[dict[str, object]] = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", maxsplit=1) for line in block.splitlines())
        assert set(fields) == {"id", "data"}
        parsed.append(
            {
                "id": fields["id"],
                "data": json.loads(fields["data"]),
            }
        )
    return parsed


def test_agent_failed_stream_is_bounded_and_secret_free(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.integration.test_agent_attempts import attempt_request

    canary = "private-agent-secret-41b8e0"
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "controlled-process", b"unused"
    )
    factory.__dict__["private_material"] = canary
    runtime = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "failed-sse"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("failed-sse"),
        ),
        ExactOutputAgentExecutorFactory(),
        (factory,),
    )
    runtime.initialize_storage()
    try:
        caplog.set_level(logging.DEBUG)
        request = attempt_request(runtime, "attempt/failed-sse")
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(agent_attempt_execution(request))
        store.claim(agent_attempt_execution(request))
        store.complete_known_failure(agent_attempt_execution(request))
        queries = DbosQueries(runtime.engine)

        found = queries.get_run(request.run_id)
        assert isinstance(found, RunFound)

        async def first_event() -> ServerSentEvent:
            stream = aiter(
                stream_server_events(
                    PreparedEventStream(request.run_id, 0, 1, False, found.projection),
                    stream_page_reader(queries),
                    BoundedQueryRunner(1, admission_timeout_seconds=1),
                    page_size=1,
                    limits=api_limits(),
                    poll_backoff=event_poll_backoff(),
                )
            )
            return await anext(stream)

        streamed = asyncio.run(first_event())
        assert streamed.event is None
        stream_json = streamed.model_dump_json()
        run_json = (
            _client(runtime)
            .get("/atelier/api/v1/runs/" + encode_public_run_reference(request.run_id))
            .text
        )
        with runtime.engine.connect() as connection:
            durable = {
                table: tuple(
                    tuple(row)
                    for row in connection.exec_driver_sql(f'SELECT * FROM "{table}"')
                )
                for table in sa.inspect(connection).get_table_names()
            }

        assert '"event":"AGENT_FAILED"' in stream_json
        assert '"failure_code":"PROCESS_EXITED_UNSUCCESSFULLY"' in stream_json
        assert all(
            canary not in channel
            for channel in (
                repr(durable),
                run_json,
                stream_json,
                "\n".join(record.getMessage() for record in caplog.records),
            )
        )
        assert "payload" not in stream_json
        assert "base64" not in stream_json
        assert "stderr" not in stream_json
    finally:
        runtime.close()


def test_sse_emits_all_seven_persisted_events_and_resumes_after_cursor(
    tmp_path: Path,
) -> None:
    for runtime in _runtime(tmp_path):
        _run_id, _revision = _complete_history(runtime)
        client = _client(runtime)
        path = f"/atelier/api/v1/runs/{SSE_PUBLIC_RUN_REFERENCE}/events"

        first = _parse_events(client.get(path).text)
        resumed = _parse_events(
            client.get(path, headers={"last-event-id": SSE_CURSOR_AFTER_THREE}).text
        )
        second_reader = _parse_events(client.get(path).text)

        assert first == SSE_COMPLETE_HISTORY
        assert resumed == SSE_COMPLETE_HISTORY[3:]
        assert second_reader == SSE_COMPLETE_HISTORY


def test_two_concurrent_readers_receive_the_same_durable_order(
    tmp_path: Path,
) -> None:
    for runtime in _runtime(tmp_path):
        _run_id, _revision = _complete_history(runtime)
        path = f"/atelier/api/v1/runs/{SSE_PUBLIC_RUN_REFERENCE}/events"
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


def test_receipt_result_limit_stays_in_each_indexed_snapshot_query(
    tmp_path: Path,
) -> None:
    for runtime in _runtime(tmp_path):
        run_id, revision = _complete_history(runtime)
        receipt_selects: list[tuple[str, tuple[Any, ...]]] = []
        connection_ids: set[int] = set()
        transaction_states: list[bool] = []

        def capture_receipt_select(
            connection: Any,
            _cursor: Any,
            statement: str,
            parameters: tuple[Any, ...],
            _context: Any,
            _executemany: bool,
            captured_receipt_selects: list[tuple[str, tuple[Any, ...]]] = (
                receipt_selects
            ),
            captured_connection_ids: set[int] = connection_ids,
            captured_transaction_states: list[bool] = transaction_states,
        ) -> None:
            if "FROM effect_receipts" not in statement:
                return
            captured_receipt_selects.append((statement, parameters))
            raw = connection.connection.driver_connection
            captured_connection_ids.add(id(raw))
            captured_transaction_states.append(bool(raw.in_transaction))

        event.listen(runtime.engine, "before_cursor_execute", capture_receipt_select)
        try:
            page = DbosQueries(runtime.engine).read_run_event_page(
                run_id,
                2,
                2,
                WorkflowPublicationLimits(
                    maximum_document_bytes=len(revision.document),
                    maximum_nodes=10,
                    maximum_string_characters=100,
                    maximum_payload_bytes=100,
                ),
            )
        finally:
            event.remove(
                runtime.engine, "before_cursor_execute", capture_receipt_select
            )

        assert isinstance(page, RunEventPage)
        assert len(receipt_selects) == 2
        assert connection_ids and len(connection_ids) == 1
        assert transaction_states == [True, True]
        for statement, parameters in receipt_selects:
            assert "THEN effect_receipts.result END AS result" in statement
            assert (
                "length(effect_receipts.result) AS _atelier_length_result" in statement
            )
            with runtime.engine.connect() as connection:
                plan = tuple(
                    str(record[-1]).upper()
                    for record in connection.exec_driver_sql(
                        "EXPLAIN QUERY PLAN " + statement, parameters
                    )
                )
            assert any(
                "SEARCH EFFECT_RECEIPTS USING INDEX" in detail for detail in plan
            )
            assert all("SCAN" not in detail for detail in plan)
