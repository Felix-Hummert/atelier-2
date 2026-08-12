from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from dbos import DBOS, DBOSConfig, SQLAlchemyDatasource
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

from atelier2.adapters.dbos.schema import (
    agent_receipts,
    effect_intents,
    initialize_schema,
)
from atelier2.adapters.dbos.workflow import QUEUE_NAME, register_durable_run_workflow
from atelier2.contracts.agents import (
    AgentExecutorBinding,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
)
from atelier2.ports.agent_executions import AgentExecutor, AgentExecutorFactory
from atelier2.ports.effects import EffectAdapter, EffectAdapterFactory

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

    The resource-free adapter binding participates in compatibility so a
    refused lease cannot open or mutate an unrelated external destination.
    """

    canonical_database_path: Path
    application_version: str
    agent_executor: AgentExecutorBinding
    effect_adapter: EffectAdapterBinding


@dataclass(frozen=True)
class DbosRuntimeSettings:
    database_path: Path
    application_version: str

    def __post_init__(self) -> None:
        if not self.application_version.strip():
            raise ValueError("application_version must be nonempty")

    def binding(
        self,
        agent_executor: AgentExecutorBinding,
        effect_adapter: EffectAdapterBinding,
    ) -> DbosRuntimeBinding:
        return DbosRuntimeBinding(
            self.database_path.resolve(),
            self.application_version,
            agent_executor,
            effect_adapter,
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


@contextmanager
def canonical_write_transaction(engine: Engine) -> Iterator[Connection]:
    """Serialize a read-decide-write invariant from its first observation."""

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


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
    agent_executor_binding: AgentExecutorBinding
    agent_executor: AgentExecutor
    effect_adapter_binding: EffectAdapterBinding
    effect_adapter: EffectAdapter
    leases: int = 0
    launched: bool = False
    storage_ready: bool = False


def _open_binding(
    settings: DbosRuntimeSettings,
    agent_factory: AgentExecutorFactory,
    agent_binding: AgentExecutorBinding,
    effect_factory: EffectAdapterFactory,
    effect_binding: EffectAdapterBinding,
) -> _BoundRuntime:
    canonical_database = settings.database_path.resolve()
    # H2's sole concrete adapter binds its resolved external SQLite path here.
    # This closes file-alias corruption without widening the generic factory port.
    external_database = Path(effect_binding.operational_identity.value)
    same_existing_file = False
    if (
        external_database.is_absolute()
        and canonical_database.exists()
        and external_database.exists()
    ):
        try:
            same_existing_file = canonical_database.samefile(external_database)
        except OSError:
            same_existing_file = True
    if str(canonical_database) == str(external_database) or same_existing_file:
        raise DbosRuntimeBindingConflict(
            "canonical and external effect stores must be distinct"
        )
    engine = create_canonical_engine(settings.database_path)
    agent_executor: AgentExecutor | None = None
    adapter: EffectAdapter | None = None
    try:
        initialize_schema(engine)
        with engine.connect() as connection:
            durable_agent_bindings = {
                AgentExecutorBinding(
                    AgentExecutorRevision(str(record.executor_adapter_revision)),
                    AgentExecutorOperationalIdentity(
                        str(record.executor_operational_identity)
                    ),
                )
                for record in connection.execute(
                    sa.select(
                        agent_receipts.c.executor_adapter_revision,
                        agent_receipts.c.executor_operational_identity,
                    ).distinct()
                )
            }
            durable_bindings = {
                EffectAdapterBinding(
                    AdapterRevision(str(record.adapter_revision)),
                    EffectDestination(str(record.destination_identity)),
                    AdapterOperationalIdentity(
                        str(record.adapter_operational_identity)
                    ),
                )
                for record in connection.execute(
                    sa.select(
                        effect_intents.c.adapter_revision,
                        effect_intents.c.destination_identity,
                        effect_intents.c.adapter_operational_identity,
                    ).distinct()
                )
            }
        if durable_agent_bindings and durable_agent_bindings != {agent_binding}:
            raise DbosRuntimeBindingConflict(
                "runtime agent binding differs from durable agent receipts"
            )
        if durable_bindings and durable_bindings != {effect_binding}:
            raise DbosRuntimeBindingConflict(
                "runtime adapter binding differs from durable effect intents"
            )
        agent_executor = agent_factory.open()
        adapter = effect_factory.open()
        datasource = SQLAlchemyDatasource.create(
            sqlite_url(settings.database_path), engine=engine
        )
        register_durable_run_workflow(
            datasource,
            agent_executor,
            agent_binding,
            adapter,
            effect_binding,
        )
    except Exception:
        try:
            try:
                if adapter is not None:
                    adapter.close()
            finally:
                if agent_executor is not None:
                    agent_executor.close()
        finally:
            engine.dispose()
        raise
    return _BoundRuntime(
        settings,
        engine,
        datasource,
        agent_binding,
        agent_executor,
        effect_binding,
        adapter,
    )


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

    def acquire(
        self,
        settings: DbosRuntimeSettings,
        agent_factory: AgentExecutorFactory,
        effect_factory: EffectAdapterFactory,
    ) -> _BoundRuntime:
        with self._lock:
            agent_binding = agent_factory.binding
            effect_binding = effect_factory.binding
            requested_binding = settings.binding(agent_binding, effect_binding)
            if self._bound is None:
                self._bound = _open_binding(
                    settings,
                    agent_factory,
                    agent_binding,
                    effect_factory,
                    effect_binding,
                )
            elif (
                self._bound.settings.binding(
                    self._bound.agent_executor_binding,
                    self._bound.effect_adapter_binding,
                )
                != requested_binding
            ):
                raise DbosRuntimeBindingConflict(
                    "this process already owns "
                    f"{self._bound.settings.binding(self._bound.agent_executor_binding, self._bound.effect_adapter_binding)}; "
                    f"refusing {requested_binding}"
                )
            self._bound.leases += 1
            return self._bound

    def release(self, bound: _BoundRuntime) -> None:
        with self._lock:
            bound.leases -= 1
            if bound.leases > 0:
                return
            try:
                DBOS.destroy(
                    destroy_registry=True,
                    workflow_completion_timeout_sec=(
                        _SHUTDOWN_WORKFLOW_COMPLETION_SECONDS if bound.launched else 0
                    ),
                )
            finally:
                try:
                    bound.effect_adapter.close()
                finally:
                    try:
                        bound.agent_executor.close()
                    finally:
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

    def __init__(
        self,
        settings: DbosRuntimeSettings,
        effect_adapter_factory: EffectAdapterFactory,
        agent_executor_factory: AgentExecutorFactory,
    ) -> None:
        self._close_lock = threading.Lock()
        self._bound: _BoundRuntime | None = _PROCESS_OWNER.acquire(
            settings, agent_executor_factory, effect_adapter_factory
        )

    @property
    def settings(self) -> DbosRuntimeSettings:
        return self._held().settings

    @property
    def engine(self) -> Engine:
        return self._held().engine

    @property
    def datasource(self) -> SQLAlchemyDatasource:
        return self._held().datasource

    @property
    def effect_adapter(self) -> EffectAdapter:
        return self._held().effect_adapter

    @property
    def agent_executor(self) -> AgentExecutor:
        return self._held().agent_executor

    @property
    def agent_executor_binding(self) -> AgentExecutorBinding:
        return self._held().agent_executor_binding

    @property
    def effect_adapter_binding(self) -> EffectAdapterBinding:
        return self._held().effect_adapter_binding

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
