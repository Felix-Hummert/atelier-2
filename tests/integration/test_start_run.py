from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from itertools import repeat
from pathlib import Path
from threading import Barrier
from typing import Any, NoReturn

import pytest
import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy import exc
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
    create_canonical_engine,
)
from atelier2.adapters.dbos.schema import (
    MigrationRequired,
    UnsupportedSchemaVersion,
    initialize_schema,
    workflow_revisions,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    bootstrap_workflow_id_for,
)
from atelier2.adapters.dbos.workflow import bootstrap_run_binding
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.start_run import start_run
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.runs import (
    RevisionHashCollision,
    RunId,
    RunIdentityConflict,
    RunState,
    StartRunRequest,
    WorkflowRevision,
)


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[tuple[DbosRuntime, DbosDurableRunStarter]]:
    runtime = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "executor-A"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    runtime.initialize_storage()
    try:
        yield runtime, DbosDurableRunStarter(runtime.engine, runtime.settings)
    finally:
        runtime.close()


def request(run_id: str = "run-1", document: bytes = b"workflow-v1") -> StartRunRequest:
    return StartRunRequest(RunId(run_id), WorkflowRevision(document))


def count(engine: sa.Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(sa.text(f"SELECT COUNT(*) FROM {table}")) or 0)


@pytest.mark.parametrize("run_id", ["run-1", " run-1 ", "\N{SNOWMAN}"])
def test_workflow_id_is_deterministic_from_exact_run_id(run_id: str) -> None:
    assert bootstrap_workflow_id_for(RunId(run_id)) == (
        "atelier2-run-" + hashlib.sha256(run_id.encode()).hexdigest()
    )


def test_start_commits_revision_run_and_enqueue_atomically(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage

    started = start_run(request(), starter)

    assert started.state is RunState.STARTED
    with runtime.engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT run_id, revision_hash, state FROM runs")
        ).all() == [("run-1", request().revision.revision_hash.value, "STARTED")]
        assert connection.execute(
            sa.text("SELECT workflow_uuid, application_version FROM workflow_status")
        ).all() == [(bootstrap_workflow_id_for(RunId("run-1")), "executor-A")]


def test_raise_after_real_enqueue_rolls_back_every_record(
    storage: tuple[DbosRuntime, DbosDurableRunStarter], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, starter = storage
    real_enqueue = DBOSClient.enqueue_in_transaction

    def enqueue_then_raise(
        self: DBOSClient,
        connection: Connection | Session,
        options: EnqueueOptions,
        *args: Any,
        **kwargs: Any,
    ) -> NoReturn:
        real_enqueue(self, connection, options, *args, **kwargs)
        raise RuntimeError("injected after real enqueue")

    monkeypatch.setattr(DBOSClient, "enqueue_in_transaction", enqueue_then_raise)

    with pytest.raises(RuntimeError, match="injected"):
        start_run(request(), starter)

    assert count(runtime.engine, "workflow_revisions") == 0
    assert count(runtime.engine, "runs") == 0
    assert count(runtime.engine, "workflow_status") == 0


def test_identical_retry_returns_current_run_without_enqueueing_again(
    storage: tuple[DbosRuntime, DbosDurableRunStarter], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, starter = storage
    first = start_run(request(), starter)

    def unexpected_enqueue(*args: object, **kwargs: object) -> object:
        raise AssertionError("retry enqueued again")

    monkeypatch.setattr(DBOSClient, "enqueue_in_transaction", unexpected_enqueue)
    second = start_run(request(), starter)

    assert second == first
    assert count(runtime.engine, "runs") == 1
    assert count(runtime.engine, "workflow_status") == 1


@pytest.mark.parametrize("state", [RunState.WAITING_RECONCILIATION, RunState.COMPLETED])
def test_retry_after_progress_returns_the_current_run(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
    state: RunState,
) -> None:
    runtime, starter = storage
    start_run(request(), starter)
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE runs SET state=:state"), {"state": state.value}
        )

    assert start_run(request(), starter).state is state
    assert count(runtime.engine, "workflow_status") == 1


def test_conflicting_run_id_fails_without_mutation(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage
    original = start_run(request(), starter)

    with pytest.raises(RunIdentityConflict):
        start_run(request(document=b"workflow-v2"), starter)

    assert start_run(request(), starter) == original
    assert count(runtime.engine, "workflow_revisions") == 1
    assert count(runtime.engine, "runs") == 1
    assert count(runtime.engine, "workflow_status") == 1


def test_different_runs_share_the_same_exact_revision(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage

    start_run(request("run-1"), starter)
    start_run(request("run-2"), starter)

    assert count(runtime.engine, "workflow_revisions") == 1
    assert count(runtime.engine, "runs") == 2
    assert count(runtime.engine, "workflow_status") == 2


def test_durable_hash_collision_fails_without_run_or_enqueue(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage
    revision = request().revision
    with runtime.engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=b"different durable bytes",
            )
        )

    with pytest.raises(RevisionHashCollision):
        start_run(request(), starter)

    assert count(runtime.engine, "workflow_revisions") == 1
    assert count(runtime.engine, "runs") == 0
    assert count(runtime.engine, "workflow_status") == 0


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_revision_document_cannot_be_updated_or_deleted(
    storage: tuple[DbosRuntime, DbosDurableRunStarter], operation: str
) -> None:
    runtime, starter = storage
    started = start_run(request(), starter)
    statement = (
        "UPDATE workflow_revisions SET document=X'00' WHERE revision_hash=:hash"
        if operation == "UPDATE"
        else "DELETE FROM workflow_revisions WHERE revision_hash=:hash"
    )

    with pytest.raises(exc.IntegrityError), runtime.engine.begin() as connection:
        connection.execute(sa.text(statement), {"hash": started.revision_hash.value})

    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(workflow_revisions.c.document).where(
                    workflow_revisions.c.revision_hash == started.revision_hash.value
                )
            )
            == b"workflow-v1"
        )


@pytest.mark.parametrize("version", [0, 3])
def test_unsupported_schema_version_is_refused_without_mutation(
    tmp_path: Path, version: int
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE atelier_schema_versions(version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO atelier_schema_versions VALUES(?)", (version,))
    before = database_path.read_bytes()
    engine = sa.create_engine(f"sqlite:///{database_path}")

    with pytest.raises(UnsupportedSchemaVersion):
        initialize_schema(engine)

    engine.dispose()
    assert database_path.read_bytes() == before


def test_schema_version_one_requires_an_explicit_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY)"
        )
        connection.execute("INSERT INTO atelier_schema_versions VALUES(1)")
    before = database_path.read_bytes()
    engine = sa.create_engine(f"sqlite:///{database_path}")

    with pytest.raises(MigrationRequired):
        initialize_schema(engine)

    engine.dispose()
    assert database_path.read_bytes() == before


def test_schema_version_two_opens_idempotently(tmp_path: Path) -> None:
    runtime = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "executor-A"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    try:
        initialize_schema(runtime.engine)
        initialize_schema(runtime.engine)
        assert count(runtime.engine, "atelier_schema_versions") == 1
    finally:
        runtime.close()


def test_concurrent_first_schema_initializers_converge_on_version_two(
    tmp_path: Path,
) -> None:
    participants = 4

    def initialize(database_path: Path, barrier: Barrier) -> list[int]:
        engine = create_canonical_engine(database_path)
        try:
            barrier.wait(timeout=5)
            initialize_schema(engine)
            with engine.connect() as connection:
                return list(
                    connection.execute(
                        sa.text("SELECT version FROM atelier_schema_versions")
                    ).scalars()
                )
        finally:
            engine.dispose()

    for round_number in range(8):
        database_path = tmp_path / f"atelier-{round_number}.sqlite"
        barrier = Barrier(participants)

        with ThreadPoolExecutor(max_workers=participants) as pool:
            results = list(
                pool.map(
                    initialize,
                    repeat(database_path, participants),
                    repeat(barrier, participants),
                )
            )

        assert results == [[2]] * participants


def test_initialized_runtime_can_execute_a_later_seeded_workflow(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage
    started = start_run(request(), starter)

    runtime.launch()
    deadline = time.monotonic() + 5
    workflow_state = "PENDING"
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            workflow_state = str(
                connection.scalar(
                    sa.text(
                        "SELECT status FROM workflow_status WHERE workflow_uuid=:id"
                    ),
                    {"id": bootstrap_workflow_id_for(started.run_id)},
                )
            )
        if workflow_state == "SUCCESS":
            break
        time.sleep(0.025)

    assert workflow_state == "SUCCESS"
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT state FROM runs"))
            == RunState.STARTED.value
        )


@pytest.mark.parametrize("application_version", ["", "   "])
def test_runtime_requires_a_nonempty_application_version(
    tmp_path: Path, application_version: str
) -> None:
    with pytest.raises(ValueError, match="application_version"):
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", application_version)


def test_bootstrap_returns_current_state_and_requires_the_exact_run_binding(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage
    started = start_run(request(), starter)

    assert (
        bootstrap_run_binding(runtime.datasource, started.run_id, started.revision_hash)
        is RunState.STARTED
    )

    with pytest.raises(RuntimeError, match="exact durable run binding"):
        bootstrap_run_binding(
            runtime.datasource,
            started.run_id,
            WorkflowRevision(b"other").revision_hash,
        )

    with runtime.engine.begin() as connection:
        connection.execute(sa.text("UPDATE runs SET state='WAITING_RECONCILIATION'"))
    assert (
        bootstrap_run_binding(runtime.datasource, started.run_id, started.revision_hash)
        is RunState.WAITING_RECONCILIATION
    )
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT state FROM runs"))
            == RunState.WAITING_RECONCILIATION.value
        )
