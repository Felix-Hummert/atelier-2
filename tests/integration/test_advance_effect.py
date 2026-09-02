from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.advancer import (
    EffectIntentIdentityConflict,
)
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import effect_intents
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.runs import (
    complete_v3_agent_node,
    prepare_graph_action,
    publish_pinned_revisions,
    start_published_v3_run,
)
from tests.scenarios.runtime import recording_exact_runtime
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    OPEN_PR_OPERATION,
    V3_EFFECT_LINE_AGENT_JOB,
    V3_EFFECT_LINE_AGENT_NODE_ID,
    V3_EFFECT_LINE_DOCUMENT,
)

REVISION = WorkflowRevision(V3_EFFECT_LINE_DOCUMENT)
RUN_ID = RunId("run-1")
PROVIDER_OUTPUT = b'"draft-17"'


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[DbosRuntime]:
    runtime = recording_exact_runtime(
        canonical_runtime_settings(
            tmp_path, "executor-A", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        PROVIDER_OUTPUT,
    )
    runtime.initialize_storage()
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    start_published_v3_run(
        runtime.engine,
        runtime.settings,
        RUN_ID,
        REVISION,
        runtime.agent_executor_registry,
    )
    complete_v3_agent_node(
        runtime,
        RUN_ID,
        V3_EFFECT_LINE_AGENT_NODE_ID,
        V3_EFFECT_LINE_AGENT_JOB,
        PROVIDER_OUTPUT,
    )
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
            intent.binding.operation_name.value,
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
