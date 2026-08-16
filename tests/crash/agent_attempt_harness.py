from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.cancel_agent_attempt import (
    continue_agent_attempt_cancellation,
)
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptProcessPhase,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
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
    ResolvedAgentBinding,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.agent_attempts import AgentAttemptCancellationAccepted
from atelier2.ports.agent_executions import (
    AgentProcessInvocation,
)
from atelier2.ports.durable_runs import StartPublishedRunRequestV2
from atelier2.ports.run_queries import (
    RunFound,
)
from tests.scenarios.agents import (
    SCENARIO_PROVIDER_FRAME_BYTES,
    RecordingAgentExecutorFactoryV2,
    RecordingAgentExecutorV2,
    agent_attempt_execution,
    decoding_to,
    launching,
    refusing,
)
from tests.scenarios.api import durable_queries

CRASHED = 86
DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
"""


_APPENDS_TO_COUNTER = (
    "from pathlib import Path; Path(__import__('sys').argv[1]).open('ab').write(b'x')"
)


def inert_factory() -> RecordingAgentExecutorFactoryV2:
    """The registered executor, which this harness must never be served by."""

    refuse = refusing("runtime-owned executor is not the controlled test process")
    return RecordingAgentExecutorFactoryV2(
        "anthropic",
        "claude-cli/v1",
        "controlled-process",
        b"unused",
        command=refuse,
        decoder=refuse,
    )


def controlled_process_executor(counter: Path) -> RecordingAgentExecutorV2:
    """The executor this harness drives itself, counting every launch on disk."""

    return RecordingAgentExecutorV2(
        command=launching(sys.executable, "-c", _APPENDS_TO_COUNTER, str(counter)),
        decoder=decoding_to(b"done"),
    )


def run_controlled_process(counter: Path) -> None:
    subprocess.run(
        [sys.executable, "-c", _APPENDS_TO_COUNTER, str(counter)],
        check=True,
        timeout=10,
    )


def runtime(root: Path) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(root / "atelier.sqlite", "agent-attempt-crash"),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("agent-attempt-crash"),
        ),
        ExactOutputAgentExecutorFactory(),
        (inert_factory(),),
    )


def request(lease: DbosRuntime) -> AgentExecutionRequestV2:
    lease.initialize_storage()
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    catalog = DbosAgentConfigurationCatalog(lease.engine, lease.agent_executor_registry)
    catalog.publish_auth_profile_revision(auth)
    catalog.publish_agent_configuration_revision(configuration)
    workflow = WorkflowRevision(DOCUMENT)
    DbosWorkflowRevisionPublisher(lease.engine).publish(workflow)
    binding_set = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    run_id = RunId("agent-attempt/crash")
    DbosDurableRunStarter(
        lease.engine, lease.settings, lease.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, binding_set)
    )
    resolved = ResolvedAgentBinding(AgentRole("builder"), configuration, auth)
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
        run_id,
        workflow.revision_hash,
        "build",
        resolved,
        AgentExecutorOperationalIdentity("controlled-process"),
        b"build",
    )


def main(root: Path, mode: str) -> None:
    lease = runtime(root)
    try:
        exact_request = request(lease)
        store = DbosAgentAttemptStore(lease.engine)
        if mode == "crash-prepared":
            store.prepare(agent_attempt_execution(exact_request))
            os._exit(CRASHED)
        if mode == "crash-armed":
            store.prepare(agent_attempt_execution(exact_request))
            store.claim(agent_attempt_execution(exact_request))
            run_controlled_process(root / "counter")
            os._exit(CRASHED)
        if mode == "crash-running":
            ready = root / "running-pid"
            launch_attempt(lease, store, exact_request, ready)
            os._exit(CRASHED)
        if mode == "close-running":
            ready = root / "closed-running-pid"
            execution = launch_attempt(lease, store, exact_request, ready)
            attempt = store.load(execution.attempt_id)
            (root / "closed-cgroup").write_text(
                str(attempt_cgroup(lease, attempt)), encoding="utf-8"
            )
            (root / "closed-endpoint").write_text(
                str(attempt_endpoint(lease, attempt)), encoding="utf-8"
            )
            lease.close()
            return
        if mode == "crash-watchdog-and-running-descendant":
            descendant_pid = root / "descendant-pid"
            provider = (
                "from pathlib import Path; import subprocess,sys,time; "
                "subprocess.Popen((sys.executable,'-c',"
                "'from pathlib import Path; import os,signal,sys,time; '"
                "+'signal.signal(signal.SIGTERM,signal.SIG_IGN); '"
                "+'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)',"
                "sys.argv[1]), start_new_session=True, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL); time.sleep(60)"
            )
            launch_attempt(
                lease,
                store,
                exact_request,
                descendant_pid,
                (sys.executable, "-c", provider, str(descendant_pid)),
            )
            watchdog_pid = only_watchdog_child_pid()
            os.kill(watchdog_pid, 9)
            os._exit(CRASHED)
        if mode == "crash-after-cleanup-before-attestation":
            ready = root / "cleanup-before-attestation-pid"
            execution = launch_attempt(lease, store, exact_request, ready)
            attempt = store.load(execution.attempt_id)
            command = CancelAgentAttemptRequest(
                attempt.run_id,
                attempt.attempt_id,
                "cleanup-before-attestation",
                attempt.state_version,
                AgentAttemptReplacement.NONE,
            )
            cancellation = DbosAgentAttemptStore(
                lease.engine, lease.settings.application_version
            ).request_cancellation(command)
            if not isinstance(cancellation, AgentAttemptCancellationAccepted):
                raise AssertionError("controlled cancellation was not accepted")
            disposition, _owner, _generation = lease.agent_process_supervisor.cancel(
                store.load(attempt.attempt_id)
            )
            cgroup = attempt_cgroup(lease, store.load(attempt.attempt_id))
            endpoint = attempt_endpoint(lease, store.load(attempt.attempt_id))
            (root / "cleanup-disposition").write_text(
                disposition.value, encoding="ascii"
            )
            (root / "cleanup-cgroup").write_text(str(cgroup), encoding="utf-8")
            (root / "cleanup-endpoint").write_text(str(endpoint), encoding="utf-8")
            os._exit(CRASHED)
        if mode == "crash-after-attestation-before-release":
            ready = root / "attestation-before-release-pid"
            execution = launch_attempt(lease, store, exact_request, ready)
            attempt = store.load(execution.attempt_id)
            command = CancelAgentAttemptRequest(
                attempt.run_id,
                attempt.attempt_id,
                "attestation-before-release",
                attempt.state_version,
                AgentAttemptReplacement.NONE,
            )
            submitting_store = DbosAgentAttemptStore(
                lease.engine, lease.settings.application_version
            )
            cancellation = submitting_store.request_cancellation(command)
            if not isinstance(cancellation, AgentAttemptCancellationAccepted):
                raise AssertionError("controlled cancellation was not accepted")
            attempt = store.load(attempt.attempt_id)
            disposition, owner, generation = lease.agent_process_supervisor.cancel(
                attempt
            )
            terminal = store.attest_cancellation_cleanup(
                command, disposition, owner, generation
            )
            cgroup = attempt_cgroup(lease, terminal.attempt)
            endpoint = attempt_endpoint(lease, terminal.attempt)
            (root / "attested-cgroup").write_text(str(cgroup), encoding="utf-8")
            (root / "attested-endpoint").write_text(str(endpoint), encoding="utf-8")
            (root / "attested-state-version").write_text(
                str(terminal.attempt.state_version), encoding="ascii"
            )
            os._exit(CRASHED)
        if mode == "recover-after-missing-witness-restored":
            execution = agent_attempt_execution(exact_request)
            attempt = store.load(execution.attempt_id)
            cgroup = attempt_cgroup(lease, attempt)
            wait_for_empty_cgroup(cgroup)
            cgroup.rmdir()
            command = exact_cancellation_command(attempt)
            DbosAgentAttemptStore(
                lease.engine, lease.settings.application_version
            ).request_cancellation(command)
            assert (
                continue_agent_attempt_cancellation(
                    command, store, lease.agent_process_supervisor
                )
                is None
            )
            cgroup.mkdir(mode=0o700)
            result = continue_agent_attempt_cancellation(
                command, store, lease.agent_process_supervisor
            )
            if result is None:
                raise AssertionError("second recovery remained poisoned")
            (root / "second-recovery-state").write_text(
                result.attempt.state.value, encoding="ascii"
            )
            return
        if mode == "recover-cancellation":
            execution = agent_attempt_execution(exact_request)
            attempt = store.load(execution.attempt_id)
            witness = (
                None
                if attempt.watchdog_generation_id is None
                else attempt_cgroup(lease, attempt)
            )
            command = exact_cancellation_command(attempt)
            store_with_submission = DbosAgentAttemptStore(
                lease.engine, lease.settings.application_version
            )
            store_with_submission.request_cancellation(command)
            lease.launch()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                terminal = store.load(execution.attempt_id)
                if terminal.state in {
                    AgentAttemptState.CANCELLED,
                    AgentAttemptState.INTERRUPTED,
                } and (witness is None or not witness.exists()):
                    (root / "recovered-cancellation-state").write_text(
                        terminal.state.value, encoding="ascii"
                    )
                    return
                time.sleep(0.01)
            raise AssertionError("parent-death cancellation was not recovered")
        if mode == "recover":
            execute_agent_attempt(
                agent_attempt_execution(exact_request),
                controlled_process_executor(root / "counter"),
                store,
                lease.agent_process_supervisor,
            )
            found = durable_queries(lease.engine).get_run(exact_request.run_id)
            if isinstance(found, RunFound):
                attempt = found.projection.current_agent_attempt
                if attempt is not None:
                    (root / "projected-attempt-state").write_text(
                        attempt.state, encoding="utf-8"
                    )
            return
        raise ValueError(f"unknown mode {mode!r}")
    finally:
        lease.close()


def launch_attempt(
    lease: DbosRuntime,
    store: DbosAgentAttemptStore,
    exact_request: AgentExecutionRequestV2,
    ready: Path,
    arguments: tuple[str, ...] | None = None,
) -> AgentAttemptExecution:
    execution = agent_attempt_execution(exact_request)
    store.prepare(execution)
    lease.agent_process_supervisor.prepare(execution)
    store.claim(execution)
    invocation = AgentProcessInvocation(
        arguments
        or (
            sys.executable,
            "-c",
            "from pathlib import Path; import os,sys,time; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)",
            str(ready),
        ),
        Path.cwd(),
        standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
    )
    threading.Thread(
        target=lease.agent_process_supervisor.launch_and_wait,
        args=(execution, invocation),
        daemon=True,
    ).start()
    wait_for_file(ready)
    wait_for_process_observation(store, execution)
    return execution


def wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError("controlled process did not become ready")


def wait_for_process_observation(
    store: DbosAgentAttemptStore, execution: AgentAttemptExecution
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        attempt = store.load(execution.attempt_id)
        if attempt.process_phase is AgentAttemptProcessPhase.PROCESS_OBSERVED:
            return
        time.sleep(0.01)
    raise AssertionError("controlled process was not durably observed")


def only_watchdog_child_pid() -> int:
    children = (
        Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
        .read_text(encoding="ascii")
        .split()
    )
    matching = []
    for value in children:
        command = Path(f"/proc/{value}/cmdline").read_bytes()
        if b"atelier2.adapters.agent_process_watchdog" in command:
            matching.append(int(value))
    if len(matching) != 1:
        raise AssertionError("expected exactly one live watchdog child")
    return matching[0]


def exact_cancellation_command(attempt: AgentAttempt) -> CancelAgentAttemptRequest:
    cancellation = attempt.cancellation
    return CancelAgentAttemptRequest(
        attempt.run_id,
        attempt.attempt_id,
        (
            "cancel-after-parent-death"
            if cancellation is None
            else cancellation.command_id
        ),
        (
            attempt.state_version
            if cancellation is None
            else cancellation.expected_attempt_state_version
        ),
        (
            AgentAttemptReplacement.NONE
            if cancellation is None
            else cancellation.replacement
        ),
    )


def attempt_cgroup(lease: DbosRuntime, attempt: AgentAttempt) -> Path:
    generation = attempt.watchdog_generation_id
    if generation is None:
        raise AssertionError("attempt has no watchdog generation")
    return lease.settings.process_cgroup_root() / (
        "atelier2-" + attempt.attempt_id.value[:16] + "-" + generation.value[:8]
    )


def attempt_endpoint(lease: DbosRuntime, attempt: AgentAttempt) -> Path:
    return lease.settings.process_control_root() / f"{attempt.attempt_id.value}.sock"


def wait_for_empty_cgroup(cgroup: Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (
            cgroup.is_dir()
            and "populated 0"
            in (cgroup / "cgroup.events").read_text(encoding="ascii").splitlines()
        ):
            return
        time.sleep(0.01)
    raise AssertionError("attempt cgroup did not become empty")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
