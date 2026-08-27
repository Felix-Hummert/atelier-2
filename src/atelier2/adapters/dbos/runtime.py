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
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.effect_store import converge_driverless_effect_intents
from atelier2.adapters.dbos.host_configuration import (
    append_project_root,
    project_root_for,
)
from atelier2.adapters.dbos.names import QUEUE_NAME, RUNNER_LEASE_QUEUE_NAME
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.run_transitions import RunTransitionConflict
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
from atelier2.adapters.dbos.uncontinuable_runs import (
    DbosUncontinuableRunStore,
    live_driver_workflow_ids,
)
from atelier2.adapters.dbos.workflow import (
    AgentExecutorMap,
    RunnerLeaseAttemptDriver,
    reconstruct_agent_attempt,
    register_durable_run_workflow,
)
from atelier2.adapters.dbos.workflow_ids import driving_workflow_ids
from atelier2.adapters.file_runner_leases import FileRunnerLeasePublisher
from atelier2.adapters.file_runner_terminal_evidence import (
    FileRunnerTerminalEvidenceSource,
)
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
from atelier2.application.converge_driverless_runner_lease_attempts import (
    RunnerLeaseConvergenceJob,
    RunnerLeaseConvergenceReport,
    converge_driverless_runner_lease_attempts,
)
from atelier2.application.converge_uncontinuable_runs import (
    converge_uncontinuable_runs,
)
from atelier2.application.execute_agent_attempt_on_runner import (
    ExecuteAgentAttemptOnRunnerOutcome,
    RunnerAttemptLeaseMaterial,
    execute_agent_attempt_on_runner,
)
from atelier2.application.start_admitted_queue_items import (
    start_admitted_queue_items,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.agent_attempts import (
    TERMINAL_AGENT_ATTEMPT_STATES,
    AgentAttempt,
    AgentAttemptId,
    RunnerBindingConflict,
    RunnerGenerationBinding,
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
    _COMMIT as RUNNER_SOURCE_COMMIT_FORMAT,
)
from atelier2.contracts.runner_manifests import (
    _IMAGE_DIGEST as RUNNER_IMAGE_DIGEST_FORMAT,
)
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
from atelier2.ports.effects import (
    EffectAdapter,
    EffectAdapterFactory,
    EffectAdapterRegistration,
    EffectAdapterRegistry,
    OpenEffectAdapterRegistry,
)
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
    effect_adapters: tuple[EffectAdapterBinding, ...]
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
        # A Runner manifest rejects a malformed digest or source commit per
        # attempt; validating the same formats here keeps a typo in the
        # `atelier serve` flags a command-line refusal rather than a first-lease
        # traceback. The manifest owns the format contract; we reuse it.
        if self.runner_image_digest is not None and (
            RUNNER_IMAGE_DIGEST_FORMAT.fullmatch(self.runner_image_digest) is None
        ):
            raise ValueError(
                "runner_image_digest must be a sha256:<64 lowercase hex> digest"
            )
        if self.runner_lease_source_commit is not None and (
            RUNNER_SOURCE_COMMIT_FORMAT.fullmatch(self.runner_lease_source_commit)
            is None
        ):
            raise ValueError(
                "runner_lease_source_commit must be a full 40-character commit SHA"
            )

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
        effect_adapters: tuple[EffectAdapterBinding, ...],
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
            effect_adapters,
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
    effect_adapter_bindings: tuple[EffectAdapterBinding, ...]
    effect_adapters: OpenEffectAdapterRegistry
    agent_process_supervisor: AgentProcessSupervisor | None
    agent_workspace_owner: LocalAgentAttemptWorkspaceOwner | None
    declared_project: DeclaredProject | None
    leases: int = 0
    launched: bool = False
    storage_ready: bool = False


def _declared_project_for(
    engine: Engine, project_id: ProjectId | None, database_path: Path
) -> DeclaredProject | None:
    """The project this process serves, read from the host channel.

    A missing mapping is `project-unknown`: naming a project with no configured
    root is the ADR 0011 service refusal, not the channel's own row miss.

    The database path travels with it because the project's candidate store is
    placed beside the store this process binds, the same derivation the
    agent-control root uses -- so the project keeps its work inside the root it
    is served from rather than inside the checkout it reads.
    """

    if project_id is None:
        return None
    try:
        return declared_project(project_root_for(engine, project_id), database_path)
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
        driving = live_driver_workflow_ids(
            connection,
            (
                workflow_id
                for attempt in candidates
                for workflow_id in driving_workflow_ids(attempt)
            ),
            application_version,
        )
    return tuple(
        attempt
        for attempt in candidates
        if driving.isdisjoint(driving_workflow_ids(attempt))
    )


def _runner_generation_binding(attempt: AgentAttempt) -> RunnerGenerationBinding:
    """The generation an armed Runner-lease Attempt durably bound.

    Rebuilt from the attempt's own durable columns, so it names the exact
    generation the launcher's retained evidence is stamped with. Only ever
    called for an Attempt `_driverless_runner_lease_attempts` returned, which
    filters on a bound `runner_manifest_id` -- so its generation is bound too
    (`AgentAttempt` keeps the two together), and a missing one is a durable lie.
    """
    if attempt.runner_generation_id is None or attempt.runner_manifest_id is None:
        raise DbosRuntimeBindingConflict(
            "a runner-lease attempt without a bound generation cannot converge"
        )
    return RunnerGenerationBinding(
        attempt.attempt_id,
        attempt.request_hash,
        attempt.runner_generation_id,
        attempt.runner_manifest_id,
    )


def _agent_executor_map(
    registry: AgentExecutorRegistry,
    executors: tuple[tuple[AgentExecutorManifestEntry, AgentExecutorV2], ...],
) -> AgentExecutorMap:
    """Every registered executor key mapped to what a driver or converger needs.

    One owner for the map the durable workflow binding is composed with and the
    map a Serve-restart convergence reconstructs its attempts through -- the
    opened executor where there is one, and the manifest facts either way.
    """
    opened = {manifest_entry.key: executor for manifest_entry, executor in executors}
    return {
        entry.key: (
            opened.get(entry.key),
            entry.manifest_entry.operational_identity,
            entry.manifest_entry.declared_capabilities,
            entry.manifest_entry.carrier,
        )
        for entry in registry.entries
    }


def _reconstructed_runner_lease_jobs(
    bound: _BoundRuntime,
    executors: AgentExecutorMap,
    source: FileRunnerTerminalEvidenceSource,
    driverless: tuple[AgentAttempt, ...],
) -> tuple[list[RunnerLeaseConvergenceJob], list[tuple[AgentAttemptId, str]]]:
    """Reconstruct each driverless Attempt into a convergence job, tolerating one.

    One Attempt whose executor was removed from config between restarts
    (`KeyError`), or whose durable binding no longer reconstructs to the request
    it committed under (`RunTransitionConflict`/`RunnerBindingConflict`), or that
    lost the generation it bound (`DbosRuntimeBindingConflict`), must not abort
    the convergence of every other healthy Attempt and must not fail Serve start.
    Such an Attempt is named and reported non-terminal -- left exactly as durable
    as it already was, never forced -- while every reconstructable Attempt still
    converges.
    """
    jobs: list[RunnerLeaseConvergenceJob] = []
    left_nonterminal: list[tuple[AgentAttemptId, str]] = []
    for attempt in driverless:
        try:
            execution = reconstruct_agent_attempt(
                bound.datasource, executors, bound.declared_project, attempt
            ).execution
            binding = _runner_generation_binding(attempt)
        except (
            KeyError,
            RunTransitionConflict,
            RunnerBindingConflict,
            DbosRuntimeBindingConflict,
        ) as conflict:
            left_nonterminal.append((attempt.attempt_id, type(conflict).__name__))
            continue
        jobs.append(RunnerLeaseConvergenceJob(execution, binding, source))
    return jobs, left_nonterminal


def _log_runner_lease_convergence(report: RunnerLeaseConvergenceReport) -> None:
    """Say what a Serve-restart convergence moved, and what it could not.

    A converged Attempt reached its real terminal; one left non-terminal stays
    exactly as durable as it already was -- its retained fact was missing,
    corrupt, or refused -- and is named for the operator to read, never forced.
    """
    for attempt_id in report.converged:
        _LOG.info(
            "Converged Runner-lease attempt %s to its retained terminal.",
            attempt_id.value,
            extra={
                "event": "runner_lease_attempt_converged",
                "attempt_id": attempt_id.value,
            },
        )
    for attempt_id, reason in report.left_nonterminal:
        _LOG.warning(
            "Runner-lease attempt %s has no living driver and no committable "
            "retained evidence (%s).",
            attempt_id.value,
            reason,
            extra={
                "event": "runner_lease_attempt_left_nonterminal",
                "attempt_id": attempt_id.value,
                "reason": reason,
            },
        )
    if report.converged or report.left_nonterminal:
        _LOG.info(
            "Runner-lease convergence after start: %d converged, %d left non-terminal.",
            len(report.converged),
            len(report.left_nonterminal),
            extra={
                "event": "runner_lease_convergence_total",
                "converged": len(report.converged),
                "left_nonterminal": len(report.left_nonterminal),
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
    effect_registry: EffectAdapterRegistry,
    effect_bindings: tuple[EffectAdapterBinding, ...],
) -> _BoundRuntime:
    canonical_database = settings.database_path.resolve()
    # H2's sole concrete adapter binds its resolved external SQLite path here.
    # This closes file-alias corruption without widening the generic factory port.
    for effect_binding in effect_bindings:
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
    adapters: OpenEffectAdapterRegistry | None = None
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
        declared_project_source = _declared_project_for(
            engine, settings.project_id, settings.database_path
        )
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
                    AdapterOperationName(str(record.operation_name)),
                )
                for record in connection.execute(
                    sa.select(
                        effect_intents.c.adapter_revision,
                        effect_intents.c.destination_identity,
                        effect_intents.c.adapter_operational_identity,
                        effect_intents.c.operation_name,
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
        if not durable_bindings.issubset(set(effect_bindings)):
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
        adapters = effect_registry.open()
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
        register_durable_run_workflow(
            datasource,
            agent_executor,
            agent_binding,
            _agent_executor_map(agent_registry, tuple(agent_executors_v2)),
            attempt_store,
            agent_process_supervisor,
            agent_workspace_owner,
            declared_project_source,
            adapters,
            effect_bindings,
            settings.project_id,
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
        resources: list[
            OpenEffectAdapterRegistry | EffectAdapter | AgentExecutorV2 | AgentExecutor
        ] = []
        if adapters is not None:
            resources.append(adapters)
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
        effect_bindings,
        adapters,
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


def _register_queues() -> None:
    """The two queues a launched runtime polls, and what each admits at a time.

    The run queue admits as much as there are workers. The Runner-lease queue
    admits one Attempt: `RUNNER_LEASE` sessions share one fixed Core listener
    port (`atelier2.adapters.runner_tls.CORE_SESSION_PORT`), so at most one
    Runner Attempt may ever be in flight (`#540` C-3.6 D-8b, D1). That bound
    serializes rather than fails a second arrival -- no run may end
    unsuccessfully only because another Runner Attempt was already running --
    and every run waiting for the slot is a queue row rather than a blocked DBOS
    worker, so a second lease-carried run cannot starve the pool the whole
    process shares (#636).

    Both are polled rather than notified, because this deployment runs without
    LISTEN/NOTIFY, and polled often enough that a freed place is taken without an
    operator-visible pause. Registering on every launch is deliberate: the
    configuration lives in the system database, and this is the process that owns
    what it should say.
    """

    polling_interval_sec = 0.05
    DBOS.register_queue(
        QUEUE_NAME,
        polling_interval_sec=polling_interval_sec,
        on_conflict="always_update",
    )
    DBOS.register_queue(
        RUNNER_LEASE_QUEUE_NAME,
        concurrency=1,
        polling_interval_sec=polling_interval_sec,
        on_conflict="always_update",
    )


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
        effect_registry: EffectAdapterRegistry,
    ) -> _BoundRuntime:
        with self._lock:
            agent_binding = agent_factory.binding
            agent_manifest = agent_registry.manifest
            effect_bindings = effect_registry.bindings
            requested_binding = settings.binding(
                agent_binding, agent_manifest, effect_bindings
            )
            if self._bound is None:
                self._bound = _open_binding(
                    settings,
                    agent_factory,
                    agent_binding,
                    agent_registry,
                    agent_manifest,
                    effect_registry,
                    effect_bindings,
                )
            elif (
                self._bound.settings.binding(
                    self._bound.agent_executor_binding,
                    self._bound.agent_executor_registry.manifest,
                    self._bound.effect_adapter_bindings,
                )
                != requested_binding
            ):
                raise DbosRuntimeBindingConflict(
                    "this process already owns "
                    f"{self._bound.settings.binding(self._bound.agent_executor_binding, self._bound.agent_executor_registry.manifest, self._bound.effect_adapter_bindings)}; "
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
                resources: list[
                    OpenEffectAdapterRegistry
                    | EffectAdapter
                    | AgentExecutorV2
                    | AgentExecutor
                ] = [bound.effect_adapters]
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
            self._converge_driverless_effect_intents(bound)
            self._converge_uncontinuable_runs(bound)
            self._start_admitted_queue_items(bound)
            self._converge_driverless_runner_lease_attempts(bound)

    @staticmethod
    def _converge_driverless_runner_lease_attempts(bound: _BoundRuntime) -> None:
        """Converge every driverless Runner-lease Attempt to its real terminal.

        `#540` Kind #585: after a Serve restart mid-session, a Runner-lease
        Attempt whose driving workflow is gone stands armed (publicly
        `POSSIBLY_RAN`) forever. The launcher lays that Attempt's own retained
        terminal fact in its handoff directory; this reads it back over
        `FileRunnerTerminalEvidenceSource` and commits it exactly once, so the
        run reaches the terminal the Runner actually reported rather than the
        `INTERRUPTED` the driverless sweep would invent.

        After the launch, and last, same as the convergences above: only once
        recovery has armed every workflow that is still going to run can a
        workflow's absence from `workflow_status` mean it is truly gone. The
        reconstruction reads durable truth through the same owner the
        replacement workflow uses (`reconstruct_agent_attempt`), so the
        `request_hash` it commits under is exactly the one the attempt bound.

        Reconstruction is per-Attempt and tolerant: an Attempt whose executor
        left config, or whose durable binding no longer reconstructs, is left
        non-terminal and named rather than aborting the whole sweep -- one bad
        Attempt never blocks the others, and Serve always starts.
        """

        if not any(
            entry.manifest_entry.carrier is AgentExecutorCarrier.RUNNER_LEASE
            for entry in bound.agent_executor_registry.entries
        ):
            return
        lease_root = bound.settings.runner_lease_root
        if lease_root is None:
            return
        driverless = _driverless_runner_lease_attempts(
            bound.engine, bound.settings.application_version
        )
        if not driverless:
            return
        executors = _agent_executor_map(
            bound.agent_executor_registry, bound.agent_executors_v2
        )
        source = FileRunnerTerminalEvidenceSource(lease_root / "attempts")
        jobs, left_nonterminal = _reconstructed_runner_lease_jobs(
            bound, executors, source, driverless
        )
        report = converge_driverless_runner_lease_attempts(
            jobs,
            DbosAgentAttemptStore(bound.engine, bound.settings.application_version),
        )
        _log_runner_lease_convergence(
            RunnerLeaseConvergenceReport(
                report.converged, (*left_nonterminal, *report.left_nonterminal)
            )
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
    def _converge_driverless_effect_intents(bound: _BoundRuntime) -> None:
        """Route effect intents whose durable workflow raised to the operator.

        After the launch, for the same reason as the attempt sweep: only once
        recovery has re-armed every pending workflow does a terminal
        workflow_status row mean nothing will resolve the intent. Before the
        uncontinuable-run sweep: routing lifts a stranded action run to
        WAITING_RECONCILIATION, out of the STARTED rows that inventory reads,
        so an effect nobody observed reaches the operator door instead of
        being misread as a dead gap.
        """

        converge_driverless_effect_intents(
            bound.engine, bound.settings.application_version
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

    @staticmethod
    def _start_admitted_queue_items(bound: _BoundRuntime) -> None:
        """Start the bound workflow of every admitted queue item, idempotently.

        After the convergence sweeps, and once DBOS is launched: starting a run
        enqueues its driver, so this belongs with the launch that arms the
        queue, not before it. The sweep derives each run's identity from the
        item and its resolved head, so a relaunch re-derives the same id and the
        starter answers `RunExisting` -- no admitted item is started twice.
        Surfaced per-item refusals are the queue's own view a later slice wires
        to the operator; a durable lie or an unreadable queue raises here.
        """

        # Local import: `starter` imports `DbosRuntimeSettings` from this module,
        # so importing it at module scope would close a cycle.
        from atelier2.adapters.dbos.starter import DbosDurableRunStarter

        start_admitted_queue_items(
            DbosQueueProjectionStore(bound.engine),
            DbosCatalogStore(bound.engine),
            DbosDurableRunStarter(
                bound.engine,
                bound.settings,
                bound.agent_executor_registry,
            ),
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
        _register_queues()
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
        effect_adapter_factory: EffectAdapterFactory | EffectAdapterRegistry,
        agent_executor_factory: AgentExecutorFactory,
        agent_executor_factories_v2: tuple[
            AgentExecutorFactoryV2 | AgentExecutorRegistration, ...
        ] = (),
    ) -> None:
        self._close_lock = threading.Lock()
        registry = AgentExecutorRegistry(agent_executor_factories_v2)
        effect_registry = (
            effect_adapter_factory
            if isinstance(effect_adapter_factory, EffectAdapterRegistry)
            else EffectAdapterRegistry(
                (
                    EffectAdapterRegistration(
                        effect_adapter_factory.binding.operation_name,
                        effect_adapter_factory,
                    ),
                )
            )
        )
        self._bound: _BoundRuntime | None = _PROCESS_OWNER.acquire(
            settings, agent_executor_factory, registry, effect_registry
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
        binding = self.effect_adapter_binding
        return self._held().effect_adapters.adapter_for(binding.operation_name, binding)

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
        bindings = self._held().effect_adapter_bindings
        for binding in bindings:
            if binding.operation_name is AdapterOperationName.OPEN_PR:
                return binding
        return bindings[0]

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
