from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from typing import Never

import pytest
import sqlalchemy as sa
from dbos import SQLAlchemyDatasource
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.advancer import (
    DbosDurableRunAdvancer,
    graph_action_intent,
)
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeBindingConflict,
    DbosRuntimeLeaseClosed,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    bootstrap_workflow_id_for,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.advance_run import advance_run
from atelier2.application.start_run import start_run
from atelier2.contracts.agents import (
    AgentExecutionCapability,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    ProviderId,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    PerformedEffect,
)
from atelier2.contracts.runs import RunId, RunState, StartRunRequest, WorkflowRevision
from atelier2.ports.agent_executions import (
    AgentExecutorFactoryV2,
    AgentExecutorKey,
)
from atelier2.ports.effects import EffectAdapter
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    commit_configured_agent,
    failing_agent_executor_factory,
)
from tests.scenarios.runtime import exact_output_runtime

WORKFLOW_TIMEOUT_SECONDS = 5.0
WORKFLOW_POLL_SECONDS = 0.025
BARRIER_TIMEOUT_SECONDS = 5.0
WORKFLOW_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: waiting, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: waiting}
  - {id: agent, type: agent, job: job-17, output: request, next: action}
"""

AcquireLease = Callable[[DbosRuntimeSettings], DbosRuntime]


class CountingAdapter:
    def __init__(self, delegate: EffectAdapter) -> None:
        self._delegate = delegate
        self.closes = 0

    def readback(self, intent: EffectIntent) -> EffectReadback:
        return self._delegate.readback(intent)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        return self._delegate.execute(intent)

    def close(self) -> None:
        self.closes += 1
        self._delegate.close()


class CountingFactory:
    def __init__(self, delegate: LoopbackEffectAdapterFactory) -> None:
        self._delegate = delegate
        self.opens = 0
        self.opened: CountingAdapter | None = None

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    def open(self) -> CountingAdapter:
        self.opens += 1
        self.opened = CountingAdapter(self._delegate.open())
        return self.opened


def runtime_settings(
    database_path: Path, application_version: str = "executor-A"
) -> DbosRuntimeSettings:
    return DbosRuntimeSettings(database_path, application_version)


def canonical_database(root: Path) -> Path:
    return root / "atelier.sqlite"


def start_request() -> StartRunRequest:
    return StartRunRequest(RunId("run-1"), WorkflowRevision(WORKFLOW_DOCUMENT))


def starter_for(runtime: DbosRuntime) -> DbosDurableRunStarter:
    return DbosDurableRunStarter(runtime.engine, runtime.settings)


def run_state(engine: Engine, run_id: RunId) -> RunState:
    with engine.connect() as connection:
        state = connection.scalar(
            sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
        )
    return RunState(str(state))


def wait_until_bootstrap_succeeds(engine: Engine, run_id: RunId) -> str:
    deadline = time.monotonic() + WORKFLOW_TIMEOUT_SECONDS
    status = "PENDING"
    while status != "SUCCESS" and time.monotonic() < deadline:
        with engine.connect() as connection:
            status = str(
                connection.scalar(
                    sa.text(
                        "SELECT status FROM workflow_status WHERE workflow_uuid=:id"
                    ),
                    {"id": bootstrap_workflow_id_for(run_id)},
                )
            )
        if status != "SUCCESS":
            time.sleep(WORKFLOW_POLL_SECONDS)
    return status


def wait_until_run_state(engine: Engine, run_id: RunId, expected: RunState) -> RunState:
    deadline = time.monotonic() + WORKFLOW_TIMEOUT_SECONDS
    state = run_state(engine, run_id)
    while state is not expected and time.monotonic() < deadline:
        time.sleep(WORKFLOW_POLL_SECONDS)
        state = run_state(engine, run_id)
    return state


def execute_one_bootstrap(runtime: DbosRuntime) -> RunState:
    runtime.initialize_storage()
    started = start_run(start_request(), starter_for(runtime))
    runtime.launch()
    assert wait_until_bootstrap_succeeds(runtime.engine, started.run_id) == "SUCCESS"
    return wait_until_run_state(runtime.engine, started.run_id, RunState.WAITING_INPUT)


@pytest.fixture
def acquire() -> Iterator[AcquireLease]:
    leases: list[DbosRuntime] = []

    def acquire_lease(settings: DbosRuntimeSettings) -> DbosRuntime:
        lease = exact_output_runtime(
            settings,
            LoopbackEffectAdapterFactory(
                settings.database_path.parent / "external-effect.sqlite",
                AdapterRevision("loopback-v1"),
                EffectDestination("loopback-test"),
            ),
        )
        leases.append(lease)
        return lease

    yield acquire_lease
    for lease in reversed(leases):
        lease.close()


def test_identical_settings_share_one_process_runtime(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)

    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))

    assert second.engine is first.engine
    assert second.datasource is first.datasource


def test_an_equivalently_spelled_database_path_is_the_same_binding(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))

    second = acquire(runtime_settings(tmp_path / "nested" / ".." / database.name))

    assert second.engine is first.engine


@pytest.mark.parametrize(
    "conflicting",
    [
        pytest.param(
            lambda root: runtime_settings(root / "other.sqlite"), id="other-database"
        ),
        pytest.param(
            lambda root: runtime_settings(canonical_database(root), "executor-B"),
            id="other-application-version",
        ),
    ],
)
def test_an_incompatible_second_binding_is_refused_and_the_active_one_keeps_working(
    acquire: AcquireLease,
    tmp_path: Path,
    conflicting: Callable[[Path], DbosRuntimeSettings],
) -> None:
    active = acquire(runtime_settings(canonical_database(tmp_path)))

    with pytest.raises(DbosRuntimeBindingConflict):
        acquire(conflicting(tmp_path))

    assert execute_one_bootstrap(active) is RunState.WAITING_INPUT


def test_a_refused_binding_opens_no_second_canonical_store(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    acquire(runtime_settings(canonical_database(tmp_path)))
    refused = tmp_path / "second" / "atelier.sqlite"

    with pytest.raises(DbosRuntimeBindingConflict):
        acquire(runtime_settings(refused))

    assert not refused.parent.exists()


def test_closing_one_of_two_identical_leases_keeps_the_executor_running(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))
    first.initialize_storage()
    first.launch()

    first.close()

    started = start_run(start_request(), starter_for(second))
    assert wait_until_bootstrap_succeeds(second.engine, started.run_id) == "SUCCESS"
    assert (
        wait_until_run_state(second.engine, started.run_id, RunState.WAITING_INPUT)
        is RunState.WAITING_INPUT
    )


def test_initializing_storage_from_a_second_lease_keeps_the_executor_running(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))
    first.initialize_storage()
    first.launch()

    second.initialize_storage()

    started = start_run(start_request(), starter_for(first))
    assert wait_until_bootstrap_succeeds(first.engine, started.run_id) == "SUCCESS"
    assert (
        wait_until_run_state(first.engine, started.run_id, RunState.WAITING_INPUT)
        is RunState.WAITING_INPUT
    )


def test_the_last_close_releases_the_binding_for_a_different_one(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))
    first.close()
    second.close()

    rebound = acquire(runtime_settings(tmp_path / "second.sqlite", "executor-B"))

    assert execute_one_bootstrap(rebound) is RunState.WAITING_INPUT


def test_closing_one_lease_twice_does_not_release_the_other(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))

    first.close()
    first.close()

    with pytest.raises(DbosRuntimeBindingConflict):
        acquire(runtime_settings(tmp_path / "second.sqlite"))
    assert acquire(runtime_settings(database)).engine is second.engine


@pytest.mark.parametrize(
    "use_lease",
    [
        pytest.param(lambda lease: lease.engine, id="engine"),
        pytest.param(lambda lease: lease.datasource, id="datasource"),
        pytest.param(lambda lease: lease.settings, id="settings"),
        pytest.param(lambda lease: lease.launch(), id="launch"),
        pytest.param(lambda lease: lease.initialize_storage(), id="initialize-storage"),
    ],
)
def test_a_closed_lease_refuses_further_use(
    acquire: AcquireLease,
    tmp_path: Path,
    use_lease: Callable[[DbosRuntime], object],
) -> None:
    lease = acquire(runtime_settings(canonical_database(tmp_path)))
    lease.close()

    with pytest.raises(DbosRuntimeLeaseClosed):
        use_lease(lease)


def test_concurrent_closes_of_one_lease_release_it_exactly_once(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    closers = 2
    barrier = Barrier(closers)
    database = canonical_database(tmp_path)
    released = acquire(runtime_settings(database))
    held = acquire(runtime_settings(database))

    def close_together() -> None:
        barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        released.close()

    with ThreadPoolExecutor(max_workers=closers) as pool:
        for future in [pool.submit(close_together) for _ in range(closers)]:
            future.result()

    with pytest.raises(DbosRuntimeBindingConflict):
        acquire(runtime_settings(tmp_path / "second.sqlite"))
    assert execute_one_bootstrap(held) is RunState.WAITING_INPUT


def test_concurrent_identical_acquisitions_hold_one_counted_binding(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    participants = 4
    barrier = Barrier(participants)
    database = canonical_database(tmp_path)

    def acquire_together() -> DbosRuntime:
        barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        return acquire(runtime_settings(database))

    with ThreadPoolExecutor(max_workers=participants) as pool:
        futures = [pool.submit(acquire_together) for _ in range(participants)]
        leases = [future.result() for future in futures]

    assert all(lease.engine is leases[0].engine for lease in leases)
    for lease in leases[:-1]:
        lease.close()
    with pytest.raises(DbosRuntimeBindingConflict):
        acquire(runtime_settings(tmp_path / "second.sqlite"))
    leases[-1].close()

    rebound = acquire(runtime_settings(tmp_path / "second.sqlite"))
    assert rebound.settings.database_path == tmp_path / "second.sqlite"


def test_equivalent_factories_open_once_and_last_lease_closes_once(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(canonical_database(tmp_path))
    first_factory = CountingFactory(
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        )
    )
    second_factory = CountingFactory(first_factory._delegate)
    first = exact_output_runtime(settings, first_factory)
    second = exact_output_runtime(settings, second_factory)

    assert first_factory.opens == 1
    assert second_factory.opens == 0
    assert first.effect_adapter is second.effect_adapter
    assert first_factory.opened is not None
    first.close()
    assert first_factory.opened.closes == 0
    second.close()
    assert first_factory.opened.closes == 1


def test_incompatible_factory_is_refused_before_it_opens_or_mutates_its_store(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(canonical_database(tmp_path))
    active = exact_output_runtime(
        settings,
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    refused_path = tmp_path / "refused" / "external.sqlite"
    refused = CountingFactory(
        LoopbackEffectAdapterFactory(
            refused_path,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        )
    )
    try:
        with pytest.raises(DbosRuntimeBindingConflict):
            exact_output_runtime(settings, refused)
        assert refused.opens == 0
        assert not refused_path.parent.exists()
    finally:
        active.close()


def test_initialization_failure_closes_the_opened_adapter_and_releases_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = runtime_settings(canonical_database(tmp_path))
    factory = CountingFactory(
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        )
    )

    def fail_datasource(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected datasource failure")

    with monkeypatch.context() as context:
        context.setattr(SQLAlchemyDatasource, "create", fail_datasource)
        with pytest.raises(RuntimeError, match="injected datasource failure"):
            exact_output_runtime(settings, factory)

    assert factory.opens == 1
    assert factory.opened is not None
    assert factory.opened.closes == 1
    recovered = exact_output_runtime(settings, factory._delegate)
    recovered.close()


def test_restart_refuses_a_store_identity_different_from_the_durable_intent(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(canonical_database(tmp_path))
    original_factory = LoopbackEffectAdapterFactory(
        tmp_path / "external.sqlite",
        AdapterRevision("loopback-v1"),
        EffectDestination("loopback-test"),
    )
    runtime = exact_output_runtime(settings, original_factory)
    runtime.initialize_storage()
    started = start_run(start_request(), starter_for(runtime))
    with canonical_write_transaction(runtime.engine) as connection:
        commit_configured_agent(
            connection,
            started.run_id,
            started.revision_hash,
            "agent",
        )
        intent = graph_action_intent(
            connection,
            started.run_id,
            started.revision_hash,
            runtime.effect_adapter_binding,
        )
    advance_run(
        intent,
        DbosDurableRunAdvancer(
            runtime.engine, runtime.settings, runtime.effect_adapter_binding
        ),
    )
    runtime.close()
    changed_path = tmp_path / "changed" / "external.sqlite"

    with pytest.raises(DbosRuntimeBindingConflict, match="durable effect intents"):
        exact_output_runtime(
            settings,
            LoopbackEffectAdapterFactory(
                changed_path,
                AdapterRevision("loopback-v1"),
                EffectDestination("loopback-test"),
            ),
        )

    assert not changed_path.parent.exists()


def test_canonical_and_external_store_must_be_distinct(tmp_path: Path) -> None:
    database = canonical_database(tmp_path)

    with pytest.raises(DbosRuntimeBindingConflict, match="must be distinct"):
        exact_output_runtime(
            runtime_settings(database),
            LoopbackEffectAdapterFactory(
                database,
                AdapterRevision("loopback-v1"),
                EffectDestination("loopback-test"),
            ),
        )


def test_existing_hardlink_alias_is_refused_before_external_store_mutation(
    tmp_path: Path,
) -> None:
    database = canonical_database(tmp_path)
    original = exact_output_runtime(
        runtime_settings(database),
        LoopbackEffectAdapterFactory(
            tmp_path / "original-external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    original.close()
    before = database.read_bytes()
    external_alias = tmp_path / "external-alias.sqlite"
    os.link(database, external_alias)

    with pytest.raises(DbosRuntimeBindingConflict, match="must be distinct"):
        exact_output_runtime(
            runtime_settings(database),
            LoopbackEffectAdapterFactory(
                external_alias,
                AdapterRevision("loopback-v1"),
                EffectDestination("loopback-test"),
            ),
        )

    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'loopback_effect%'"
        ).fetchone() == (0,)


def _runtime_with_v2(
    root: Path,
    factories: tuple[AgentExecutorFactoryV2, ...],
    application_version: str = "v2-life",
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(root / "atelier.sqlite", application_version),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("lifecycle"),
        ),
        ExactOutputAgentExecutorFactory(),
        factories,
    )


class ChangingKeyFactory(RecordingAgentExecutorFactoryV2):
    """A factory answering a different key and identity on every single read."""

    @property
    def key(self) -> AgentExecutorKey:
        self.key_reads += 1
        return AgentExecutorKey(
            ProviderId(f"provider-{self.key_reads}"),
            AgentExecutorRevision(f"executor/{self.key_reads}"),
        )

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        self.identity_reads += 1
        return AgentExecutorOperationalIdentity(f"operation-{self.identity_reads}")


def unstable_key_factory() -> ChangingKeyFactory:
    return ChangingKeyFactory("unstable", "unstable/v1", "unstable-operation", b"")


@dataclass
class BaseExceptionEffectFactory:
    binding_value: EffectAdapterBinding
    failure: BaseException
    lifecycle: list[str]

    @property
    def binding(self) -> EffectAdapterBinding:
        return self.binding_value

    def open(self) -> Never:
        self.lifecycle.append("open:effect")
        raise self.failure


def test_v2_factories_open_sorted_and_last_lease_closes_them_reverse_once(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    first_factories = (
        RecordingAgentExecutorFactoryV2(
            "openai", "codex/v1", "codex-operation", b"", lifecycle
        ),
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude/v1", "claude-operation", b"", lifecycle
        ),
    )
    second_factories = (
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude/v1", "claude-operation", b""
        ),
        RecordingAgentExecutorFactoryV2("openai", "codex/v1", "codex-operation", b""),
    )

    first = _runtime_with_v2(tmp_path, first_factories)
    second = _runtime_with_v2(tmp_path, second_factories)

    assert lifecycle == ["open:anthropic", "open:openai"]
    assert [factory.opens for factory in first_factories] == [1, 1]
    assert [factory.opens for factory in second_factories] == [0, 0]
    first.close()
    assert lifecycle == ["open:anthropic", "open:openai"]
    second.close()
    assert lifecycle == [
        "open:anthropic",
        "open:openai",
        "close:openai",
        "close:anthropic",
    ]
    assert all(
        factory.opened is not None and factory.opened.closes == 1
        for factory in first_factories
    )


def test_duplicate_v2_registry_key_is_refused_before_any_factory_opens(
    tmp_path: Path,
) -> None:
    factories = (
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude/v1", "first-operation", b""
        ),
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude/v1", "second-operation", b""
        ),
    )

    with pytest.raises(ValueError, match="keys must be unique"):
        _runtime_with_v2(tmp_path, factories)

    assert [factory.opens for factory in factories] == [0, 0]
    assert [factory.key_reads for factory in factories] == [1, 1]
    assert [factory.identity_reads for factory in factories] == [1, 1]
    assert not (tmp_path / "atelier.sqlite").exists()
    assert not (tmp_path / "effects.sqlite").exists()


def test_v2_registry_without_headless_capability_is_refused_before_open(
    tmp_path: Path,
) -> None:
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic",
        "claude/v1",
        "interactive-only",
        b"",
        capability_set=frozenset({AgentExecutionCapability.INTERACTIVE}),
    )

    with pytest.raises(ValueError, match="headless capability"):
        _runtime_with_v2(tmp_path, (factory,))

    assert factory.opens == 0
    assert not (tmp_path / "atelier.sqlite").exists()
    assert not (tmp_path / "effects.sqlite").exists()


def test_v2_factory_identity_is_captured_once_before_open(tmp_path: Path) -> None:
    factory = unstable_key_factory()
    runtime = _runtime_with_v2(tmp_path, (factory,))
    try:
        assert factory.key_reads == 1
        assert factory.identity_reads == 1
        assert factory.opens == 1
    finally:
        runtime.close()


def test_factory_open_never_enters_agent_invocation(tmp_path: Path) -> None:
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude/v1", "operation", b"unused"
    )
    runtime = _runtime_with_v2(tmp_path, (factory,))
    try:
        assert factory.opens == 1
        assert factory.opened is not None
        assert factory.opened.requests == []
    finally:
        runtime.close()


def test_same_v2_factory_object_is_refused_without_durable_mutation(
    tmp_path: Path,
) -> None:
    factory = unstable_key_factory()
    runtime: DbosRuntime | None = None
    try:
        with pytest.raises(ValueError, match="factory objects must be unique"):
            runtime = _runtime_with_v2(tmp_path, (factory, factory))
    finally:
        if runtime is not None:
            runtime.close()

    assert factory.key_reads == 0
    assert factory.identity_reads == 0
    assert factory.opens == 0
    assert not (tmp_path / "atelier.sqlite").exists()
    assert not (tmp_path / "effects.sqlite").exists()


def test_partial_v2_open_failure_closes_prior_executor_and_releases_owner(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    opened = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude/v1", "claude-operation", b"", lifecycle
    )

    failing = failing_agent_executor_factory(
        "openai", lifecycle, open_failure=RuntimeError("injected V2 open failure")
    )

    with pytest.raises(RuntimeError, match="injected V2 open failure"):
        _runtime_with_v2(tmp_path, (failing, opened))

    assert lifecycle == ["open:anthropic", "open:openai", "close:anthropic"]
    assert opened.opened is not None and opened.opened.closes == 1
    recovered = _runtime_with_v2(tmp_path, ())
    recovered.close()


def test_v2_base_exception_open_closes_prior_executor_and_releases_owner(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    opened = RecordingAgentExecutorFactoryV2(
        "alpha", "alpha/v1", "alpha-operation", b"", lifecycle
    )
    failure = KeyboardInterrupt("open:beta failed")

    with pytest.raises(KeyboardInterrupt) as captured:
        _runtime_with_v2(
            tmp_path,
            (
                opened,
                failing_agent_executor_factory("beta", lifecycle, open_failure=failure),
            ),
        )

    assert captured.value is failure
    assert lifecycle == ["open:alpha", "open:beta", "close:alpha"]
    assert opened.opened is not None and opened.opened.closes == 1
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()


def test_v2_registration_failure_closes_all_opened_executors_reverse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle: list[str] = []
    factories = (
        RecordingAgentExecutorFactoryV2(
            "openai", "codex/v1", "codex-operation", b"", lifecycle
        ),
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude/v1", "claude-operation", b"", lifecycle
        ),
    )

    def fail_registration(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected registration failure")

    with monkeypatch.context() as context:
        context.setattr(
            "atelier2.adapters.dbos.runtime.register_durable_run_workflow",
            fail_registration,
        )
        with pytest.raises(RuntimeError, match="injected registration failure"):
            _runtime_with_v2(tmp_path, factories)

    assert lifecycle == [
        "open:anthropic",
        "open:openai",
        "close:openai",
        "close:anthropic",
    ]
    assert all(
        factory.opened is not None and factory.opened.closes == 1
        for factory in factories
    )
    recovered = _runtime_with_v2(tmp_path, ())
    recovered.close()


def test_v2_open_and_cleanup_failures_preserve_original_then_cleanup_order(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []

    with pytest.raises(ExceptionGroup) as captured:
        _runtime_with_v2(
            tmp_path,
            (
                failing_agent_executor_factory(
                    "openai", lifecycle, open_failure=RuntimeError("open:openai failed")
                ),
                failing_agent_executor_factory(
                    "anthropic",
                    lifecycle,
                    close_failure=RuntimeError("cleanup:anthropic"),
                ),
            ),
        )

    assert lifecycle == ["open:anthropic", "open:openai", "close:anthropic"]
    assert [str(error) for error in captured.value.exceptions] == [
        "open:openai failed",
        "cleanup:anthropic",
    ]
    recovered = _runtime_with_v2(tmp_path, ())
    recovered.close()


def test_base_exception_open_and_multiple_cleanup_failures_preserve_order(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    alpha_cleanup = KeyboardInterrupt("cleanup:alpha")
    beta_cleanup = SystemExit("cleanup:beta")
    original = KeyboardInterrupt("open:gamma")

    with pytest.raises(BaseExceptionGroup) as captured:
        _runtime_with_v2(
            tmp_path,
            (
                failing_agent_executor_factory(
                    "alpha", lifecycle, close_failure=alpha_cleanup
                ),
                failing_agent_executor_factory(
                    "beta", lifecycle, close_failure=beta_cleanup
                ),
                failing_agent_executor_factory(
                    "gamma", lifecycle, open_failure=original
                ),
            ),
        )

    assert lifecycle == [
        "open:alpha",
        "open:beta",
        "open:gamma",
        "close:beta",
        "close:alpha",
    ]
    assert captured.value.exceptions == (original, beta_cleanup, alpha_cleanup)
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()


def test_registration_base_exception_runs_all_cleanup_and_preserves_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle: list[str] = []
    original = SystemExit("registration")
    factory = RecordingAgentExecutorFactoryV2(
        "alpha", "alpha/v1", "alpha-operation", b"", lifecycle
    )

    def fail_registration(*_arguments: object, **_keywords: object) -> Never:
        lifecycle.append("register")
        raise original

    with monkeypatch.context() as context:
        context.setattr(
            "atelier2.adapters.dbos.runtime.register_durable_run_workflow",
            fail_registration,
        )
        with pytest.raises(SystemExit) as captured:
            _runtime_with_v2(tmp_path, (factory,))

    assert captured.value is original
    assert lifecycle == ["open:alpha", "register", "close:alpha"]
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()


def test_effect_open_base_exception_closes_provider_and_releases_owner(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    original = KeyboardInterrupt("effect open")
    factory = RecordingAgentExecutorFactoryV2(
        "alpha", "alpha/v1", "alpha-operation", b"", lifecycle
    )
    effect_factory = BaseExceptionEffectFactory(
        EffectAdapterBinding(
            AdapterRevision("failing/v1"),
            EffectDestination("failing"),
            AdapterOperationalIdentity(str((tmp_path / "effects.sqlite").resolve())),
        ),
        original,
        lifecycle,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        DbosRuntime(
            DbosRuntimeSettings(tmp_path / "atelier.sqlite", "effect-open"),
            effect_factory,
            ExactOutputAgentExecutorFactory(),
            (factory,),
        )

    assert captured.value is original
    assert lifecycle == ["open:alpha", "open:effect", "close:alpha"]
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()


def test_last_close_runs_every_v2_cleanup_and_releases_owner_despite_failures(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    runtime = _runtime_with_v2(
        tmp_path,
        (
            failing_agent_executor_factory(
                "openai", lifecycle, close_failure=RuntimeError("cleanup:openai")
            ),
            failing_agent_executor_factory(
                "anthropic", lifecycle, close_failure=RuntimeError("cleanup:anthropic")
            ),
        ),
    )

    with pytest.raises(ExceptionGroup) as captured:
        runtime.close()

    assert lifecycle == [
        "open:anthropic",
        "open:openai",
        "close:openai",
        "close:anthropic",
    ]
    assert [str(error) for error in captured.value.exceptions] == [
        "cleanup:openai",
        "cleanup:anthropic",
    ]
    runtime.close()
    recovered = _runtime_with_v2(tmp_path, ())
    recovered.close()


def test_last_close_continues_after_base_exception_and_resets_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle: list[str] = []
    factories = (
        RecordingAgentExecutorFactoryV2(
            "beta", "beta/v1", "beta-operation", b"", lifecycle
        ),
        RecordingAgentExecutorFactoryV2(
            "alpha", "alpha/v1", "alpha-operation", b"", lifecycle
        ),
    )
    runtime = _runtime_with_v2(tmp_path, factories)
    failure = SystemExit("effect close failed")
    disposed: list[str] = []
    original_dispose = runtime.engine.dispose

    def fail_effect_close() -> Never:
        lifecycle.append("close:effect")
        raise failure

    def dispose() -> None:
        disposed.append("engine")
        original_dispose()

    with monkeypatch.context() as context:
        context.setattr(runtime.effect_adapter, "close", fail_effect_close)
        context.setattr(runtime.engine, "dispose", dispose)
        with pytest.raises(SystemExit) as captured:
            runtime.close()

    assert captured.value is failure
    assert lifecycle == [
        "open:alpha",
        "open:beta",
        "close:effect",
        "close:beta",
        "close:alpha",
    ]
    assert disposed == ["engine"]
    assert all(
        factory.opened is not None and factory.opened.closes == 1
        for factory in factories
    )
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()


def test_last_close_aggregates_destroy_close_and_dispose_base_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle: list[str] = []
    alpha_cleanup = KeyboardInterrupt("cleanup:alpha")
    beta_cleanup = SystemExit("cleanup:beta")
    runtime = _runtime_with_v2(
        tmp_path,
        (
            failing_agent_executor_factory(
                "alpha", lifecycle, close_failure=alpha_cleanup
            ),
            failing_agent_executor_factory(
                "beta", lifecycle, close_failure=beta_cleanup
            ),
        ),
    )
    destroy_failure = KeyboardInterrupt("destroy")
    effect_failure = SystemExit("cleanup:effect")
    dispose_failure = KeyboardInterrupt("dispose")

    def destroy(**_arguments: object) -> Never:
        lifecycle.append("destroy")
        raise destroy_failure

    def close_effect() -> Never:
        lifecycle.append("close:effect")
        raise effect_failure

    def dispose() -> Never:
        lifecycle.append("dispose")
        raise dispose_failure

    with monkeypatch.context() as context:
        context.setattr("atelier2.adapters.dbos.runtime.DBOS.destroy", destroy)
        context.setattr(runtime.effect_adapter, "close", close_effect)
        context.setattr(runtime.engine, "dispose", dispose)
        with pytest.raises(BaseExceptionGroup) as captured:
            runtime.close()

    assert lifecycle == [
        "open:alpha",
        "open:beta",
        "destroy",
        "close:effect",
        "close:beta",
        "close:alpha",
        "dispose",
    ]
    assert captured.value.exceptions == (
        destroy_failure,
        effect_failure,
        beta_cleanup,
        alpha_cleanup,
        dispose_failure,
    )
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()
