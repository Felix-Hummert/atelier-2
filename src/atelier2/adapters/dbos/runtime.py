from __future__ import annotations

import hashlib
import logging
import sqlite3
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import sqlalchemy as sa
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from dbos import DBOS, DBOSConfig, SQLAlchemyDatasource
from sqlalchemy import event
from sqlalchemy.engine import Engine

from atelier2.adapters.agent_processes import (
    AgentProcessSupervisor,
    delegated_cgroup_root,
)
from atelier2.adapters.agent_workspaces import LocalAgentAttemptWorkspaceOwner
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.host_configuration import (
    append_project_root,
    project_root_for,
)
from atelier2.adapters.dbos.names import QUEUE_NAME
from atelier2.adapters.dbos.runner_session_core import DbosRunnerSessionCore
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_configuration_revisions,
    agent_receipts,
    auth_profile_revisions,
    effect_intents,
    initialize_schema,
    run_agent_bindings,
    runs,
)
from atelier2.adapters.dbos.uncontinuable_runs import DbosUncontinuableRunStore
from atelier2.adapters.dbos.workflow import (
    RunnerLeaseAttemptDriver,
    register_durable_run_workflow,
)
from atelier2.adapters.dbos.workflow_ids import driving_workflow_id
from atelier2.adapters.file_runner_leases import FileRunnerLeasePublisher
from atelier2.adapters.free_runner_executor import free_runner_auth_reference
from atelier2.adapters.project_verification import declared_project
from atelier2.adapters.runner_child import REQUIRED_LANDLOCK_ABI
from atelier2.adapters.runner_cli_pins import runner_executor_cli_pin
from atelier2.adapters.runner_lease_session import RunnerLeaseSessionListener
from atelier2.adapters.runner_tls import (
    CORE_DNS_NAME,
    CORE_SESSION_PORT,
    CorePeerDocument,
    SupportedPublicKey,
    core_uri_for_certificate,
    encode_core_peer_document,
    pin_tls_13,
)
from atelier2.application.converge_driverless_attempts import (
    converge_driverless_attempts,
)
from atelier2.application.converge_uncontinuable_runs import (
    converge_uncontinuable_runs,
)
from atelier2.application.execute_agent_attempt_on_runner import (
    ExecuteAgentAttemptOnRunnerOutcome,
    RunnerAttemptLeaseMaterial,
    execute_agent_attempt_on_runner,
)
from atelier2.contracts.agent_attempts import (
    TERMINAL_AGENT_ATTEMPT_STATES,
    AgentAttempt,
    AgentAttemptId,
)
from atelier2.contracts.agents import (
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutorBinding,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.host_configuration import (
    PROJECT_UNKNOWN,
    ProjectId,
    ProjectRootMissing,
    ProjectUnknown,
)
from atelier2.contracts.runner_leases import RunnerLeaseId
from atelier2.contracts.runner_manifests import (
    RunnerManifestV1,
    candidate_runner_manifest,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.agent_executions import (
    AgentExecutor,
    AgentExecutorCarrier,
    AgentExecutorFactory,
    AgentExecutorFactoryV2,
    AgentExecutorKey,
    AgentExecutorManifestEntry,
    AgentExecutorRegistration,
    AgentExecutorRegistry,
    AgentExecutorV2,
)
from atelier2.ports.effects import EffectAdapter, EffectAdapterFactory
from atelier2.ports.project_verification import DeclaredProject
from atelier2.ports.runner_leases import RunnerLeaseWithdrawn

_LOG = logging.getLogger("atelier2")

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


class AgentProcessSupervisorUnavailable(RuntimeError):
    """The runtime has no local process authority for V2/V3 execution."""


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
    agent_process_control_root: Path | None
    agent_process_cgroup_root: Path | None
    agent_scratch_root: Path | None
    project_id: ProjectId | None
    agent_termination_grace_seconds: float | None


@dataclass(frozen=True)
class DbosRuntimeSettings:
    database_path: Path
    application_version: str
    agent_process_control_root: Path | None = None
    agent_process_cgroup_root: Path | None = None
    agent_scratch_root: Path | None = None
    project_id: ProjectId | None = None
    bootstrap_project_root: Path | None = None
    agent_termination_grace_seconds: float = AGENT_TERMINATION_GRACE_SECONDS
    sqlite_lock_timeout_seconds: float = SQLITE_LOCK_TIMEOUT_SECONDS
    # The Runner-lease deployment (`#540` C-3.6): declared together or not at
    # all, because a `RUNNER_LEASE`-carried key served with only some of these
    # would leave `atelier2.adapters.dbos.runtime._runner_lease_attempt_driver`
    # guessing at the rest. `runner_lease_source_commit` is a second carrier of
    # `atelier2.host.serving.HostSettings.source_commit` rather than that same
    # field read twice, because a Runner manifest's `source_commit` is a
    # distinct domain fact (what the served container was built from) that
    # only happens to share a value with the deployment's own provenance today.
    runner_lease_root: Path | None = None
    runner_image: str | None = None
    runner_image_digest: str | None = None
    runner_console_container: str | None = None
    runner_core_identity_directory: Path | None = None
    runner_accept_timeout_seconds: float | None = None
    runner_lease_source_commit: str | None = None

    def __post_init__(self) -> None:
        if not self.application_version.strip():
            raise ValueError("application_version must be nonempty")
        if self.agent_termination_grace_seconds <= 0:
            raise ValueError("agent termination grace must be positive")
        if self.sqlite_lock_timeout_seconds <= 0:
            raise ValueError("the SQLite lock timeout must be positive")
        if self.bootstrap_project_root is not None and self.project_id is None:
            raise ValueError(
                "a bootstrap project root writes the host configuration "
                "channel, so it needs a project id"
            )
        if self.project_id is not None and not isinstance(self.project_id, ProjectId):
            raise TypeError("project id must use its typed contract")
        runner_lease_fields = (
            self.runner_lease_root,
            self.runner_image,
            self.runner_image_digest,
            self.runner_console_container,
            self.runner_core_identity_directory,
            self.runner_accept_timeout_seconds,
            self.runner_lease_source_commit,
        )
        declared = tuple(field for field in runner_lease_fields if field is not None)
        if declared and len(declared) != len(runner_lease_fields):
            raise ValueError(
                "a Runner-lease deployment needs its lease root, image, image "
                "digest, console container, core identity directory, accept "
                "deadline and source commit declared together, not in part"
            )
        if self.runner_accept_timeout_seconds is not None and (
            self.runner_accept_timeout_seconds <= 0
        ):
            raise ValueError("the runner-lease accept deadline must be positive")
        for name in (
            "runner_image",
            "runner_image_digest",
            "runner_console_container",
            "runner_lease_source_commit",
        ):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"{name} must be nonempty")

    @property
    def runner_lease_declared(self) -> bool:
        return self.runner_lease_root is not None

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
        # Only a `LOCAL_PROCESS`-carried key needs Serve's own supervisor and
        # scratch root (`#540` C-3.6): a deployment serving nothing but
        # `RUNNER_LEASE` keys starts without either.
        process_runner_required = any(
            entry.carrier is AgentExecutorCarrier.LOCAL_PROCESS
            for entry in agent_executors_v2
        )
        return DbosRuntimeBinding(
            self.database_path.resolve(),
            self.application_version,
            agent_executor,
            agent_executors_v2,
            effect_adapter,
            self.process_control_root() if process_runner_required else None,
            self.process_cgroup_root() if process_runner_required else None,
            (
                self.agent_scratch_root.resolve()
                if process_runner_required and self.agent_scratch_root is not None
                else None
            ),
            self.project_id,
            self.agent_termination_grace_seconds if process_runner_required else None,
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
    agent_process_supervisor: AgentProcessSupervisor | None
    agent_workspace_owner: LocalAgentAttemptWorkspaceOwner | None
    declared_project: DeclaredProject | None
    leases: int = 0
    launched: bool = False
    storage_ready: bool = False


def _declared_project_for(
    engine: Engine, project_id: ProjectId | None
) -> DeclaredProject | None:
    """The project this process serves, read from the host channel.

    A missing mapping is `project-unknown`: naming a project with no configured
    root is the ADR 0011 service refusal, not the channel's own row miss.
    """

    if project_id is None:
        return None
    try:
        return declared_project(project_root_for(engine, project_id))
    except ProjectRootMissing as missing:
        raise ProjectUnknown(
            f"{PROJECT_UNKNOWN}: project {project_id.value!r} has no configured root"
        ) from missing


class RunnerLeaseAuthReferenceUnowned(ValueError):
    """No production owner resolves an auth reference for this provider yet.

    Only `fake-free` is served as a `RUNNER_LEASE` carrier today (`#540`
    C-3.6's fake-free-only slice); a real provider's auth reference is
    `#540`'s own "not in this slice" boundary (real providers wait on `#15`
    and B-3).
    """


def _runner_manifest_for(
    request: AgentExecutionRequestV2, source_commit: str, image_digest: str
) -> RunnerManifestV1:
    """One Attempt's Runner manifest, built from its own durable request.

    Every carrier fact but the two the deployment itself declares
    (`source_commit`, `image_digest`) comes from the request's own resolved
    binding, so a manifest can never name an executor, provider or capability
    other than the one the durable Attempt was bound to.
    """

    configuration = request.resolved_binding.configuration
    auth = request.resolved_binding.auth_profile
    return candidate_runner_manifest(
        source_commit=source_commit,
        image_digest=image_digest,
        required_landlock_abi=REQUIRED_LANDLOCK_ABI,
        executor_revision=configuration.executor_revision.value,
        executor_operational_identity=request.executor_operational_identity.value,
        provider_id=auth.provider_id.value,
        auth_mode=auth.auth_mode.value,
        requested_capability=configuration.requested_capability.value,
    )


def _runner_auth_reference_for(profile: AuthProfileRevision) -> str:
    if profile.provider_id.value == "fake-free":
        return free_runner_auth_reference(profile).value
    raise RunnerLeaseAuthReferenceUnowned(
        f"no runner-lease auth reference owner for provider {profile.provider_id.value!r}"
    )


def runner_lease_cancellation_command_id(attempt_id: AgentAttemptId) -> str:
    """The persisted `cancellation_command_id` a Runner-lease Attempt's session
    core cancels under (`#540` C-3.6): one owner for the token the driver and
    its tests must spell identically, so neither can drift from the other."""

    return f"runner-lease-cancel:{attempt_id.value}"


# One Core session listener binds one fixed port per Serve process
# (`atelier2.adapters.runner_tls.CORE_SESSION_PORT`), so a Runner-lease Attempt
# opens exactly one connection in this fake-free-only slice: the normal
# lifetime never reconnects, and a reconnect after a mid-session Serve crash is
# Kind #585 (`#540`)'s job, not this driver's.
_RUNNER_SESSION_CONNECTION_ATTEMPTS = 1


@dataclass(frozen=True, slots=True)
class DbosRunnerLeaseAttemptDriver:
    """The production `RunnerLeaseAttemptDriver`: one Attempt's manifest and
    lease material, freshly composed from the durable request, driven over
    the real Runner-lease session (`#540` C-3.4's own
    `execute_agent_attempt_on_runner`)."""

    store: DbosAgentAttemptStore
    engine: Engine
    leases: FileRunnerLeasePublisher
    transport: RunnerLeaseSessionListener
    source_commit: str
    image_digest: str
    runner_image: str
    serve_container: str
    ca_certificate: bytes
    core_certificate: bytes
    core_peer_document: bytes
    invocation_deadline_seconds: float

    def drive(
        self, execution: AgentAttemptExecution
    ) -> ExecuteAgentAttemptOnRunnerOutcome:
        manifest = _runner_manifest_for(
            execution.request, self.source_commit, self.image_digest
        )
        material = RunnerAttemptLeaseMaterial(
            manifest,
            self.runner_image,
            self.serve_container,
            self.ca_certificate,
            self.core_certificate,
            self.core_peer_document,
            _runner_auth_reference_for(execution.request.resolved_binding.auth_profile),
            runner_executor_cli_pin(manifest),
        )
        core = DbosRunnerSessionCore(
            execution,
            self.store,
            runner_lease_cancellation_command_id(execution.attempt_id),
            engine=self.engine,
        )
        return execute_agent_attempt_on_runner(
            execution,
            self.store,
            self.store,
            core,
            self.transport,
            self.leases,
            self.leases,
            material,
            self.invocation_deadline_seconds,
        )


# DBOS owns this table and these tokens; this module only reads them, and only
# to answer whether a workflow that owes a Runner-lease attempt its next move
# is still going to run (`#540` C-3.6 D-8a) -- the same narrow read
# `atelier2.adapters.dbos.uncontinuable_runs` and
# `atelier2.adapters.dbos.agent_attempt_store` each already keep their own
# copy of, rather than a shared owner neither of those files' scope invites
# widening today.
_dbos_workflow_status = sa.table(
    "workflow_status",
    sa.column("workflow_uuid"),
    sa.column("status"),
)
_DRIVING_WORKFLOW_STATUSES = ("PENDING", "ENQUEUED", "DELAYED")


def _withdraw_open_runner_leases(
    leases: FileRunnerLeasePublisher, open_directory: Path
) -> None:
    """Serve's own open leases, pulled back at every start (`#540` C-3.6 D-8a).

    A lease this exact process published and never saw claimed before its own
    restart would otherwise sit `open` for as long as no launcher happens to
    poll past it -- and a launcher that does, hours later, would start a
    Runner container for an Attempt whose driving workflow this process no
    longer owns. Withdrawing every open lease before this binding does anything
    else closes that window.

    Withdrawal is one-way in this slice: it moves the document to `withdrawn/`
    and deletes the attempt material
    (`atelier2.adapters.file_runner_leases.FileRunnerLeasePublisher.withdraw`).
    A recovered workflow that replays `publish` finds its own document
    byte-identical under `withdrawn/` and is answered `RunnerLeaseExisting`, so
    no fresh open lease ever reappears -- there is no automatic republish and
    retry in this phase. That Attempt is stranded non-terminal until Kind #585
    (`#540`) converges it over the launcher's own retained journal; today it
    still burns the full accept deadline polling the deleted paths before it
    reports a timeout, and failing fast there is #585's job too. A lease a
    launcher already claimed loses this race harmlessly -- it is reported
    `RunnerLeaseAlreadyClaimed`, not an error, and is left exactly where it is
    for the launcher that owns it.
    """

    withdrawn = 0
    for path in sorted(open_directory.glob("*.json")):
        result = leases.withdraw(RunnerLeaseId(path.stem))
        if isinstance(result, RunnerLeaseWithdrawn):
            withdrawn += 1
    if withdrawn:
        _LOG.info(
            "Withdrew %d open Runner lease(s) from a previous run.",
            withdrawn,
            extra={"event": "runner_leases_withdrawn_at_start", "count": withdrawn},
        )


def _driverless_runner_lease_attempts(
    engine: Engine, application_version: str
) -> tuple[AgentAttempt, ...]:
    """Every non-terminal, Runner-lease-bound Attempt no workflow still owes.

    Read-only: `#540` Kind #585 owns converging these to a durable terminal
    state, over the launcher's own retained journal -- the only source that
    can prove what actually happened. Until it lands, this is a name, not a
    fix: an Attempt this reports stays exactly as durable as it already was.
    """

    store = DbosAgentAttemptStore(engine, application_version)
    with engine.connect() as connection:
        terminal_states = tuple(state.value for state in TERMINAL_AGENT_ATTEMPT_STATES)
        candidate_ids = tuple(
            connection.scalars(
                sa.select(agent_attempts.c.attempt_id).where(
                    agent_attempts.c.state.not_in(terminal_states),
                    agent_attempts.c.runner_manifest_id.is_not(None),
                )
            )
        )
    if not candidate_ids:
        return ()
    candidates = tuple(store.load(AgentAttemptId(value)) for value in candidate_ids)
    with engine.connect() as connection:
        driving = set(
            connection.scalars(
                sa.select(_dbos_workflow_status.c.workflow_uuid).where(
                    _dbos_workflow_status.c.workflow_uuid.in_(
                        tuple(driving_workflow_id(attempt) for attempt in candidates)
                    ),
                    _dbos_workflow_status.c.status.in_(_DRIVING_WORKFLOW_STATUSES),
                )
            )
        )
    return tuple(
        attempt for attempt in candidates if driving_workflow_id(attempt) not in driving
    )


def _log_driverless_runner_lease_attempts(
    engine: Engine, application_version: str
) -> None:
    """Name every driverless Runner-lease Attempt in the start log (D-8a (2)).

    No durable write and no invented evidence -- an Attempt this names stays
    exactly `POSSIBLY_RAN`/armed for the operator to read; only the log line
    is new.
    """

    driverless = _driverless_runner_lease_attempts(engine, application_version)
    for attempt in driverless:
        _LOG.warning(
            "Runner-lease agent attempt %s on run %s, node %s has no living driver.",
            attempt.attempt_id.value,
            attempt.run_id.value,
            attempt.node_id,
            extra={
                "event": "runner_lease_attempt_driverless",
                "run_id": attempt.run_id.value,
                "node_id": attempt.node_id,
                "attempt_id": attempt.attempt_id.value,
            },
        )
    if driverless:
        _LOG.warning(
            "%d Runner-lease agent attempt(s) have no living driver after start.",
            len(driverless),
            extra={
                "event": "runner_lease_attempts_driverless_total",
                "count": len(driverless),
            },
        )


def _open_runner_lease_publisher(
    settings: DbosRuntimeSettings,
) -> FileRunnerLeasePublisher:
    """The one Runner-lease publisher this binding drives and withdraws through.

    Composed once and shared: the attempt driver publishes leases through it,
    and the cancellation workflow withdraws a never-launched attempt's lease
    through the same directories (`#584`). Its own start-time cleanup -- pulling
    back every lease this exact process left `open` before a restart -- runs
    here, before either caller does anything else.
    """

    lease_root = settings.runner_lease_root
    if lease_root is None:
        raise DbosRuntimeBindingConflict(
            "a runner-lease deployment requires the declared lease root"
        )
    leases = FileRunnerLeasePublisher(lease_root / "leases", lease_root / "attempts")
    _withdraw_open_runner_leases(leases, lease_root / "leases" / "open")
    return leases


def _runner_lease_attempt_driver(
    settings: DbosRuntimeSettings,
    engine: Engine,
    store: DbosAgentAttemptStore,
    leases: FileRunnerLeasePublisher,
) -> RunnerLeaseAttemptDriver:
    """Compose the real Runner-lease driver from a fully declared deployment.

    Its own module-level name, rather than an inline expression at its one
    call site, is what lets a test compose `DbosRuntime` for its real binding
    and carrier-dispatch behavior while replacing only this real TLS/socket
    transport with a scripted one -- the same layering
    `tests/integration/test_execute_agent_attempt_on_runner.py` already
    established for `execute_agent_attempt_on_runner` itself.
    """

    identity = settings.runner_core_identity_directory
    accept_timeout = settings.runner_accept_timeout_seconds
    if (
        identity is None
        or accept_timeout is None
        or settings.runner_image is None
        or settings.runner_image_digest is None
        or settings.runner_console_container is None
        or settings.runner_lease_source_commit is None
    ):
        raise DbosRuntimeBindingConflict(
            "a runner-lease driver requires the declared runner-lease deployment"
        )
    ca_certificate = (identity / "ca.crt").read_bytes()
    core_certificate = (identity / "core.crt").read_bytes()
    core_certificate_object = x509.load_pem_x509_certificate(core_certificate)
    core_peer_document = encode_core_peer_document(
        CorePeerDocument(
            CORE_DNS_NAME,
            core_uri_for_certificate(
                cast(SupportedPublicKey, core_certificate_object.public_key())
            ),
            hashlib.sha256(
                core_certificate_object.public_bytes(serialization.Encoding.DER)
            ).hexdigest(),
            CORE_SESSION_PORT,
        )
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    pin_tls_13(context)
    context.load_cert_chain(identity / "core.crt", identity / "core.key")
    context.load_verify_locations(cafile=identity / "ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    transport = RunnerLeaseSessionListener(
        context,
        ca_certificate,
        accept_timeout,
        _RUNNER_SESSION_CONNECTION_ATTEMPTS,
    )
    return DbosRunnerLeaseAttemptDriver(
        store,
        engine,
        leases,
        transport,
        settings.runner_lease_source_commit,
        settings.runner_image_digest,
        settings.runner_image,
        settings.runner_console_container,
        ca_certificate,
        core_certificate,
        core_peer_document,
        accept_timeout,
    )


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
    local_process_keys = any(
        entry.manifest_entry.carrier is AgentExecutorCarrier.LOCAL_PROCESS
        for entry in agent_registry.entries
    )
    runner_lease_keys = any(
        entry.manifest_entry.carrier is AgentExecutorCarrier.RUNNER_LEASE
        for entry in agent_registry.entries
    )
    if local_process_keys and settings.agent_scratch_root is None:
        raise DbosRuntimeBindingConflict(
            "serving a provider executor requires an agent scratch root, because "
            "every attempt is started in a workspace of its own"
        )
    if runner_lease_keys and not settings.runner_lease_declared:
        raise DbosRuntimeBindingConflict(
            "serving a runner-lease executor requires the declared "
            "runner-lease deployment"
        )
    engine = create_canonical_engine(
        settings.database_path, settings.sqlite_lock_timeout_seconds
    )
    agent_executor: AgentExecutor | None = None
    agent_executors_v2: list[tuple[AgentExecutorManifestEntry, AgentExecutorV2]] = []
    adapter: EffectAdapter | None = None
    agent_process_supervisor: AgentProcessSupervisor | None = None
    agent_workspace_owner: LocalAgentAttemptWorkspaceOwner | None = None
    runner_lease_driver: RunnerLeaseAttemptDriver | None = None
    runner_lease_publisher: FileRunnerLeasePublisher | None = None
    try:
        initialize_schema(engine)
        if settings.bootstrap_project_root is not None:
            if settings.project_id is None:
                raise ValueError(
                    "a bootstrap project root writes the host configuration "
                    "channel, so it needs a project id"
                )
            append_project_root(
                engine, settings.project_id, settings.bootstrap_project_root
            )
        declared_project_source = _declared_project_for(engine, settings.project_id)
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
            required_agent_capabilities = {
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
                        runs.c.workflow_format_version.in_(
                            (WorkflowFormatVersion.V2, WorkflowFormatVersion.V3)
                        ),
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
        required_agent_keys = {key for key, _capability in required_agent_capabilities}
        if not required_agent_keys.issubset(agent_registry.keys):
            raise DbosRuntimeBindingConflict(
                "runtime registry is missing a nonterminal durable executor binding"
            )
        if any(
            capability not in agent_registry.declared_capabilities(key)
            for key, capability in required_agent_capabilities
        ):
            raise DbosRuntimeBindingConflict(
                "runtime registry lacks a nonterminal durable capability"
            )
        agent_executor = agent_factory.open()
        for registry_entry in agent_registry.entries:
            if registry_entry.factory is not None:
                agent_executors_v2.append(
                    (registry_entry.manifest_entry, registry_entry.factory.open())
                )
        adapter = effect_factory.open()
        datasource = SQLAlchemyDatasource.create(
            sqlite_url(settings.database_path), engine=engine
        )
        attempt_store = DbosAgentAttemptStore(engine, settings.application_version)
        if local_process_keys:
            agent_process_supervisor = AgentProcessSupervisor(
                attempt_store,
                settings.process_control_root(),
                settings.process_cgroup_root(),
                grace_seconds=settings.agent_termination_grace_seconds,
            )
        if local_process_keys and settings.agent_scratch_root is not None:
            agent_workspace_owner = LocalAgentAttemptWorkspaceOwner(
                settings.agent_scratch_root
            )
            # Binding the durable database is the moment a restart can tell an
            # abandoned workspace from a live one, so it is where the workspaces
            # of attempts that ended before the restart are removed.
            agent_workspace_owner.reconcile(attempt_store)
        if runner_lease_keys:
            runner_lease_publisher = _open_runner_lease_publisher(settings)
            runner_lease_driver = _runner_lease_attempt_driver(
                settings, engine, attempt_store, runner_lease_publisher
            )
        opened_agent_executors = {
            entry.key: executor for entry, executor in agent_executors_v2
        }
        register_durable_run_workflow(
            datasource,
            agent_executor,
            agent_binding,
            {
                registry_entry.key: (
                    opened_agent_executors.get(registry_entry.key),
                    registry_entry.manifest_entry.operational_identity,
                    registry_entry.manifest_entry.declared_capabilities,
                    registry_entry.manifest_entry.carrier,
                )
                for registry_entry in agent_registry.entries
            },
            attempt_store,
            agent_process_supervisor,
            agent_workspace_owner,
            declared_project_source,
            adapter,
            effect_binding,
            runner_lease_driver,
            runner_lease_publisher,
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
        declared_project_source,
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
                    self._bound.agent_executor_registry.manifest,
                    self._bound.effect_adapter_binding,
                )
                != requested_binding
            ):
                raise DbosRuntimeBindingConflict(
                    "this process already owns "
                    f"{self._bound.settings.binding(self._bound.agent_executor_binding, self._bound.agent_executor_registry.manifest, self._bound.effect_adapter_binding)}; "
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
                if bound.agent_process_supervisor is not None:
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
            self._converge_driverless_attempts(bound)
            self._converge_uncontinuable_runs(bound)
            self._inventory_driverless_runner_lease_attempts(bound)

    @staticmethod
    def _inventory_driverless_runner_lease_attempts(bound: _BoundRuntime) -> None:
        """`#540` C-3.6 D-8a (2): name every driverless Runner-lease Attempt.

        After the launch, same as the two convergences above: only once
        recovery has armed every workflow that is still going to run can a
        workflow's absence from `workflow_status` mean it is truly gone.
        """

        if not any(
            entry.manifest_entry.carrier is AgentExecutorCarrier.RUNNER_LEASE
            for entry in bound.agent_executor_registry.entries
        ):
            return
        _log_driverless_runner_lease_attempts(
            bound.engine, bound.settings.application_version
        )

    @staticmethod
    def _converge_driverless_attempts(bound: _BoundRuntime) -> None:
        """Answer for what the last process left armed, once recovery is armed.

        After the launch, and not before: the launch is what replays the
        workflows that are still pending, so asking first would stop attempts
        that recovery was about to drive. An attempt only exists where a scratch
        root is declared -- a V2 agent node refuses before it prepares one
        otherwise -- so a runtime without a workspace owner has none to converge.
        """

        supervisor = bound.agent_process_supervisor
        workspaces = bound.agent_workspace_owner
        if supervisor is None or workspaces is None:
            return
        converge_driverless_attempts(
            DbosAgentAttemptStore(bound.engine, bound.settings.application_version),
            supervisor,
            workspaces,
        )

    @staticmethod
    def _converge_uncontinuable_runs(bound: _BoundRuntime) -> None:
        """End STARTED runs whose current node can no longer continue.

        After driverless-attempt convergence: that path stops armed attempts
        whose driver died and leaves them INTERRUPTED. This path is the
        leftover half — the attempt is already FAILED or INTERRUPTED, or the
        run advanced onto a node that never prepared and whose durable
        workflow will not recover, the run still says STARTED, and nothing
        will move it.
        """

        converge_uncontinuable_runs(
            DbosUncontinuableRunStore(bound.engine, bound.settings.application_version)
        )

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
        agent_executor_factories_v2: tuple[
            AgentExecutorFactoryV2 | AgentExecutorRegistration, ...
        ] = (),
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
        supervisor = self._held().agent_process_supervisor
        if supervisor is None:
            raise AgentProcessSupervisorUnavailable(
                "runtime has no local agent process supervisor: no LOCAL_PROCESS-"
                "carried executor key is registered"
            )
        return supervisor

    @property
    def agent_workspace_owner(self) -> LocalAgentAttemptWorkspaceOwner | None:
        return self._held().agent_workspace_owner

    @property
    def declared_project(self) -> DeclaredProject | None:
        return self._held().declared_project

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
