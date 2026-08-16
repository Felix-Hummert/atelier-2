from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.advancer import (
    EFFECT_WORKFLOW_ID_PREFIX,
    EffectIntentIdentityConflict,
    effect_workflow_id_for,
)
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import effect_intents
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
    LogicalEffectKey,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from tests.scenarios.agents import commit_configured_agent
from tests.scenarios.runs import prepare_graph_action, start_published_v1_run
from tests.scenarios.runtime import exact_output_runtime

WORKFLOW_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: waiting, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: waiting}
  - {id: agent, type: agent, job: job-17, output: draft-17, next: action}
"""
REVISION = WorkflowRevision(WORKFLOW_DOCUMENT)
RUN_ID = RunId("run-1")


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[DbosRuntime]:
    runtime = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "executor-A"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    runtime.initialize_storage()
    start_published_v1_run(runtime.engine, runtime.settings, RUN_ID, REVISION)
    with canonical_write_transaction(runtime.engine) as connection:
        commit_configured_agent(connection, RUN_ID, REVISION.revision_hash, "agent")
    try:
        yield runtime
    finally:
        runtime.close()


def prepare(
    runtime: DbosRuntime, binding: EffectAdapterBinding | None = None
) -> EffectIntentSnapshot:
    return prepare_graph_action(
        runtime.engine,
        RUN_ID,
        REVISION.revision_hash,
        runtime.effect_adapter_binding if binding is None else binding,
    )


def test_effect_workflow_id_is_deterministic_from_the_exact_logical_key() -> None:
    key = LogicalEffectKey(" run-1/action-1 ")
    first = effect_workflow_id_for(key)
    second = effect_workflow_id_for(key)
    other = effect_workflow_id_for(LogicalEffectKey("run-1/action-1"))

    assert first.startswith(EFFECT_WORKFLOW_ID_PREFIX)
    assert first == second
    assert first != other


def test_preparing_the_graph_action_writes_exactly_one_exact_intent(
    storage: DbosRuntime,
) -> None:
    snapshot = prepare(storage)

    intent = snapshot.intent
    assert snapshot == EffectIntentSnapshot(
        intent, EffectIntentState.PREPARED, EffectIntentStateVersion(0)
    )
    with storage.engine.connect() as connection:
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


def test_exact_retry_returns_the_current_durable_snapshot(
    storage: DbosRuntime,
) -> None:
    first = prepare(storage)
    with storage.engine.begin() as connection:
        connection.execute(
            effect_intents.update().values(
                state=EffectIntentState.WAITING_RECONCILIATION.value,
                state_version=1,
            )
        )

    assert prepare(storage) == EffectIntentSnapshot(
        first.intent,
        EffectIntentState.WAITING_RECONCILIATION,
        EffectIntentStateVersion(1),
    )


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            lambda binding: EffectAdapterBinding(
                AdapterRevision("loopback-v2"),
                binding.destination,
                binding.operational_identity,
            ),
            id="adapter",
        ),
        pytest.param(
            lambda binding: EffectAdapterBinding(
                binding.adapter_revision,
                EffectDestination("other"),
                binding.operational_identity,
            ),
            id="destination",
        ),
        pytest.param(
            lambda binding: EffectAdapterBinding(
                binding.adapter_revision,
                binding.destination,
                AdapterOperationalIdentity("/other/external.sqlite"),
            ),
            id="adapter-operational-identity",
        ),
    ],
)
def test_a_retry_under_another_adapter_binding_refuses_without_mutation(
    storage: DbosRuntime,
    change: Callable[[EffectAdapterBinding], EffectAdapterBinding],
) -> None:
    original = prepare(storage)

    with pytest.raises(EffectIntentIdentityConflict):
        prepare(storage, change(storage.effect_adapter_binding))

    assert prepare(storage) == original
    with storage.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_intents))
            == 1
        )


def test_concurrent_preparations_of_one_action_write_exactly_one_intent(
    storage: DbosRuntime,
) -> None:
    barrier = Barrier(2)

    def prepare_after_barrier(_worker: int) -> EffectIntentSnapshot:
        barrier.wait(timeout=5)
        return prepare(storage)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare_after_barrier, (0, 1)))

    assert results[0] == results[1]
    with storage.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_intents))
            == 1
        )
