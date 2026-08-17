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

from atelier2.adapters.agent_processes import (
    AgentProcessSupervisor,
    delegated_cgroup_root,
)
from atelier2.adapters.agent_workspaces import LocalAgentAttemptWorkspaceOwner
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.schema import (
    agent_configuration_revisions,
    agent_receipts,
    auth_profile_revisions,
    effect_intents,
    initialize_schema,
    run_agent_bindings,
    runs,
)
from atelier2.adapters.dbos.workflow import QUEUE_NAME, register_durable_run_workflow
from atelier2.adapters.project_verification import LocalProjectVerificationRunner
from atelier2.contracts.agents import (
    AgentExecutionCapability,
    AgentExecutorBinding,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    ProviderId,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
)
from atelier2.ports.agent_executions import (
    AgentExecutor,
    AgentExecutorFactory,
    AgentExecutorFactoryV2,
    AgentExecutorKey,
    AgentExecutorManifestEntry,
    AgentExecutorRegistry,
    AgentExecutorV2,
)
from atelier2.ports.effects import EffectAdapter, EffectAdapterFactory

EXECUTOR_ID = "atelier2-local"
SQLITE_LOCK_TIMEOUT_SECONDS = 30.0
_SQLITE_WAL_RETRY_SECONDS = 0.01
_SQLITE_RETRYABLE_ERRORS = frozenset((sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
_SHUTDOWN_WORKFLOW_COMPLETION_SECONDS = 1
AGENT_TERMINATION_GRACE_SECONDS = 2.0


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
    agent_executors_v2: tuple[AgentExecutorManifestEntry, ...]
    effect_adapter: EffectAdapterBinding
    agent_process_control_root: Path
    agent_process_cgroup_root: Path
    agent_scratch_root: Path | None
    project_root: Path | None
    agent_termination_grace_seconds: float


@dataclass(frozen=True)
class DbosRuntimeSettings:
    database_path: Path
    application_version: str
    agent_process_control_root: Path | None = None
    agent_process_cgroup_root: Path | None = None
    agent_scratch_root: Path | None = None
    project_root: Path | None = None
    agent_termination_grace_seconds: float = AGENT_TERMINATION_GRACE_SECONDS
    sqlite_lock_timeout_seconds: float = SQLITE_LOCK_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.application_version.strip():
            raise ValueError("application_version must be nonempty")
        if self.agent_termination_grace_seconds <= 0:
            raise ValueError("agent termination grace must be positive")
        if self.sqlite_lock_timeout_seconds <= 0:
            raise ValueError("the SQLite lock timeout must be positive")

    def process_control_root(self) -> Path:
        root = self.agent_process_control_root
        return (
            (self.database_path.parent / ".atelier2-agent-control").resolve()
            if root is None
            else root.resolve()
        )

    def process_cgroup_root(self) -> Path:
        root = self.agent_process_cgroup_root
        return delegated_cgroup_root() if root is None else root.resolve()

    def binding(
        self,
        agent_executor: AgentExecutorBinding,
        agent_executors_v2: tuple[AgentExecutorManifestEntry, ...],
        effect_adapter: EffectAdapterBinding,
    ) -> DbosRuntimeBinding:
        return DbosRuntimeBinding(
            self.database_path.resolve(),
            self.application_version,
            agent_executor,
            agent_executors_v2,
            effect_adapter,
            self.process_control_root(),
            self.process_cgroup_root(),
            None
            if self.agent_scratch_root is None
            else self.agent_scratch_root.resolve(),
            None if self.project_root is None else self.project_root.resolve(),
            self.agent_termination_grace_seconds,
        )


def sqlite_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.resolve()}"


def create_canonical_engine(
    database_path: Path,
    lock_timeout_seconds: float = SQLITE_LOCK_TIMEOUT_SECONDS,
) -> Engine:
    """The one engine every durable path shares, waiting as long as it was told.

    How long to wait for a busy store is the instance's answer, not the code's:
    a laptop and a loaded host disagree honestly, and the serving host passes what
    it was configured with.

    The default is the named owner itself rather than a second number, so a caller
    that says nothing still waits the one documented wait. It stays a default
    because making it required buys nothing here and costs every test that opens a
    store a line of noise: the value has one home either way.
    """

    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = sa.create_engine(
        sqlite_url(database_path),
        connect_args={
            "check_same_thread": False,
            "timeout": lock_timeout_seconds,
        },
    )

    @event.listens_for(engine, "connect")
    def configure(connection: Any, _record: Any) -> None:
        connection.isolation_level = "IMMEDIATE"
        connection.execute(f"PRAGMA busy_timeout={int(lock_timeout_seconds * 1000)}")
        connection.execute("PRAGMA foreign_keys=ON")
        _establish_wal_journal_mode(connection, lock_timeout_seconds)

    return engine


def _establish_wal_journal_mode(
    connection: Any, lock_timeout_seconds: float = SQLITE_LOCK_TIMEOUT_SECONDS
) -> None:
    deadline = time.monotonic() + lock_timeout_seconds
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
    agent_executor_registry: AgentExecutorRegistry
    agent_executors_v2: tuple[tuple[AgentExecutorManifestEntry, AgentExecutorV2], ...]
    effect_adapter_binding: EffectAdapterBinding
    effect_adapter: EffectAdapter
    agent_process_supervisor: AgentProcessSupervisor
    agent_workspace_owner: LocalAgentAttemptWorkspaceOwner | None
    leases: int = 0
    launched: bool = False
    storage_ready: bool = False


def _open_binding(
    settings: DbosRuntimeSettings,
    agent_factory: AgentExecutorFactory,
    agent_binding: AgentExecutorBinding,
    agent_registry: AgentExecutorRegistry,
    agent_manifest: tuple[AgentExecutorManifestEntry, ...],
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
    if agent_registry.keys and settings.agent_scratch_root is None:
        raise DbosRuntimeBindingConflict(
            "serving a provider executor requires an agent scratch root, because "
            "every attempt is started in a workspace of its own"
        )
    engine = create_canonical_engine(
        settings.database_path, settings.sqlite_lock_timeout_seconds
    )
    agent_executor: AgentExecutor | None = None
    agent_executors_v2: list[tuple[AgentExecutorManifestEntry, AgentExecutorV2]] = []
    adapter: EffectAdapter | None = None
    agent_process_supervisor: AgentProcessSupervisor | None = None
    agent_workspace_owner: LocalAgentAttemptWorkspaceOwner | None = None
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
            required_v2_capabilities = {
                (
                    AgentExecutorKey(
                        ProviderId(str(record.provider_id)),
                        AgentExecutorRevision(str(record.executor_revision)),
                    ),
                    AgentExecutionCapability(str(record.requested_capability)),
                )
                for record in connection.execute(
                    sa.select(
                        auth_profile_revisions.c.provider_id,
                        agent_configuration_revisions.c.executor_revision,
                        agent_configuration_revisions.c.requested_capability,
                    )
                    .select_from(runs)
                    .join(
                        run_agent_bindings,
                        run_agent_bindings.c.run_id == runs.c.run_id,
                    )
                    .join(
                        agent_configuration_revisions,
                        agent_configuration_revisions.c.revision_hash
                        == run_agent_bindings.c.agent_configuration_revision_hash,
                    )
                    .join(
                        auth_profile_revisions,
                        auth_profile_revisions.c.revision_hash
                        == agent_configuration_revisions.c.auth_profile_revision_hash,
                    )
                    .where(
                        runs.c.workflow_format_version == 2,
                        runs.c.state != "COMPLETED",
                    )
                    .distinct()
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
        required_v2_keys = {key for key, _capability in required_v2_capabilities}
        if not required_v2_keys.issubset(agent_registry.keys):
            raise DbosRuntimeBindingConflict(
                "runtime V2 registry is missing a nonterminal durable executor binding"
            )
        if any(
            capability not in agent_registry.declared_capabilities(key)
            for key, capability in required_v2_capabilities
        ):
            raise DbosRuntimeBindingConflict(
                "runtime V2 registry lacks a nonterminal durable capability"
            )
        agent_executor = agent_factory.open()
        for registry_entry in agent_registry.entries:
            agent_executors_v2.append(
                (registry_entry.manifest_entry, registry_entry.factory.open())
            )
        adapter = effect_factory.open()
        datasource = SQLAlchemyDatasource.create(
            sqlite_url(settings.database_path), engine=engine
        )
        attempt_store = DbosAgentAttemptStore(engine, settings.application_version)
        agent_process_supervisor = AgentProcessSupervisor(
            attempt_store,
            settings.process_control_root(),
            settings.process_cgroup_root(),
            grace_seconds=settings.agent_termination_grace_seconds,
        )
        if settings.agent_scratch_root is not None:
            agent_workspace_owner = LocalAgentAttemptWorkspaceOwner(
                settings.agent_scratch_root
            )
            # Binding the durable database is the moment a restart can tell an
            # abandoned workspace from a live one, so it is where the workspaces
            # of attempts that ended before the restart are removed.
            agent_workspace_owner.reconcile(attempt_store)
        register_durable_run_workflow(
            datasource,
            agent_executor,
            agent_binding,
            {
                entry.key: (
                    executor,
                    entry.operational_identity,
                    agent_registry.declared_capabilities(entry.key),
                )
                for entry, executor in agent_executors_v2
            },
            attempt_store,
            agent_process_supervisor,
            agent_workspace_owner,
            None
            if settings.project_root is None
            else LocalProjectVerificationRunner(settings.project_root.resolve()),
            adapter,
            effect_binding,
        )
    except BaseException as original:
        cleanup_errors: list[BaseException] = []
        if agent_process_supervisor is not None:
            try:
                agent_process_supervisor.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if agent_workspace_owner is not None:
            try:
                agent_workspace_owner.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        resources: list[EffectAdapter | AgentExecutorV2 | AgentExecutor] = []
        if adapter is not None:
            resources.append(adapter)
        resources.extend(executor for _entry, executor in reversed(agent_executors_v2))
        if agent_executor is not None:
            resources.append(agent_executor)
        for resource in resources:
            try:
                resource.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        try:
            engine.dispose()
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "runtime open and cleanup both failed", [original, *cleanup_errors]
            ) from None
        raise
    return _BoundRuntime(
        settings,
        engine,
        datasource,
        agent_binding,
        agent_executor,
        agent_registry,
        tuple(agent_executors_v2),
        effect_binding,
        adapter,
        agent_process_supervisor,
        agent_workspace_owner,
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
        agent_registry: AgentExecutorRegistry,
        effect_factory: EffectAdapterFactory,
    ) -> _BoundRuntime:
        with self._lock:
            agent_binding = agent_factory.binding
            agent_manifest = agent_registry.manifest
            effect_binding = effect_factory.binding
            requested_binding = settings.binding(
                agent_binding, agent_manifest, effect_binding
            )
            if self._bound is None:
                self._bound = _open_binding(
                    settings,
                    agent_factory,
                    agent_binding,
                    agent_registry,
                    agent_manifest,
                    effect_factory,
                    effect_binding,
                )
            elif (
                self._bound.settings.binding(
                    self._bound.agent_executor_binding,
                    tuple(entry for entry, _executor in self._bound.agent_executors_v2),
                    self._bound.effect_adapter_binding,
                )
                != requested_binding
            ):
                raise DbosRuntimeBindingConflict(
                    "this process already owns "
                    f"{self._bound.settings.binding(self._bound.agent_executor_binding, tuple(entry for entry, _executor in self._bound.agent_executors_v2), self._bound.effect_adapter_binding)}; "
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
                errors: list[BaseException] = []
                try:
                    DBOS.destroy(
                        destroy_registry=True,
                        workflow_completion_timeout_sec=(
                            _SHUTDOWN_WORKFLOW_COMPLETION_SECONDS
                            if bound.launched
                            else 0
                        ),
                    )
                except BaseException as error:
                    errors.append(error)
                resources: list[EffectAdapter | AgentExecutorV2 | AgentExecutor] = [
                    bound.effect_adapter
                ]
                try:
                    bound.agent_process_supervisor.close()
                except BaseException as error:
                    errors.append(error)
                if bound.agent_workspace_owner is not None:
                    try:
                        bound.agent_workspace_owner.close()
                    except BaseException as error:
                        errors.append(error)
                resources.extend(
                    executor for _entry, executor in reversed(bound.agent_executors_v2)
                )
                resources.append(bound.agent_executor)
                for resource in resources:
                    try:
                        resource.close()
                    except BaseException as error:
                        errors.append(error)
                try:
                    bound.engine.dispose()
                except BaseException as error:
                    errors.append(error)
            finally:
                self._bound = None
            if len(errors) == 1:
                raise errors[0]
            if errors:
                raise BaseExceptionGroup("runtime close failed", errors)

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
        agent_executor_factories_v2: tuple[AgentExecutorFactoryV2, ...] = (),
    ) -> None:
        self._close_lock = threading.Lock()
        registry = AgentExecutorRegistry(agent_executor_factories_v2)
        self._bound: _BoundRuntime | None = _PROCESS_OWNER.acquire(
            settings, agent_executor_factory, registry, effect_adapter_factory
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
    def agent_executor_registry(self) -> AgentExecutorRegistry:
        return self._held().agent_executor_registry

    @property
    def agent_process_supervisor(self) -> AgentProcessSupervisor:
        return self._held().agent_process_supervisor

    @property
    def agent_workspace_owner(self) -> LocalAgentAttemptWorkspaceOwner | None:
        return self._held().agent_workspace_owner

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
