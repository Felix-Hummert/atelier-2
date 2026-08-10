from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa
from dbos import SQLAlchemyDatasource
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.advancer import DbosDurableRunAdvancer
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
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.advance_run import advance_run
from atelier2.application.start_run import start_run
from atelier2.contracts.effects import (
    AdapterRevision,
    CanonicalRequest,
    EffectAdapterBinding,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    LogicalEffectKey,
    PerformedEffect,
)
from atelier2.contracts.runs import RunId, RunState, StartRunRequest, WorkflowRevision
from atelier2.ports.effects import EffectAdapter

WORKFLOW_TIMEOUT_SECONDS = 5.0
WORKFLOW_POLL_SECONDS = 0.025
BARRIER_TIMEOUT_SECONDS = 5.0

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
    return StartRunRequest(RunId("run-1"), WorkflowRevision(b"workflow-v1"))


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


def execute_one_bootstrap(runtime: DbosRuntime) -> RunState:
    runtime.initialize_storage()
    started = start_run(start_request(), starter_for(runtime))
    runtime.launch()
    assert wait_until_bootstrap_succeeds(runtime.engine, started.run_id) == "SUCCESS"
    return run_state(runtime.engine, started.run_id)


@pytest.fixture
def acquire() -> Iterator[AcquireLease]:
    leases: list[DbosRuntime] = []

    def acquire_lease(settings: DbosRuntimeSettings) -> DbosRuntime:
        lease = DbosRuntime(
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

    assert execute_one_bootstrap(active) is RunState.STARTED


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
    assert run_state(second.engine, started.run_id) is RunState.STARTED


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
    assert run_state(first.engine, started.run_id) is RunState.STARTED


def test_the_last_close_releases_the_binding_for_a_different_one(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))
    first.close()
    second.close()

    rebound = acquire(runtime_settings(tmp_path / "second.sqlite", "executor-B"))

    assert execute_one_bootstrap(rebound) is RunState.STARTED


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
    assert execute_one_bootstrap(held) is RunState.STARTED


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
    first = DbosRuntime(settings, first_factory)
    second = DbosRuntime(settings, second_factory)

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
    active = DbosRuntime(
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
            DbosRuntime(settings, refused)
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
            DbosRuntime(settings, factory)

    assert factory.opens == 1
    assert factory.opened is not None
    assert factory.opened.closes == 1
    recovered = DbosRuntime(settings, factory._delegate)
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
    runtime = DbosRuntime(settings, original_factory)
    runtime.initialize_storage()
    started = start_run(start_request(), starter_for(runtime))
    intent = EffectIntent(
        EffectBinding(
            LogicalEffectKey("run-1/action-1"),
            started.run_id,
            started.revision_hash,
            original_factory.binding.adapter_revision,
            original_factory.binding.destination,
            original_factory.binding.operational_identity,
        ),
        CanonicalRequest(b"request"),
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
        DbosRuntime(
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
        DbosRuntime(
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
    original = DbosRuntime(
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
        DbosRuntime(
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
