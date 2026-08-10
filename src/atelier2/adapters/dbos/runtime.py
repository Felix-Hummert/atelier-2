from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from dbos import DBOS, DBOSConfig, SQLAlchemyDatasource
from sqlalchemy import event
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.schema import initialize_schema
from atelier2.adapters.dbos.workflow import QUEUE_NAME, register_durable_run_workflow

EXECUTOR_ID = "atelier2-local"
_SQLITE_LOCK_TIMEOUT_SECONDS = 30.0
_SQLITE_WAL_RETRY_SECONDS = 0.01
_SQLITE_RETRYABLE_ERRORS = frozenset((sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
_SHUTDOWN_WORKFLOW_COMPLETION_SECONDS = 1


class DbosRuntimeBindingConflict(RuntimeError):
    """A second, incompatible DBOS binding was requested inside one process."""


class DbosRuntimeLeaseClosed(RuntimeError):
    """A released lease on the process DBOS runtime was used again."""


@dataclass(frozen=True)
class DbosRuntimeBinding:
    """What a process globally binds while it owns the DBOS runtime.

    ADR 0001 also binds an effect adapter revision into effect identity, but no
    effect adapter exists behind a port yet, so there is nothing honest to
    identify here until one has a caller.
    """

    canonical_database_path: Path
    application_version: str


@dataclass(frozen=True)
class DbosRuntimeSettings:
    database_path: Path
    application_version: str

    def __post_init__(self) -> None:
        if not self.application_version.strip():
            raise ValueError("application_version must be nonempty")

    @property
    def binding(self) -> DbosRuntimeBinding:
        return DbosRuntimeBinding(
            self.database_path.resolve(), self.application_version
        )


def sqlite_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.resolve()}"


def create_canonical_engine(database_path: Path) -> Engine:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = sa.create_engine(
        sqlite_url(database_path),
        connect_args={
            "check_same_thread": False,
            "timeout": _SQLITE_LOCK_TIMEOUT_SECONDS,
        },
    )

    @event.listens_for(engine, "connect")
    def configure(connection: Any, _record: Any) -> None:
        connection.isolation_level = "IMMEDIATE"
        connection.execute(
            f"PRAGMA busy_timeout={int(_SQLITE_LOCK_TIMEOUT_SECONDS * 1000)}"
        )
        connection.execute("PRAGMA foreign_keys=ON")
        _establish_wal_journal_mode(connection)

    return engine


def _establish_wal_journal_mode(connection: Any) -> None:
    deadline = time.monotonic() + _SQLITE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        except sqlite3.OperationalError as error:
            if (
                error.sqlite_errorcode not in _SQLITE_RETRYABLE_ERRORS
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(_SQLITE_WAL_RETRY_SECONDS)
            continue
        if journal_mode != "wal":
            raise RuntimeError("canonical SQLite database requires WAL journal mode")
        return


@dataclass
class _BoundRuntime:
    settings: DbosRuntimeSettings
    engine: Engine
    datasource: SQLAlchemyDatasource
    leases: int = 0
    launched: bool = False
    storage_ready: bool = False


def _open_binding(settings: DbosRuntimeSettings) -> _BoundRuntime:
    engine = create_canonical_engine(settings.database_path)
    try:
        initialize_schema(engine)
        datasource = SQLAlchemyDatasource.create(
            sqlite_url(settings.database_path), engine=engine
        )
        register_durable_run_workflow(datasource)
    except Exception:
        engine.dispose()
        raise
    return _BoundRuntime(settings, engine, datasource)


def _dbos_config(settings: DbosRuntimeSettings, engine: Engine) -> DBOSConfig:
    return {
        "name": "atelier2",
        "system_database_url": sqlite_url(settings.database_path),
        "system_database_engine": engine,
        "application_version": settings.application_version,
        "executor_id": EXECUTOR_ID,
        "use_listen_notify": False,
        "notification_listener_polling_interval_sec": 0.01,
    }


class _DbosProcessOwner:
    """Owner of the one DBOS global, canonical engine, and workflow registry a
    process may hold.

    DBOS silently reuses its global singleton, so a second binding would adopt
    the first one's database and application version instead of failing. This
    owner refuses that before any global mutation and counts the leases that
    share the accepted binding, so recovery concurrency stays across processes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bound: _BoundRuntime | None = None

    def acquire(self, settings: DbosRuntimeSettings) -> _BoundRuntime:
        with self._lock:
            if self._bound is None:
                self._bound = _open_binding(settings)
            elif self._bound.settings.binding != settings.binding:
                raise DbosRuntimeBindingConflict(
                    f"this process already owns {self._bound.settings.binding}; "
                    f"refusing {settings.binding}"
                )
            self._bound.leases += 1
            return self._bound

    def release(self, bound: _BoundRuntime) -> None:
        with self._lock:
            bound.leases -= 1
            if bound.leases > 0:
                return
            DBOS.destroy(
                destroy_registry=True,
                workflow_completion_timeout_sec=(
                    _SHUTDOWN_WORKFLOW_COMPLETION_SECONDS if bound.launched else 0
                ),
            )
            bound.engine.dispose()
            self._bound = None

    def launch(self, bound: _BoundRuntime) -> None:
        with self._lock:
            if bound.launched:
                return
            self._start(bound)
            bound.launched = True

    def initialize_storage(self, bound: _BoundRuntime) -> None:
        with self._lock:
            if bound.storage_ready:
                return
            self._start(bound)
            DBOS.destroy()

    @staticmethod
    def _start(bound: _BoundRuntime) -> None:
        DBOS(config=_dbos_config(bound.settings, bound.engine))
        DBOS.launch()
        DBOS.register_queue(
            QUEUE_NAME, polling_interval_sec=0.05, on_conflict="always_update"
        )
        bound.storage_ready = True


_PROCESS_OWNER = _DbosProcessOwner()


class DbosRuntime:
    """One lease on the process-global DBOS runtime binding.

    Closing releases that lease exactly once, so concurrent closes of one lease
    cannot destroy a binding another lease still holds.
    """

    def __init__(self, settings: DbosRuntimeSettings) -> None:
        self._close_lock = threading.Lock()
        self._bound: _BoundRuntime | None = _PROCESS_OWNER.acquire(settings)

    @property
    def settings(self) -> DbosRuntimeSettings:
        return self._held().settings

    @property
    def engine(self) -> Engine:
        return self._held().engine

    @property
    def datasource(self) -> SQLAlchemyDatasource:
        return self._held().datasource

    def launch(self) -> None:
        _PROCESS_OWNER.launch(self._held())

    def initialize_storage(self) -> None:
        _PROCESS_OWNER.initialize_storage(self._held())

    def close(self) -> None:
        with self._close_lock:
            bound = self._bound
            if bound is None:
                return
            self._bound = None
            _PROCESS_OWNER.release(bound)

    def _held(self) -> _BoundRuntime:
        if self._bound is None:
            raise DbosRuntimeLeaseClosed("this DBOS runtime lease is already closed")
        return self._bound
