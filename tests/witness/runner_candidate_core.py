"""Disposable #301-A Core process: its SQLite volume is the only product truth.

The transport this process drives its one Runner connection over -- the TLS
listener, the accept/reconnect bound, frame I/O, peer-leaf pinning, and the
drive loop to RELEASED -- is not reimplemented here: it is the production
owner `atelier2.adapters.runner_core_transport`, first called by this exact
witness. Nothing in this module imports `atelier2.runner` -- the scenario this
process declares (`--scenario`) is Witness-only vocabulary, carried as a plain
bootstrap string a real launcher's bootstrap need never write at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import select
import ssl
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.run_store import run_from_record_with_bindings
from atelier2.adapters.dbos.runner_session_core import DbosRunnerSessionCore
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
    create_canonical_engine,
)
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.free_runner_executor import (
    FreeRunnerExecutorFactory,
    FreeRunnerHoldJob,
    FreeRunnerPrintJob,
    encode_free_runner_job,
    free_runner_auth_reference,
    refuse_unbound_runner_a_request,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.runner_cli_pins import runner_executor_cli_pin
from atelier2.adapters.runner_core_transport import (
    RunnerPeerPin,
    accept_and_drive_session,
    bind_session_listener,
)
from atelier2.adapters.runner_tls import (
    CORE_DNS_NAME,
    CORE_SESSION_PORT,
    CorePeerDocument,
    SupportedPublicKey,
    core_uri_for_certificate,
    encode_core_peer_document,
    invocation_from_runner_uri,
    pin_tls_13,
    runner_uri_for_invocation,
    sole_peer_uri,
    validate_peer_certificate,
)
from atelier2.application.run_runner_session import (
    CoreRunnerSession,
    encode_runner_prepare_payload,
)
from atelier2.contracts.agent_attempts import (
    TERMINAL_AGENT_ATTEMPT_STATES,
    AgentAttemptId,
    RunnerEvidenceAcceptancePhase,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.host_configuration import (
    ModelRegistryEntry,
    ModelRegistryEntrySource,
    ModelRegistryRevision,
    ProviderModelCheck,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.runner_leases import decode_runner_binding
from atelier2.contracts.runner_manifests import (
    RunnerManifestV1,
    decode_runner_manifest,
    runner_manifest_id,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.host_configuration import (
    ModelRegistryRevisionCreated,
    ModelRegistryRevisionExisting,
)

_OUTPUT_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b"true")

# The Witness's own declared scenario vocabulary (`scripts/runner_candidate.sh`
# passes exactly one of these to `--scenario`) -- production carries scenario
# as an optional plain string in `bootstrap.json`, and never imports this set.
_SCENARIO_SUCCESS = "success"
_SCENARIO_CANCEL = "cancel"
_SCENARIO_CRASH_AFTER_PUBLISH = "crash-after-publish"
_SCENARIO_CORE_RESTART = "core-restart"
_SCENARIOS = (
    _SCENARIO_SUCCESS,
    _SCENARIO_CANCEL,
    _SCENARIO_CRASH_AFTER_PUBLISH,
    _SCENARIO_CORE_RESTART,
)
_WITNESS_RECORD_FAMILY = "runner-core-reconnect-witness/v1"
_RUN_ID = RunId("runner-candidate/one")
_NODE_ID = "execute"
_CORE_STARTED_CUT_EXIT_CODE = 93
_CHILD_PHASE_AFTER_CORE_DEATH = "after-core-death"
_CHILD_PHASE_AFTER_CORE_RESTART = "after-core-restart"
_CHILD_PHASES = (
    _CHILD_PHASE_AFTER_CORE_DEATH,
    _CHILD_PHASE_AFTER_CORE_RESTART,
)
_CORE_WITNESS_BINDING_FIELDS = (
    "attempt_id",
    "request_hash",
    "generation_id",
    "manifest_id",
    "invocation_id",
    "scenario",
)
_CORE_WITNESS_RECORD_FIELDS = (
    "record_family",
    *_CORE_WITNESS_BINDING_FIELDS,
    "core_pid",
    "core_start_time_ticks",
)
_CHILD_OBSERVATION_FIELDS = (
    "runner_container_id",
    "runner_process_id",
    "provider_child_pid",
    "provider_child_start_time_ticks",
    "provider_child_state",
    "provider_child_count",
    "runner_cgroup_pids_current",
    "runner_cgroup_pids_limit",
    "runner_cgroup_limit_hit_count",
)
_CORE_STARTED_CUT_RECORD_FIELDS = (
    *_CORE_WITNESS_RECORD_FIELDS,
    *_CHILD_OBSERVATION_FIELDS,
)
_CORE_STARTED_CHILD_RECORD = "core-started-child.json"
_CORE_STARTED_CUT_EVENT = "core-started-cut.event"
_CORE_STARTED_CUT_FENCED_EVENT = "core-started-cut-fenced.event"
_CORE_STARTED_CUT_REQUEST = b"runner-core-started-cut\n"
_CORE_STARTED_CUT_FENCED = b"runner-core-started-cut-fenced\n"
_RECONNECTED_STARTED_EVENT = "core-reconnected-started.event"


@dataclass(frozen=True, slots=True)
class CandidateBootstrap:
    runtime: DbosRuntime | None
    engine: Engine
    execution: AgentAttemptExecution
    binding: RunnerGenerationBinding
    store: DbosAgentAttemptStore
    request: AgentExecutionRequestV2
    manifest: RunnerManifestV1
    auth_reference: str
    restarted: bool

    def close(self) -> None:
        if self.runtime is None:
            self.engine.dispose()
        else:
            self.runtime.close()


def _document_for(job: FreeRunnerPrintJob | FreeRunnerHoldJob) -> bytes:
    """The one-node workflow whose authored instruction *is* the job document.

    The durable store recomputes `job_bytes` from this exact node's authored
    instruction (`_validate_request`), so the fixed candidate program's job
    document cannot be handed to the request separately -- it has to be what
    the workflow's author wrote. `json.dumps` doubles as a YAML-safe quoted
    scalar here (a JSON string literal is valid YAML flow-scalar syntax),
    which keeps the encoded document's own JSON braces and quotes from being
    read as YAML flow-mapping syntax.
    """
    instruction_literal = json.dumps(encode_free_runner_job(job).decode("utf-8"))
    return (
        b"format_version: 3\n"
        b"name: Disposable free Runner candidate\n"
        b"nodes:\n"
        b"  - id: execute\n"
        b"    type: agent\n"
        b"    role: runner\n"
        b"    mode: headless\n"
        b"    instruction: " + instruction_literal.encode("utf-8") + b"\n"
        b"    outputs:\n"
        b"      - name: result\n"
        b"        schema:\n"
        b"          ref: result-schema\n"
        b"          revision: "
        + _OUTPUT_SCHEMA.revision_hash.value.encode("ascii")
        + b"\n"
    )


# Bounds every accept() so an unreachable or crashed-and-never-restarted
# Runner fails this witness loudly instead of hanging Core -- and the
# launcher's `docker wait` on it -- forever.
_ACCEPT_TIMEOUT_SECONDS = 30.0


def _maximum_runner_connections(scenario: str) -> int:
    """One connection for a normal lifetime; only the declared
    `crash-after-publish` scenario's real process death ever legitimately
    reconnects, restarting this exact Runner container (`#15-B5`). Any other
    scenario reaching a second `accept()` means its one connection dropped
    unexpectedly -- a loud failure from the reconnect bound, not a silent
    extra retry that could hang forever on a Runner that will never come back.
    """
    if scenario == _SCENARIO_CRASH_AFTER_PUBLISH:
        return 2
    return 1


def _job_for(
    manifest: RunnerManifestV1, scenario: str
) -> FreeRunnerPrintJob | FreeRunnerHoldJob:
    if scenario == _SCENARIO_CANCEL:
        return FreeRunnerHoldJob(manifest.total_attempt_milliseconds / 1000)
    if scenario == _SCENARIO_CORE_RESTART:
        return FreeRunnerHoldJob(manifest.total_attempt_milliseconds / 3000)
    return FreeRunnerPrintJob("runner candidate")


def _runner_scenario(scenario: str) -> str:
    # Core restart is a host-orchestration witness, not Runner behavior. The
    # Runner therefore executes its ordinary success lifetime while this Core
    # alone owns the process-death cut around it.
    return _SCENARIO_SUCCESS if scenario == _SCENARIO_CORE_RESTART else scenario


def _runtime(root: Path) -> DbosRuntime:
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "core.sqlite3",
            "runner-candidate-core",
            agent_scratch_root=workspace,
        ),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite3",
            AdapterRevision("runner-candidate/v1"),
            EffectDestination("runner-candidate"),
        ),
        ExactOutputAgentExecutorFactory(),
        (),
    )


def _load_candidate_run(engine: Engine) -> RunV3:
    with engine.connect() as connection:
        record = (
            connection.execute(runs.select().where(runs.c.run_id == _RUN_ID.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    if not isinstance(run, RunV3):
        raise TypeError("candidate Core did not resolve its V3 run")
    return run


def _execution_for(
    run: RunV3,
    manifest: RunnerManifestV1,
    scenario: str,
    attempt_id: AgentAttemptId | None = None,
    attempt_ordinal: int = 1,
) -> AgentAttemptExecution:
    workflow = WorkflowRevision(_document_for(_job_for(manifest, scenario)))
    if run.revision_hash != workflow.revision_hash:
        raise ValueError("runner-core-reconnect-witness-binding-mismatch")
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(_RUN_ID, workflow.revision_hash, _NODE_ID),
        _RUN_ID,
        workflow.revision_hash,
        _NODE_ID,
        run.agent_bindings[0],
        AgentExecutorOperationalIdentity(manifest.executor_operational_identity),
        encode_free_runner_job(_job_for(manifest, scenario)),
    )
    refuse_unbound_runner_a_request(request)
    return AgentAttemptExecution(
        request,
        (
            AgentAttemptId.for_execution(
                request.node_execution_id, request.request_hash, attempt_ordinal
            )
            if attempt_id is None
            else attempt_id
        ),
        attempt_ordinal,
    )


def _seed_candidate(
    runtime: DbosRuntime,
    root: Path,
    handoff: Path,
    manifest: RunnerManifestV1,
    scenario: str,
) -> CandidateBootstrap:
    DbosCatalogStore(runtime.engine).publish_revision(_OUTPUT_SCHEMA)
    runner_registry = AgentExecutorRegistry((FreeRunnerExecutorFactory(),))
    catalog = DbosAgentConfigurationCatalog(runtime.engine, runner_registry)
    auth = AuthProfileRevision(
        "candidate", 1, ProviderId("fake-free"), AuthMode.API_KEY
    )
    catalog.publish_auth_profile_revision(auth)
    configuration = AgentConfigurationRevision(
        "free",
        auth.revision_hash,
        AgentExecutorRevision("fake-free/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    catalog.publish_agent_configuration_revision(configuration)
    published_models = DbosHostConfigurationChannel(
        runtime.engine
    ).publish_model_registry_revision(
        ModelRegistryRevision(
            auth.provider_id,
            1,
            (
                ModelRegistryEntry(
                    configuration.model,
                    configuration.revision_hash,
                    ModelRegistryEntrySource.OPERATOR,
                    ProviderModelCheck.CHECKED,
                ),
            ),
        )
    )
    if not isinstance(
        published_models,
        (ModelRegistryRevisionCreated, ModelRegistryRevisionExisting),
    ):
        raise TypeError(
            f"candidate Core could not publish its model registry: {published_models!r}"
        )
    workflow = WorkflowRevision(_document_for(_job_for(manifest, scenario)))
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runner_registry,
    ).start_published(
        StartPublishedRunRequestV2(
            _RUN_ID,
            workflow.revision_hash,
            AgentBindingSet(
                (AgentBinding(AgentRole("runner"), configuration.revision_hash),)
            ),
        )
    )
    if not isinstance(started, DurableRunCreated):
        raise TypeError(f"candidate Core could not start its durable run: {started!r}")
    execution = _execution_for(_load_candidate_run(runtime.engine), manifest, scenario)
    store = DbosAgentAttemptStore(runtime.engine)
    store.prepare(execution)
    identity = runner_manifest_id(manifest)
    stated = handoff.joinpath("manifest-id").read_text(encoding="ascii").strip()
    if identity.value != stated:
        raise ValueError("runner-manifest-mismatch")
    binding = RunnerGenerationBinding(
        execution.attempt_id,
        execution.request.request_hash,
        RunnerGenerationId(secrets.token_urlsafe(32)),
        identity,
    )
    store.bind_runner_generation(execution, binding)
    _write_json(
        root / "bootstrap.json",
        {
            "record_family": _WITNESS_RECORD_FAMILY,
            "attempt_id": binding.attempt_id.value,
            "request_hash": binding.request_hash.value,
            "generation_id": binding.generation_id.value,
            "manifest_id": binding.manifest_id.value,
            "scenario": _runner_scenario(scenario),
            "witness_scenario": scenario,
        },
    )
    return CandidateBootstrap(
        runtime,
        runtime.engine,
        execution,
        binding,
        store,
        execution.request,
        manifest,
        free_runner_auth_reference(
            execution.request.resolved_binding.auth_profile
        ).value,
        False,
    )


def _decode_restart_bootstrap(path: Path, scenario: str) -> RunnerGenerationBinding:
    try:
        document = path.read_bytes()
    except FileNotFoundError as error:
        raise ValueError("runner-core-reconnect-witness-missing") from error
    except OSError as error:
        raise ValueError("runner-core-reconnect-witness-unreadable") from error
    try:
        record = json.loads(document)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("runner-core-reconnect-witness-malformed") from error
    if (
        not isinstance(record, dict)
        or set(record)
        != {
            "record_family",
            "attempt_id",
            "request_hash",
            "generation_id",
            "manifest_id",
            "scenario",
            "witness_scenario",
        }
        or record.get("record_family") != _WITNESS_RECORD_FAMILY
        or record.get("scenario") != _runner_scenario(scenario)
        or record.get("witness_scenario") != scenario
    ):
        raise ValueError("runner-core-reconnect-witness-malformed")
    try:
        return decode_runner_binding(document)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("runner-core-reconnect-witness-malformed") from error


def _resume_candidate(
    engine: Engine,
    root: Path,
    handoff: Path,
    manifest: RunnerManifestV1,
    scenario: str,
) -> CandidateBootstrap:
    binding = _decode_restart_bootstrap(root / "bootstrap.json", scenario)
    stated_manifest = (
        handoff.joinpath("manifest-id").read_text(encoding="ascii").strip()
    )
    if (
        binding.manifest_id != runner_manifest_id(manifest)
        or binding.manifest_id.value != stated_manifest
    ):
        raise ValueError("runner-core-reconnect-witness-binding-mismatch")
    store = DbosAgentAttemptStore(engine)
    durable = store.load(binding.attempt_id)
    execution = _execution_for(
        _load_candidate_run(engine),
        manifest,
        scenario,
        binding.attempt_id,
        durable.attempt_ordinal,
    )
    if (
        durable.request_hash != binding.request_hash
        or durable.runner_generation_id != binding.generation_id
        or durable.runner_manifest_id != binding.manifest_id
        or execution.request.request_hash != binding.request_hash
    ):
        raise ValueError("runner-core-reconnect-witness-binding-mismatch")
    if durable.runner_invocation_id is None:
        raise ValueError("runner-core-reconnect-witness-invocation-missing")
    return CandidateBootstrap(
        None,
        engine,
        execution,
        binding,
        store,
        execution.request,
        manifest,
        free_runner_auth_reference(
            execution.request.resolved_binding.auth_profile
        ).value,
        True,
    )


def _bootstrap(root: Path, handoff: Path, scenario: str) -> CandidateBootstrap:
    manifest = decode_runner_manifest((handoff / "manifest").read_bytes())
    database = root / "core.sqlite3"
    bootstrap = root / "bootstrap.json"
    if database.is_file() and not bootstrap.is_file():
        raise ValueError("runner-core-reconnect-witness-missing")
    if bootstrap.exists() and not database.is_file():
        raise ValueError("runner-core-reconnect-database-missing")
    restarted = database.is_file()
    if restarted:
        engine = create_canonical_engine(database)
        try:
            return _resume_candidate(engine, root, handoff, manifest, scenario)
        except BaseException:
            engine.dispose()
            raise
    runtime = _runtime(root)
    try:
        runtime.initialize_storage()
        return _seed_candidate(runtime, root, handoff, manifest, scenario)
    except BaseException:
        runtime.close()
        raise


def _write_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _write_json(path: Path, document: dict[str, str]) -> None:
    _write_bytes(
        path,
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def _process_start_time_ticks(process_id: int) -> str:
    stat = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
    closing_parenthesis = stat.rfind(")")
    if closing_parenthesis < 0:
        raise RuntimeError("runner-core-reconnect-process-identity-unreadable")
    fields_after_command = stat[closing_parenthesis + 2 :].split()
    if len(fields_after_command) <= 19:
        raise RuntimeError("runner-core-reconnect-process-identity-unreadable")
    return fields_after_command[19]


def _core_witness_record(
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    scenario: str,
) -> dict[str, str]:
    process_id = os.getpid()
    return {
        "record_family": _WITNESS_RECORD_FAMILY,
        "attempt_id": binding.attempt_id.value,
        "request_hash": binding.request_hash.value,
        "generation_id": binding.generation_id.value,
        "manifest_id": binding.manifest_id.value,
        "invocation_id": invocation.value,
        "scenario": scenario,
        "core_pid": str(process_id),
        "core_start_time_ticks": _process_start_time_ticks(process_id),
    }


def _started_child_record(root: Path) -> dict[str, str]:
    document = _read_witness_document(
        root / _CORE_STARTED_CHILD_RECORD,
        "runner-core-reconnect-started-child-missing",
        "runner-core-reconnect-started-child-malformed",
    )
    _require_exact_string_fields(
        document,
        _CHILD_OBSERVATION_FIELDS,
        "runner-core-reconnect-started-child-malformed",
    )
    return {field: cast(str, document[field]) for field in _CHILD_OBSERVATION_FIELDS}


def _opened_cut_fifo(path: Path) -> int:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as error:
        raise RuntimeError("runner-core-reconnect-cut-fence-event-missing") from error
    if not stat.S_ISFIFO(mode):
        raise RuntimeError("runner-core-reconnect-cut-fence-event-malformed")
    return os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)


def _fence_started_child(root: Path) -> dict[str, str]:
    request_descriptor = _opened_cut_fifo(root / _CORE_STARTED_CUT_EVENT)
    fenced_descriptor = _opened_cut_fifo(root / _CORE_STARTED_CUT_FENCED_EVENT)
    try:
        os.write(request_descriptor, _CORE_STARTED_CUT_REQUEST)
        readable, _, _ = select.select(
            [fenced_descriptor], [], [], _LAUNCHER_HANDOFF_TIMEOUT_SECONDS
        )
        if not readable:
            raise RuntimeError("runner-core-reconnect-cut-fence-deadline")
        if os.read(fenced_descriptor, 256) != _CORE_STARTED_CUT_FENCED:
            raise RuntimeError("runner-core-reconnect-cut-fence-marker-mismatch")
    finally:
        os.close(fenced_descriptor)
        os.close(request_descriptor)
    return _started_child_record(root)


def _write_core_started_cut(
    root: Path,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    scenario: str,
    started_child: dict[str, str],
) -> None:
    _write_json(
        root / "core-started-cut.json",
        {
            **_core_witness_record(binding, invocation, scenario),
            **started_child,
        },
    )


def _write_reconnected_core_started(
    root: Path,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    scenario: str,
) -> None:
    _write_json(
        root / "core-reconnected-started.json",
        _core_witness_record(binding, invocation, scenario),
    )
    event = root / _RECONNECTED_STARTED_EVENT
    try:
        event_mode = event.stat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISFIFO(event_mode):
        raise RuntimeError("runner-core-reconnect-started-event-malformed")
    with event.open("w", encoding="ascii") as channel:
        channel.write("runner-core-reconnected-started\n")


def _read_witness_document(
    path: Path,
    missing_refusal: str,
    malformed_refusal: str,
) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(missing_refusal) from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(malformed_refusal) from error
    if not isinstance(loaded, dict):
        raise TypeError(malformed_refusal)
    return cast(dict[str, object], loaded)


def _require_exact_string_fields(
    document: dict[str, object], fields: tuple[str, ...], refusal: str
) -> None:
    if set(document) != set(fields) or any(
        not isinstance(document[field], str) or not document[field] for field in fields
    ):
        raise RuntimeError(refusal)


def _require_same_child_identity(
    expected: dict[str, str], observed: dict[str, str]
) -> None:
    if expected["runner_container_id"] != observed["runner_container_id"]:
        raise RuntimeError("runner-core-reconnect-container-changed")
    if (
        expected["runner_process_id"] != observed["runner_process_id"]
        or expected["provider_child_pid"] != observed["provider_child_pid"]
    ):
        raise RuntimeError("runner-core-reconnect-pid-changed")
    if (
        expected["provider_child_start_time_ticks"]
        != observed["provider_child_start_time_ticks"]
    ):
        raise RuntimeError("runner-core-reconnect-start-tick-changed")
    if (
        expected["provider_child_count"] != "1"
        or observed["provider_child_count"] != "1"
    ):
        raise RuntimeError("runner-core-reconnect-child-count-changed")
    if (
        expected["runner_cgroup_pids_current"] != expected["runner_cgroup_pids_limit"]
        or observed["runner_cgroup_pids_current"]
        != observed["runner_cgroup_pids_limit"]
        or expected["runner_cgroup_pids_current"]
        != observed["runner_cgroup_pids_current"]
        or expected["runner_cgroup_limit_hit_count"]
        != observed["runner_cgroup_limit_hit_count"]
    ):
        raise RuntimeError("runner-core-reconnect-cgroup-changed")


def _validated_child_observations(
    root: Path, expected_binding: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    child = _read_witness_document(
        root / "child-survival.json",
        "runner-core-reconnect-child-witness-missing",
        "runner-core-reconnect-child-witness-malformed",
    )
    observations = child.get("observations")
    if (
        set(child) != {"record_family", *_CORE_WITNESS_BINDING_FIELDS, "observations"}
        or child.get("record_family") != _WITNESS_RECORD_FAMILY
        or any(
            child.get(field) != expected for field, expected in expected_binding.items()
        )
        or not isinstance(observations, dict)
        or set(observations) != set(_CHILD_PHASES)
    ):
        raise RuntimeError("runner-core-reconnect-child-witness-malformed")
    first = observations[_CHILD_PHASE_AFTER_CORE_DEATH]
    second = observations[_CHILD_PHASE_AFTER_CORE_RESTART]
    if (
        not isinstance(first, dict)
        or not isinstance(second, dict)
        or set(first) != set(_CHILD_OBSERVATION_FIELDS)
        or set(second) != set(_CHILD_OBSERVATION_FIELDS)
        or any(
            not isinstance(first[field], str)
            or not first[field]
            or not isinstance(second[field], str)
            or not second[field]
            for field in _CHILD_OBSERVATION_FIELDS
        )
    ):
        raise RuntimeError("runner-core-reconnect-child-witness-malformed")
    numeric_identity_fields = (
        "runner_process_id",
        "provider_child_pid",
        "provider_child_start_time_ticks",
        "provider_child_count",
        "runner_cgroup_pids_current",
        "runner_cgroup_pids_limit",
    )
    if (
        any(
            not cast(str, observation[field]).isdigit()
            or int(cast(str, observation[field])) < 1
            for observation in (first, second)
            for field in numeric_identity_fields
        )
        or any(
            not cast(str, observation["runner_cgroup_limit_hit_count"]).isdigit()
            for observation in (first, second)
        )
        or any(
            observation["runner_cgroup_pids_limit"]
            != observation["runner_cgroup_pids_current"]
            for observation in (first, second)
        )
    ):
        raise RuntimeError("runner-core-reconnect-child-witness-malformed")
    typed_first = cast(dict[str, str], first)
    typed_second = cast(dict[str, str], second)
    _require_same_child_identity(typed_first, typed_second)
    return typed_first, typed_second


def _wait_for_reconnected_child_observation(root: Path) -> None:
    observation = root / "child-survival.json"
    deadline = time.monotonic() + _LAUNCHER_HANDOFF_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            document = json.loads(observation.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError(
                "runner-core-reconnect-child-witness-malformed"
            ) from error
        except OSError as error:
            raise RuntimeError(
                "runner-core-reconnect-child-witness-malformed"
            ) from error
        else:
            if not isinstance(document, dict):
                raise TypeError("runner-core-reconnect-child-witness-malformed")
            observations = document.get("observations")
            if isinstance(observations, dict) and (
                _CHILD_PHASE_AFTER_CORE_RESTART in observations
            ):
                return
        time.sleep(0.05)
    raise RuntimeError("runner-core-reconnect-child-observation-missing")


def _completed_restart_proof(
    root: Path,
    bootstrap: CandidateBootstrap,
) -> None:
    durable = bootstrap.store.load(bootstrap.execution.attempt_id)
    if (
        durable.state not in TERMINAL_AGENT_ATTEMPT_STATES
        or durable.runner_evidence_acceptance_phase
        is not RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
        or durable.runner_terminal_evidence_hash is None
        or durable.runner_invocation_id is None
    ):
        raise RuntimeError("runner-core-reconnect-terminal-proof-missing")
    with bootstrap.engine.connect() as connection:
        attempt_count, generation_count, terminal_record_count = tuple(
            connection.exec_driver_sql(
                "SELECT COUNT(*), COUNT(DISTINCT runner_generation_id), "
                "COUNT(runner_terminal_evidence_hash) FROM agent_attempts"
            ).one()
        )
    if (attempt_count, generation_count, terminal_record_count) != (1, 1, 1):
        raise RuntimeError("runner-core-reconnect-duplicate-terminal-record")
    cut = _read_witness_document(
        root / "core-started-cut.json",
        "runner-core-reconnect-started-cut-missing",
        "runner-core-reconnect-started-cut-malformed",
    )
    reconnected = _read_witness_document(
        root / "core-reconnected-started.json",
        "runner-core-reconnect-started-marker-missing",
        "runner-core-reconnect-started-marker-malformed",
    )
    _require_exact_string_fields(
        cut,
        _CORE_STARTED_CUT_RECORD_FIELDS,
        "runner-core-reconnect-started-cut-malformed",
    )
    _require_exact_string_fields(
        reconnected,
        _CORE_WITNESS_RECORD_FIELDS,
        "runner-core-reconnect-started-marker-malformed",
    )
    expected_binding = {
        "attempt_id": durable.attempt_id.value,
        "request_hash": bootstrap.binding.request_hash.value,
        "generation_id": bootstrap.binding.generation_id.value,
        "manifest_id": bootstrap.binding.manifest_id.value,
        "invocation_id": durable.runner_invocation_id.value,
        "scenario": _SCENARIO_CORE_RESTART,
    }
    if (
        cut["record_family"] != _WITNESS_RECORD_FAMILY
        or reconnected["record_family"] != _WITNESS_RECORD_FAMILY
        or any(
            cut[field] != expected or reconnected[field] != expected
            for field, expected in expected_binding.items()
        )
    ):
        raise RuntimeError("runner-core-reconnect-witness-binding-mismatch")
    if (
        cut.get("core_pid"),
        cut.get("core_start_time_ticks"),
    ) == (
        reconnected.get("core_pid"),
        reconnected.get("core_start_time_ticks"),
    ):
        raise RuntimeError("runner-core-reconnect-core-did-not-restart")
    first, second = _validated_child_observations(root, expected_binding)
    started_child = {
        field: cast(str, cut[field]) for field in _CHILD_OBSERVATION_FIELDS
    }
    _require_same_child_identity(started_child, first)
    _require_same_child_identity(started_child, second)
    _write_json(
        root / "live-restart-proof.json",
        {
            "record_family": _WITNESS_RECORD_FAMILY,
            "attempt_id": durable.attempt_id.value,
            "generation_id": bootstrap.binding.generation_id.value,
            "invocation_id": durable.runner_invocation_id.value,
            "terminal_state": durable.state.value,
            "evidence_acceptance_phase": durable.runner_evidence_acceptance_phase.value,
            "attempt_count": str(attempt_count),
            "generation_count": str(generation_count),
            "terminal_record_count": str(terminal_record_count),
            "runner_container_id": cast(str, second["runner_container_id"]),
            "runner_process_id": cast(str, second["runner_process_id"]),
            "provider_child_pid": cast(str, second["provider_child_pid"]),
            "provider_child_start_time_ticks": cast(
                str, second["provider_child_start_time_ticks"]
            ),
            "provider_child_count": cast(str, second["provider_child_count"]),
            "runner_cgroup_pids_current": cast(
                str, second["runner_cgroup_pids_current"]
            ),
            "runner_cgroup_pids_limit": cast(str, second["runner_cgroup_pids_limit"]),
            "runner_cgroup_limit_hit_count": cast(
                str, second["runner_cgroup_limit_hit_count"]
            ),
        },
    )


# How long Core waits for the launcher to establish the rest of the Attempt --
# the Runner container, this invocation's issued identity, and the inspect
# attestation. It is the same bound as the accept above, because it answers the
# same question: a launcher that is not coming makes this witness fail loudly
# instead of hanging.
_LAUNCHER_HANDOFF_TIMEOUT_SECONDS = _ACCEPT_TIMEOUT_SECONDS


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + _LAUNCHER_HANDOFF_TIMEOUT_SECONDS
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not path.is_file():
        raise RuntimeError(f"issuer handoff did not create {path}")


def main(arguments: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=_SCENARIOS, default=_SCENARIO_SUCCESS)
    parsed = parser.parse_args(arguments)
    scenario = parsed.scenario
    root = Path("/var/lib/atelier2-candidate")
    handoff = Path("/handoff")
    identity = Path("/run/atelier2-core-identity")
    bootstrap = _bootstrap(root, handoff, scenario)
    execution = bootstrap.execution
    binding = bootstrap.binding
    store = bootstrap.store
    request = bootstrap.request
    manifest = bootstrap.manifest
    reference = bootstrap.auth_reference
    certificate_pem = identity.joinpath("core.crt").read_bytes()
    certificate = x509.load_pem_x509_certificate(certificate_pem)
    core_uri = core_uri_for_certificate(
        cast(SupportedPublicKey, certificate.public_key())
    )
    peer_document = CorePeerDocument(
        CORE_DNS_NAME,
        core_uri,
        hashlib.sha256(
            certificate.public_bytes(serialization.Encoding.DER)
        ).hexdigest(),
        CORE_SESSION_PORT,
    )
    _write_bytes(root / "core-peer.json", encode_core_peer_document(peer_document))
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    pin_tls_13(context)
    context.load_cert_chain(identity / "core.crt", identity / "core.key")
    context.load_verify_locations(cafile=identity / "ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    peer_directory = Path("/run/atelier2-peer-authorization")
    peer_leaf = peer_directory / "client.crt"
    inspect_attested = handoff / "inspect-attested"
    _wait_for(peer_leaf)
    expected_leaf_pem = peer_leaf.read_bytes()
    peer_certificate = x509.load_pem_x509_certificate(expected_leaf_pem)
    invocation = invocation_from_runner_uri(sole_peer_uri(peer_certificate), binding)
    if bootstrap.restarted:
        durable_invocation = store.load(execution.attempt_id).runner_invocation_id
        if durable_invocation != invocation:
            raise ValueError("runner-core-reconnect-witness-binding-mismatch")
    expected_runner_uri = runner_uri_for_invocation(binding, invocation)
    ca_pem = identity.joinpath("ca.crt").read_bytes()
    validate_peer_certificate(
        expected_leaf_pem,
        ca_pem,
        expected_dns_name=None,
        expected_uri=expected_runner_uri,
        expected_eku=ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    pin = RunnerPeerPin(
        ca_pem,
        expected_runner_uri,
        ExtendedKeyUsageOID.CLIENT_AUTH,
        peer_certificate.public_bytes(serialization.Encoding.DER),
    )

    def _session_for_first_connection() -> CoreRunnerSession:
        # Attested and constructed once, against the first connection --
        # neither the launcher's inspect attestation nor this invocation's
        # identity changes on a resumed reconnect, and the session this
        # returns is the one long-lived ordering fence every later connection
        # keeps driving.
        _wait_for(inspect_attested)
        if (
            inspect_attested.read_text(encoding="ascii").strip()
            != binding.manifest_id.value
        ):
            raise RuntimeError("runner-attestation-mismatch")
        return CoreRunnerSession(
            binding,
            DbosRunnerSessionCore(execution, store, secrets.token_urlsafe(32)),
            encode_runner_prepare_payload(request, reference),
            manifest,
            reference,
            invocation,
            # A composition root's job, not Core's: the serving deployment
            # binds the conformance set its executor adapters own. This
            # disposable witness reuses the Runner-side registry because it
            # composes both ends.
            runner_executor_cli_pin(manifest),
        )

    def _cut_core_after_started(_session: object) -> None:
        _write_core_started_cut(
            root,
            binding,
            invocation,
            scenario,
            _fence_started_child(root),
        )
        os._exit(_CORE_STARTED_CUT_EXIT_CODE)

    def _record_reconnected_started(_session: object) -> None:
        _write_reconnected_core_started(root, binding, invocation, scenario)
        _wait_for_reconnected_child_observation(root)

    on_started = None
    if scenario == _SCENARIO_CANCEL:
        on_started = lambda session: session.cancel()
    elif scenario == _SCENARIO_CORE_RESTART:
        on_started = (
            _record_reconnected_started
            if bootstrap.restarted
            else _cut_core_after_started
        )
    try:
        with bind_session_listener(
            CORE_SESSION_PORT, _ACCEPT_TIMEOUT_SECONDS
        ) as server:
            accept_and_drive_session(
                server,
                context,
                pin,
                _session_for_first_connection,
                _maximum_runner_connections(scenario),
                on_started,
            )
            if scenario == _SCENARIO_CORE_RESTART and bootstrap.restarted:
                _completed_restart_proof(root, bootstrap)
        return 0
    finally:
        bootstrap.close()


if __name__ == "__main__":
    raise SystemExit(main())
