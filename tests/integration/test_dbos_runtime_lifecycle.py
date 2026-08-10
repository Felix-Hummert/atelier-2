from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeBindingConflict,
    DbosRuntimeLeaseClosed,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.application.start_run import start_run
from atelier2.contracts.runs import RunId, RunState, StartRunRequest, WorkflowRevision

COMPLETION_TIMEOUT_SECONDS = 5.0
COMPLETION_POLL_SECONDS = 0.025
BARRIER_TIMEOUT_SECONDS = 5.0

AcquireLease = Callable[[DbosRuntimeSettings], DbosRuntime]


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


def wait_until_completed(engine: Engine, run_id: RunId) -> RunState:
    deadline = time.monotonic() + COMPLETION_TIMEOUT_SECONDS
    state = run_state(engine, run_id)
    while state is not RunState.COMPLETED and time.monotonic() < deadline:
        time.sleep(COMPLETION_POLL_SECONDS)
        state = run_state(engine, run_id)
    return state


def execute_one_run(runtime: DbosRuntime) -> RunState:
    runtime.initialize_storage()
    started = start_run(start_request(), starter_for(runtime))
    runtime.launch()
    return wait_until_completed(runtime.engine, started.run_id)


@pytest.fixture
def acquire() -> Iterator[AcquireLease]:
    leases: list[DbosRuntime] = []

    def acquire_lease(settings: DbosRuntimeSettings) -> DbosRuntime:
        lease = DbosRuntime(settings)
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

    assert execute_one_run(active) is RunState.COMPLETED


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
    assert wait_until_completed(second.engine, started.run_id) is RunState.COMPLETED


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
    assert wait_until_completed(first.engine, started.run_id) is RunState.COMPLETED


def test_the_last_close_releases_the_binding_for_a_different_one(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))
    first.close()
    second.close()

    rebound = acquire(runtime_settings(tmp_path / "second.sqlite", "executor-B"))

    assert execute_one_run(rebound) is RunState.COMPLETED


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
    assert execute_one_run(held) is RunState.COMPLETED


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
