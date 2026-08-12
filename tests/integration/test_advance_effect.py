from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from typing import Any, NoReturn

import pytest
import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.advancer import (
    DbosDurableRunAdvancer,
    EffectIntentIdentityConflict,
    RunEffectConflict,
    effect_workflow_id_for,
    graph_action_intent,
)
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import effect_intents
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.advance_run import advance_run
from atelier2.application.start_run import start_run
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    EffectDestination,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
    LogicalEffectKey,
)
from atelier2.contracts.runs import RunId, StartRunRequest, WorkflowRevision
from tests.scenarios.agents import commit_configured_agent
from tests.scenarios.runtime import exact_output_runtime

WORKFLOW_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: waiting, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: waiting}
  - {id: agent, type: agent, job: job-17, output: draft-17, next: action}
"""


@pytest.fixture
def storage(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, DbosDurableRunAdvancer, EffectIntent]]:
    runtime = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "executor-A"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    runtime.initialize_storage()
    revision = WorkflowRevision(WORKFLOW_DOCUMENT)
    start_run(
        StartRunRequest(RunId("run-1"), revision),
        DbosDurableRunStarter(runtime.engine, runtime.settings),
    )
    with canonical_write_transaction(runtime.engine) as connection:
        commit_configured_agent(
            connection, RunId("run-1"), revision.revision_hash, "agent"
        )
        intent = graph_action_intent(
            connection,
            RunId("run-1"),
            revision.revision_hash,
            runtime.effect_adapter_binding,
        )
    try:
        yield (
            runtime,
            DbosDurableRunAdvancer(
                runtime.engine, runtime.settings, runtime.effect_adapter_binding
            ),
            intent,
        )
    finally:
        runtime.close()


def test_effect_workflow_id_is_deterministic_from_the_exact_logical_key() -> None:
    key = LogicalEffectKey(" run-1/action-1 ")

    assert effect_workflow_id_for(key) == (
        "atelier2-effect-" + hashlib.sha256(key.value.encode()).hexdigest()
    )


def test_advance_atomically_prepares_one_exact_intent_and_enqueues_its_workflow(
    storage: tuple[DbosRuntime, DbosDurableRunAdvancer, EffectIntent],
) -> None:
    runtime, advancer, intent = storage

    snapshot = advance_run(intent, advancer)

    assert snapshot == EffectIntentSnapshot(
        intent, EffectIntentState.PREPARED, EffectIntentStateVersion(0)
    )
    with runtime.engine.connect() as connection:
        assert connection.execute(sa.select(effect_intents)).one() == (
            intent.binding.logical_key.value,
            intent.binding.run_id.value,
            intent.request.payload,
            intent.request.request_hash.value,
            intent.binding.workflow_revision_hash.value,
            intent.binding.adapter_revision.value,
            intent.binding.destination.value,
            intent.binding.adapter_operational_identity.value,
            EffectIntentState.PREPARED.value,
            0,
            None,
        )
        assert connection.execute(
            sa.text(
                "SELECT workflow_uuid, application_version FROM workflow_status "
                "WHERE workflow_uuid=:workflow_id"
            ),
            {"workflow_id": effect_workflow_id_for(intent.binding.logical_key)},
        ).one() == (effect_workflow_id_for(intent.binding.logical_key), "executor-A")


def test_raise_after_real_effect_enqueue_rolls_back_the_intent_and_workflow(
    storage: tuple[DbosRuntime, DbosDurableRunAdvancer, EffectIntent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, advancer, intent = storage
    real_enqueue = DBOSClient.enqueue_in_transaction

    def enqueue_then_raise(
        self: DBOSClient,
        connection: Connection | Session,
        options: EnqueueOptions,
        *args: Any,
        **kwargs: Any,
    ) -> NoReturn:
        real_enqueue(self, connection, options, *args, **kwargs)
        raise RuntimeError("injected after effect enqueue")

    monkeypatch.setattr(DBOSClient, "enqueue_in_transaction", enqueue_then_raise)

    with pytest.raises(RuntimeError, match="injected"):
        advance_run(intent, advancer)

    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_intents))
            == 0
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                ),
                {"workflow_id": effect_workflow_id_for(intent.binding.logical_key)},
            )
            == 0
        )


def test_exact_retry_returns_the_current_durable_snapshot_without_enqueueing_again(
    storage: tuple[DbosRuntime, DbosDurableRunAdvancer, EffectIntent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, advancer, intent = storage
    advance_run(intent, advancer)
    with runtime.engine.begin() as connection:
        connection.execute(
            effect_intents.update().values(
                state=EffectIntentState.WAITING_RECONCILIATION.value,
                state_version=1,
            )
        )
    monkeypatch.setattr(
        DBOSClient,
        "enqueue_in_transaction",
        lambda *args, **kwargs: pytest.fail("retry enqueued a second workflow"),
    )

    assert advance_run(intent, advancer) == EffectIntentSnapshot(
        intent,
        EffectIntentState.WAITING_RECONCILIATION,
        EffectIntentStateVersion(1),
    )


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            lambda value: replace(value, request=CanonicalRequest(b"changed")),
            id="request",
        ),
        pytest.param(
            lambda value: replace(
                value,
                binding=replace(
                    value.binding, adapter_revision=AdapterRevision("loopback-v2")
                ),
            ),
            id="adapter",
        ),
        pytest.param(
            lambda value: replace(
                value,
                binding=replace(value.binding, destination=EffectDestination("other")),
            ),
            id="destination",
        ),
        pytest.param(
            lambda value: replace(
                value,
                binding=replace(value.binding, run_id=RunId("run-2")),
            ),
            id="run-id",
        ),
        pytest.param(
            lambda value: replace(
                value,
                binding=replace(
                    value.binding,
                    workflow_revision_hash=WorkflowRevision(b"other").revision_hash,
                ),
            ),
            id="workflow-revision",
        ),
        pytest.param(
            lambda value: replace(
                value,
                binding=replace(
                    value.binding,
                    adapter_operational_identity=AdapterOperationalIdentity(
                        "/other/external.sqlite"
                    ),
                ),
            ),
            id="adapter-operational-identity",
        ),
    ],
)
def test_changed_retry_binding_refuses_without_mutation_or_enqueue(
    storage: tuple[DbosRuntime, DbosDurableRunAdvancer, EffectIntent],
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[EffectIntent], EffectIntent],
) -> None:
    _runtime, advancer, intent = storage
    original = advance_run(intent, advancer)
    monkeypatch.setattr(
        DBOSClient,
        "enqueue_in_transaction",
        lambda *args, **kwargs: pytest.fail("conflict enqueued a workflow"),
    )

    with pytest.raises(EffectIntentIdentityConflict):
        advance_run(change(intent), advancer)

    assert advance_run(intent, advancer) == original


def test_second_v1_effect_for_the_run_refuses_without_mutation_or_enqueue(
    storage: tuple[DbosRuntime, DbosDurableRunAdvancer, EffectIntent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, advancer, intent = storage
    advance_run(intent, advancer)
    second = replace(
        intent,
        binding=replace(intent.binding, logical_key=LogicalEffectKey("run-1/action-2")),
    )
    monkeypatch.setattr(
        DBOSClient,
        "enqueue_in_transaction",
        lambda *args, **kwargs: pytest.fail("conflict enqueued a workflow"),
    )

    with pytest.raises(RunEffectConflict):
        advance_run(second, advancer)

    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_intents))
            == 1
        )


def test_concurrent_distinct_v1_effects_have_one_complete_winner_and_one_typed_loser(
    storage: tuple[DbosRuntime, DbosDurableRunAdvancer, EffectIntent],
) -> None:
    runtime, advancer, first = storage
    second = replace(
        first,
        binding=replace(first.binding, logical_key=LogicalEffectKey("run-1/action-2")),
    )
    barrier = Barrier(2)

    def submit(intent: EffectIntent) -> EffectIntentSnapshot | RunEffectConflict:
        barrier.wait(timeout=5)
        try:
            return advance_run(intent, advancer)
        except RunEffectConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (first, second)))

    assert sum(isinstance(result, EffectIntentSnapshot) for result in results) == 1
    assert sum(isinstance(result, RunEffectConflict) for result in results) == 1
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_intents))
            == 1
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status "
                    "WHERE workflow_uuid LIKE 'atelier2-effect-%'"
                )
            )
            == 1
        )
