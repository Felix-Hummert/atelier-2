from __future__ import annotations

import sqlite3
import sys
from collections.abc import Sized
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.exc import DatabaseError, IntegrityError

import atelier2.adapters.dbos.agent_attempt_store as agent_attempt_store_module
import atelier2.adapters.dbos.run_transitions as run_transitions_module
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.run_transitions import RunTransitionConflict
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
    artifacts,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow_ids import (
    cancellation_workflow_id_for,
    driving_workflow_ids,
    runner_lease_workflow_id_for,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    AgentAttempt,
    AgentAttemptFailureCode,
    AgentAttemptReplacement,
    CancelAgentAttemptRequest,
    RunnerGenerationBinding,
    RunnerGenerationId,
    stop_command_for,
)
from atelier2.contracts.agent_permissions import (
    GRANTS_NOTHING,
    PermissionEffect,
    PermissionPolicyRevision,
    PermissionScope,
    PermissionScopeKind,
)
from atelier2.contracts.agent_transcripts import (
    AssistantTurn,
    AttemptTranscript,
    ToolCalled,
    TranscriptEvent,
    TranscriptMomentOrigin,
    TranscriptRecordedMoment,
    UnrecognisedProviderOutput,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS, PageLimit
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.runner_manifests import runner_manifest_id
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflows import (
    RunCompletes,
    RunContinues,
)
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    NodeOutput,
    VersionedReference,
    WorkflowGraphV3,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptClaimedByThisCall,
    AgentAttemptFailed,
    AgentAttemptPossiblyRan,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionExisting,
    AuthProfileRevisionCreated,
    AuthProfileRevisionExisting,
)
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentProcessCompletion,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.run_queries import RunFound
from atelier2.ports.workflow_revisions import QueryDurableStateCorrupt
from tests.scenarios.agents import (
    AgentCompletionDecoder,
    RecordingAgentExecutorFactoryV2,
    RecordingAgentExecutorV2,
    agent_attempt_execution,
    agent_execution_request_v2,
    agent_scratch_root,
    answering,
    decode_process_exit,
    emitting,
    launching,
    prepared_agent_attempt,
    process_exit,
    publish_checked_model_registry,
    runtime_workspace_owner,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.runners import free_runner_candidate_manifest
from tests.scenarios.runs import publish_pinned_revisions
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

_DOCUMENT = (
    b"""format_version: 3
name: Build then close it out
nodes:
  - id: build
    type: agent
    role: builder
    mode: headless
    instruction: build
"""
    + declared_output()
    + b"""  - id: done
    type: agent
    role: builder
    mode: headless
    instruction: close it out
    depends_on: [build]
"""
    + declared_output(name="closed")
)


def attempt_runtime(
    root: Path, *, agent_process_cgroup_root: Path | None = None
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "attempt-test",
            agent_process_cgroup_root=agent_process_cgroup_root,
            agent_scratch_root=agent_scratch_root(root),
        ),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("test"),
        ),
        (
            RecordingAgentExecutorFactoryV2(
                "anthropic", "claude-cli/v1", "controlled-process", b'"unused"'
            ),
        ),
    )


def attempt_request(
    runtime: DbosRuntime, run_name: str = "attempt/run"
) -> AgentExecutionRequestV2:
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth),
        (AuthProfileRevisionCreated, AuthProfileRevisionExisting),
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        (AgentConfigurationRevisionCreated, AgentConfigurationRevisionExisting),
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("anthropic"), (configuration,)
    )
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA)
    workflow = WorkflowRevision(_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    run_id = RunId(run_name)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(
            run_id,
            workflow.revision_hash,
            AgentBindingSet(
                (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
            ),
        )
    )
    assert isinstance(started, DurableRunCreated)
    assert isinstance(started.run, RunV3)
    resolved = started.run.agent_bindings[0]
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
        run_id,
        workflow.revision_hash,
        "build",
        resolved,
        AgentExecutorOperationalIdentity("controlled-process"),
        b"build",
    )


def _the_driving_workflow(attempt: AgentAttempt) -> str:
    """The one workflow a local-process attempt ever holds a status under.

    `driving_workflow_ids` also names the Runner slot, because a lease-carried
    attempt's row cannot say whether the slot has taken it over. These attempts
    are local-process, so no workflow is ever minted under that second id.
    """

    return driving_workflow_ids(attempt)[0]


def _driverless_store(runtime: DbosRuntime) -> DbosAgentAttemptStore:
    """A store that may be asked which attempts are driverless.

    `iter_driverless_attempts` refuses without an application version, because
    "still driven" means "by a workflow this version of the runtime will resume".
    """

    return DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)


def _record_driving_workflow(
    runtime: DbosRuntime,
    workflow_id: str,
    status: str,
    *,
    application_version: str | None = None,
) -> None:
    """Leave one DBOS workflow row in the state a real one would be found in.

    The durable runtime owns this table; a test that wants to ask about a
    workflow in a status only a crash or a raise produces cannot reach that
    status by running one.

    The version is half of what makes a row live: DBOS resumes only workflows of
    the version it is running, so a row a retired one left behind is dead however
    driving its status reads. It defaults to this runtime's own; a test of that
    retired case names another.
    """

    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO workflow_status "
                "(workflow_uuid, status, application_version, "
                "created_at, updated_at, priority) "
                "VALUES (:workflow_id, :status, :application_version, 0, 0, 0)"
            ),
            {
                "workflow_id": workflow_id,
                "status": status,
                "application_version": (
                    runtime.settings.application_version
                    if application_version is None
                    else application_version
                ),
            },
        )


def _ordered_prepared_attempts(
    runtime: DbosRuntime,
    store: DbosAgentAttemptStore,
    path_prefix: str,
    count: int,
) -> tuple[AgentAttempt, ...]:
    return tuple(
        sorted(
            (
                store.prepare(
                    agent_attempt_execution(
                        attempt_request(runtime, f"{path_prefix}/{index}")
                    )
                )
                for index in range(count)
            ),
            key=lambda attempt: attempt.attempt_id.value,
        )
    )


def _observing_durable_process_phase(runtime: DbosRuntime) -> AgentCompletionDecoder:
    """A decoder reading back what the durable attempt says while it decodes."""

    def decode(
        completion: AgentProcessCompletion,
    ) -> AgentExecutionResult | AgentExecutionFailure:
        with runtime.engine.connect() as connection:
            state = connection.scalar(sa.select(agent_attempts.c.process_phase))
        assert state == "PROCESS_OBSERVED"
        return decode_process_exit(completion)

    return decode


def inspecting_executor(
    runtime: DbosRuntime, output: bytes = b'"done"', delay_seconds: float = 0
) -> RecordingAgentExecutorV2:
    return RecordingAgentExecutorV2(
        command=emitting(output, delay_seconds=delay_seconds),
        decoder=_observing_durable_process_phase(runtime),
    )


_PROVIDER_APPENDS_ONE_BYTE = (
    "from pathlib import Path; Path(__import__('sys').argv[1]).open('ab').write(b'x')"
)


def counting_executor(counter: Path) -> RecordingAgentExecutorV2:
    """An executor whose process leaves one byte per real invocation behind."""

    return RecordingAgentExecutorV2(
        command=launching(
            sys.executable, "-c", _PROVIDER_APPENDS_ONE_BYTE, str(counter)
        )
    )


def test_attempt_is_prepared_before_controlled_executor_invocation(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime)
        executor = inspecting_executor(runtime)

        outcome = execute_agent_attempt(
            agent_attempt_execution(request),
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )

        assert isinstance(outcome, AgentAttemptSucceeded)
        assert outcome.completion == RunContinues("done")
        assert len(executor.results) == 1
    finally:
        runtime.close()


def test_thirty_two_claims_invoke_one_controlled_executor(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/concurrent")
        executor = inspecting_executor(runtime, b'"once"', 0.25)
        store = DbosAgentAttemptStore(runtime.engine)
        execution = agent_attempt_execution(request)
        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [
                pool.submit(
                    execute_agent_attempt,
                    execution,
                    executor,
                    store,
                    runtime.agent_process_supervisor,
                    runtime_workspace_owner(runtime),
                )
                for _ in range(32)
            ]
            outcomes = tuple(future.result(timeout=5) for future in futures)

        assert len(executor.results) == 1
        assert sum(isinstance(value, AgentAttemptSucceeded) for value in outcomes) >= 1
        assert all(
            isinstance(value, (AgentAttemptSucceeded, AgentAttemptPossiblyRan))
            for value in outcomes
        )
    finally:
        runtime.close()


def test_reentering_after_terminal_attempt_never_authorizes_invocation(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/replayed-claim")
        store = DbosAgentAttemptStore(runtime.engine)
        executor = inspecting_executor(runtime)

        first = execute_agent_attempt(
            agent_attempt_execution(request),
            executor,
            store,
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )
        recovered = execute_agent_attempt(
            agent_attempt_execution(request),
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )

        assert isinstance(first, AgentAttemptSucceeded)
        assert first.completion == RunContinues("done")
        assert recovered == first
        assert len(executor.results) == 1
        assert len(executor.released_commands) == 2
    finally:
        runtime.close()


def _stage_agent_sink_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage the one graph shape that drives a terminal agent completion.

    A single Agent node with nothing depending on it is its own sink, staged
    here rather than published and started through the real bootstrap so the
    test exercises the real store transaction at the exact graph seam H1a
    opens without also depending on that bootstrap.
    """

    terminal_graph = WorkflowGraphV3(
        format_version=3,
        name="One agent, its own sink",
        nodes=(
            AgentNodeV3(
                id="build",
                type="agent",
                role="builder",
                mode="headless",
                instruction="build",
                outputs=(
                    NodeOutput(
                        name="result",
                        schema=VersionedReference(
                            ref="result-schema",
                            revision=ANY_JSON_SCHEMA.revision_hash.value,
                        ),
                    ),
                ),
            ),
        ),
    )
    for module in (agent_attempt_store_module, run_transitions_module):
        monkeypatch.setattr(
            module, "load_graph", lambda _session, _revision_hash: terminal_graph
        )


def test_terminal_agent_success_is_one_durable_write_and_exact_reentry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/terminal-agent")
        execution = agent_attempt_execution(request)
        _stage_agent_sink_graph(monkeypatch)
        executor = inspecting_executor(runtime)

        first = execute_agent_attempt(
            execution,
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )
        recovered = execute_agent_attempt(
            execution,
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )

        with runtime.engine.connect() as connection:
            attempt = connection.execute(sa.select(agent_attempts)).mappings().one()
            event = connection.execute(sa.select(run_events)).mappings().one()
            run = connection.execute(sa.select(runs)).mappings().one()
            receipt_count = connection.scalar(
                sa.select(sa.func.count()).select_from(agent_receipts_v2)
            )

        assert isinstance(first, AgentAttemptSucceeded)
        assert first.completion == RunCompletes()
        assert first.attempt.receipt_hash is not None
        assert recovered == first
        assert len(executor.results) == 1
        assert (
            attempt["state"],
            attempt["state_version"],
            attempt["process_phase"],
            attempt["receipt_hash"],
            event["event_sequence"],
            event["event_kind"],
            event["node_id"],
            event["agent_attempt_id"],
            event["attempt_ordinal"],
            event["payload"],
            run["state"],
            run["current_node_id"],
            run["state_version"],
            run["last_event_sequence"],
            receipt_count,
        ) == (
            "SUCCEEDED",
            4,
            "PROCESS_OBSERVED",
            first.attempt.receipt_hash.value,
            1,
            "AGENT_COMPLETED",
            "build",
            execution.attempt_id.value,
            1,
            b'"done"',
            "COMPLETED",
            "build",
            1,
            1,
            1,
        )
    finally:
        runtime.close()


def test_a_claim_replayed_from_a_lost_incarnation_never_authorizes_invocation(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/lost-incarnation")
        execution = agent_attempt_execution(request)
        lost_incarnation = DbosAgentAttemptStore(runtime.engine)
        lost_incarnation.prepare(execution)
        assert isinstance(
            lost_incarnation.claim(execution), AgentAttemptClaimedByThisCall
        )

        counter = tmp_path / "invocations"
        executor = counting_executor(counter)
        outcome = execute_agent_attempt(
            execution,
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )

        assert isinstance(outcome, AgentAttemptPossiblyRan)
        assert not counter.exists()
        assert len(executor.released_commands) == 1
    finally:
        runtime.close()


def test_current_attempt_projection_maps_armed_and_rejects_broken_id(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/projection")
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(agent_attempt_execution(request))
        store.claim(agent_attempt_execution(request))

        found = durable_queries(runtime.engine).get_run(request.run_id)

        assert isinstance(found, RunFound)
        attempt = found.projection.current_agent_attempt
        assert attempt is not None
        assert attempt.state == "POSSIBLY_RAN"
        assert attempt.failure_code is None
        assert attempt.request_hash == request.request_hash

        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER agent_attempts_state_transition")
            connection.execute(agent_attempts.update().values(attempt_id="f" * 64))
        assert isinstance(
            durable_queries(runtime.engine).get_run(request.run_id),
            QueryDurableStateCorrupt,
        )
    finally:
        runtime.close()


_DECODED_STEPS = (
    ToolCalled("Read", '{"file_path":"AGENTS.md"}'),
    AssistantTurn("The file names the policy."),
)
_UNREADABLE_OUTPUT = (UnrecognisedProviderOutput("fatal: the model never answered"),)
TRANSCRIPT_RECORDED_AT = RecordedAt("2026-08-29T12:00:00Z")


@pytest.mark.parametrize(
    ("verdict", "steps"),
    [
        pytest.param(
            AgentExecutionResult(b'"done"', AttemptTranscript.of(_DECODED_STEPS)),
            _DECODED_STEPS,
            id="a success keeps what reached the answer",
        ),
        pytest.param(
            AgentExecutionFailure(
                AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
                AttemptTranscript.of(_UNREADABLE_OUTPUT),
            ),
            _UNREADABLE_OUTPUT,
            id="a failure keeps what was printed instead of an answer",
        ),
        pytest.param(
            AgentExecutionResult(b'"done"'),
            None,
            id="an executor that decoded nothing keeps nothing",
        ),
    ],
)
def test_a_terminal_attempt_names_the_transcript_its_executor_decoded(
    tmp_path: Path,
    verdict: AgentExecutionResult | AgentExecutionFailure,
    steps: tuple[TranscriptEvent, ...] | None,
) -> None:
    """The steps reach the same write that ends the attempt, or nothing does.

    Both endings are asked, because the failing one is the reason this exists:
    an exit code beside an empty standard error was the whole account of a real
    run (#733). The pointer is resolved rather than compared to a hash spelled
    here -- what the attempt must name is the bytes the store really holds.
    """

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/transcript")
        execute_agent_attempt(
            agent_attempt_execution(request),
            RecordingAgentExecutorV2(
                command=launching(sys.executable, "-c", "pass"),
                decoder=answering(verdict),
            ),
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
            clock=lambda: TRANSCRIPT_RECORDED_AT,
        )

        with runtime.engine.connect() as connection:
            kept = connection.scalar(
                sa.select(agent_attempts.c.transcript_artifact_hash)
            )
            stored = connection.scalar(
                sa.select(artifacts.c.content).where(artifacts.c.artifact_hash == kept)
            )
        if steps is None:
            assert kept is None
        else:
            expected = AttemptTranscript.of(steps).with_recorded_moment(
                TRANSCRIPT_RECORDED_AT
            )
            assert stored == expected.document
            assert all(
                isinstance(event.moment, TranscriptRecordedMoment)
                and event.moment.origin is TranscriptMomentOrigin.RECORDED
                for event in expected.events
            )
    finally:
        runtime.close()


def test_a_verification_that_never_answered_still_keeps_what_the_agent_did(
    tmp_path: Path,
) -> None:
    """The agent's work is not undone by the check's silence.

    This was the one ending that dropped the steps, which would have made an
    absent transcript mean two different things -- "no executor decoded one" and
    "a verification timed out after one was" -- with no way to tell them apart.
    """

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/verification-silent")
        store = DbosAgentAttemptStore(runtime.engine)
        execution = agent_attempt_execution(request)
        store.prepare(execution)
        store.claim(execution)
        transcript = AttemptTranscript.of(_DECODED_STEPS)

        outcome = store.complete_project_verification_failure(
            execution, "timeout 30 seconds", transcript
        )

        assert isinstance(outcome, AgentAttemptFailed)
        assert (
            outcome.attempt.failure_code
            is AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED
        )
        with runtime.engine.connect() as connection:
            kept = connection.scalar(
                sa.select(agent_attempts.c.transcript_artifact_hash)
            )
            assert (
                connection.scalar(
                    sa.select(artifacts.c.content).where(
                        artifacts.c.artifact_hash == kept
                    )
                )
                == transcript.document
            )
    finally:
        runtime.close()


def test_a_refused_verification_failure_leaves_no_transcript_behind_either(
    tmp_path: Path,
) -> None:
    """The artifact and the pointer naming it stand or fall together.

    The transcript is kept inside the same write that ends the attempt, so an
    abort further down has to take the bytes with it -- otherwise a store would
    accumulate material no attempt ever names, published by writes that in the
    end never happened.
    """

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/verification-refused")
        store = DbosAgentAttemptStore(runtime.engine)
        execution = agent_attempt_execution(request)
        store.prepare(execution)
        store.claim(execution)
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TRIGGER fail_attempt BEFORE UPDATE ON agent_attempts "
                "WHEN NEW.state='FAILED' "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            )

        with pytest.raises(DatabaseError, match="failpoint"):
            store.complete_project_verification_failure(
                execution, "timeout 30 seconds", AttemptTranscript.of(_DECODED_STEPS)
            )

        with runtime.engine.connect() as connection:
            attempt = connection.execute(sa.select(agent_attempts)).mappings().one()
            artifact_count = connection.scalar(
                sa.select(sa.func.count()).select_from(artifacts)
            )
        assert (attempt["state"], attempt["transcript_artifact_hash"]) == (
            "LAUNCH_ARMED",
            None,
        )
        assert artifact_count == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("known_failure", (False, True))
def test_terminal_attempt_commit_is_atomic_and_matches_success_or_known_failure(
    tmp_path: Path, known_failure: bool
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, f"attempt/terminal/{known_failure}")
        store = DbosAgentAttemptStore(runtime.engine)

        terminal = RecordingAgentExecutorV2(
            command=launching(sys.executable, "-c", "raise SystemExit(7)"),
            decoder=answering(
                AgentExecutionFailure(
                    AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
                )
                if known_failure
                else AgentExecutionResult(b'"done"')
            ),
        )

        outcome = execute_agent_attempt(
            agent_attempt_execution(request),
            terminal,
            store,
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )
        assert len(terminal.released_commands) == 1
        with runtime.engine.connect() as connection:
            attempt = connection.execute(sa.select(agent_attempts)).mappings().one()
            event = connection.execute(sa.select(run_events)).mappings().one()
            run = connection.execute(sa.select(runs)).mappings().one()
            receipt_count = connection.scalar(
                sa.select(sa.func.count()).select_from(agent_receipts_v2)
            )

        if known_failure:
            assert isinstance(outcome, AgentAttemptFailed)
            assert (attempt["state"], event["event_kind"], receipt_count) == (
                "FAILED",
                "AGENT_FAILED",
                0,
            )
            assert (run["current_node_id"], run["state_version"]) == ("build", 1)
        else:
            assert isinstance(outcome, AgentAttemptSucceeded)
            assert (attempt["state"], event["event_kind"], receipt_count) == (
                "SUCCEEDED",
                "AGENT_COMPLETED",
                1,
            )
            assert (run["current_node_id"], run["state_version"]) == ("done", 1)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("terminal", "failpoint", "trigger"),
    (
        (
            "success",
            "receipt",
            (
                "CREATE TRIGGER fail_receipt BEFORE INSERT ON agent_receipts_v2 "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "success",
            "attempt",
            (
                "CREATE TRIGGER fail_attempt BEFORE UPDATE ON agent_attempts "
                "WHEN NEW.state='SUCCEEDED' "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "success",
            "run",
            (
                "CREATE TRIGGER fail_run BEFORE UPDATE ON runs "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "success",
            "event",
            (
                "CREATE TRIGGER fail_event BEFORE INSERT ON run_events "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "failure",
            "attempt",
            (
                "CREATE TRIGGER fail_attempt BEFORE UPDATE ON agent_attempts "
                "WHEN NEW.state='FAILED' "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "failure",
            "run",
            (
                "CREATE TRIGGER fail_run BEFORE UPDATE ON runs "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "failure",
            "event",
            (
                "CREATE TRIGGER fail_event BEFORE INSERT ON run_events "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
    ),
)
def test_each_terminal_write_failpoint_rolls_back_the_whole_attempt(
    tmp_path: Path, terminal: str, failpoint: str, trigger: str
) -> None:
    """Whichever write aborts, nothing of the terminal is left behind.

    The event log answers in the run transition's own words -- the driver's
    error cannot cross the durable step boundary -- so the failpoint on it is
    refused under that name; every other boundary still answers with the
    driver's.
    """

    runtime = attempt_runtime(tmp_path / terminal / failpoint)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, f"attempt/failpoint/{failpoint}")
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(agent_attempt_execution(request))
        store.claim(agent_attempt_execution(request))
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql(trigger)

        refusal = RunTransitionConflict if failpoint == "event" else DatabaseError
        with pytest.raises(refusal, match="failpoint"):
            if terminal == "success":
                store.complete_success(
                    agent_attempt_execution(request), AgentExecutionResult(b'"done"')
                )
            else:
                store.complete_known_failure(
                    agent_attempt_execution(request), process_exit()
                )

        with runtime.engine.connect() as connection:
            attempt = connection.execute(sa.select(agent_attempts)).mappings().one()
            run = connection.execute(sa.select(runs)).mappings().one()
            receipt_count = connection.scalar(
                sa.select(sa.func.count()).select_from(agent_receipts_v2)
            )
            event_count = connection.scalar(
                sa.select(sa.func.count()).select_from(run_events)
            )
        assert (attempt["state"], attempt["state_version"]) == ("LAUNCH_ARMED", 1)
        assert (run["current_node_id"], run["state_version"]) == ("build", 0)
        assert (receipt_count, event_count) == (0, 0)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "mutation",
    (
        "UPDATE agent_attempts SET executor_operational_identity='other'",
        (
            "UPDATE agent_attempts SET state='FAILED', state_version=2, "
            "failure_code='PROCESS_EXITED_UNSUCCESSFULLY'"
        ),
        "DELETE FROM agent_attempts",
    ),
)
def test_attempt_trigger_rejects_binding_skips_and_deletion(
    tmp_path: Path, mutation: str
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/trigger-canary")
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(agent_attempt_execution(request))

        with pytest.raises(IntegrityError), runtime.engine.begin() as connection:
            connection.exec_driver_sql(mutation)

        with runtime.engine.connect() as connection:
            record = connection.execute(sa.select(agent_attempts)).mappings().one()
        assert (
            record["state"],
            record["state_version"],
            record["executor_operational_identity"],
        ) == ("PREPARED", 0, request.executor_operational_identity.value)
    finally:
        runtime.close()


def test_attempt_trigger_rejects_terminal_rewrite_and_mismatched_receipt(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/trigger-terminal")
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(agent_attempt_execution(request))
        store.claim(agent_attempt_execution(request))
        failed = store.complete_known_failure(
            agent_attempt_execution(request), process_exit()
        )

        with pytest.raises(IntegrityError), runtime.engine.begin() as connection:
            connection.execute(
                agent_attempts.update().values(
                    state="LAUNCH_ARMED",
                    state_version=1,
                    failure_code=None,
                )
            )
        assert store.claim(agent_attempt_execution(request)) == failed

        completed_request = attempt_request(runtime, "attempt/trigger-receipt-source")
        store.prepare(agent_attempt_execution(completed_request))
        store.claim(agent_attempt_execution(completed_request))
        completed = store.complete_success(
            agent_attempt_execution(completed_request), AgentExecutionResult(b'"wrong"')
        )
        assert completed.attempt.receipt_hash is not None

        second = attempt_request(runtime, "attempt/trigger-receipt-target")
        store.prepare(agent_attempt_execution(second))
        store.claim(agent_attempt_execution(second))
        with (
            runtime.engine.begin() as connection,
            pytest.raises(IntegrityError),
        ):
            connection.execute(
                agent_attempts.update()
                .where(agent_attempts.c.request_hash == second.request_hash.value)
                .values(
                    state="SUCCEEDED",
                    state_version=2,
                    receipt_hash=completed.attempt.receipt_hash.value,
                )
            )
    finally:
        runtime.close()


def test_reentry_after_a_terminal_success_refuses_a_run_head_that_disagrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A terminal agent success is one durable write: the attempt CAS, the
    # AGENT_COMPLETED event and the completed run head land together. If a run
    # head ever disagreed with a SUCCEEDED attempt, the torn half must surface
    # rather than be reconstructed into a success nobody durably made.
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/terminal-reentry")
        execution = agent_attempt_execution(request)
        _stage_agent_sink_graph(monkeypatch)
        executor = inspecting_executor(runtime)
        store = DbosAgentAttemptStore(runtime.engine)

        first = execute_agent_attempt(
            execution,
            executor,
            store,
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )
        assert isinstance(first, AgentAttemptSucceeded)
        assert first.completion == RunCompletes()

        with runtime.engine.begin() as connection:
            connection.execute(
                runs.update().values(state=RunState.STARTED.value, terminal_hash=None)
            )

        with pytest.raises(RunTransitionConflict):
            execute_agent_attempt(
                execution,
                executor,
                DbosAgentAttemptStore(runtime.engine),
                runtime.agent_process_supervisor,
                runtime_workspace_owner(runtime),
            )

        assert len(executor.results) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("status", "driverless"),
    (
        ("PENDING", False),
        ("ENQUEUED", False),
        ("DELAYED", False),
        ("SUCCESS", True),
        ("ERROR", True),
        ("CANCELLED", True),
        ("MAX_RECOVERY_ATTEMPTS_EXCEEDED", True),
    ),
)
def test_an_attempt_is_driverless_once_its_workflow_can_no_longer_move_it(
    tmp_path: Path, status: str, driverless: bool
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime)
        store = _driverless_store(runtime)
        attempt = store.prepare(agent_attempt_execution(request))
        _record_driving_workflow(runtime, _the_driving_workflow(attempt), status)

        assert tuple(store.iter_driverless_attempts(PageLimit(1))) == (
            (attempt,) if driverless else ()
        )
    finally:
        runtime.close()


def test_a_cancelled_runner_bound_attempt_is_still_owed_a_move_by_the_slot(
    tmp_path: Path,
) -> None:
    """#584: cancelling a lease a launcher already claimed defers the ending.

    The cancellation workflow does not finish such an Attempt itself; it hands it
    back to the slot workflow that is driving the session, which ends it on the
    Runner's own evidence. Naming only the cancellation would leave a restart's
    sweep free to converge an Attempt the slot is about to end -- two writers for
    one ending -- so both are named and the status read decides which is alive.
    """

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime)
        store = _driverless_store(runtime)
        execution = agent_attempt_execution(request)
        prepared = store.prepare(execution)
        armed = store.bind_runner_generation(
            execution,
            RunnerGenerationBinding(
                prepared.attempt_id,
                prepared.request_hash,
                RunnerGenerationId("g" * 43),
                runner_manifest_id(free_runner_candidate_manifest()),
            ),
        )
        accepted = store.request_cancellation(
            CancelAgentAttemptRequest(
                armed.run_id,
                armed.attempt_id,
                "operator-cancel:lease",
                armed.state_version,
                AgentAttemptReplacement.NONE,
            )
        )
        assert isinstance(accepted, AgentAttemptCancellationAccepted), accepted
        cancelled = store.load(armed.attempt_id)
        assert cancelled.cancellation is not None
        assert cancelled.runner_manifest_id is not None

        named = driving_workflow_ids(cancelled)
        assert cancellation_workflow_id_for(stop_command_for(cancelled)) in named
        assert (
            runner_lease_workflow_id_for(
                cancelled.node_execution_id, AGENT_ATTEMPT_ORDINAL
            )
            in named
        )
    finally:
        runtime.close()


def test_a_driving_row_of_a_retired_version_does_not_hide_a_driverless_attempt(
    tmp_path: Path,
) -> None:
    """A driving row belongs to the version that wrote it, and to no other.

    DBOS resumes workflows of the version it is running, so a row a retired
    deployment left behind is never going to move again however driving its
    status reads. Counting it as a live driver hides its attempt from every later
    sweep, and the attempt then stands non-terminal for as long as the store
    exists.

    This is the sweep that owns attempts no Runner carries; the Runner-bound half
    of the same rule is proven against the sweep that owns those, in
    `tests/integration/test_workflow_lease_dispatch.py`.
    """

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime)
        store = _driverless_store(runtime)
        attempt = store.prepare(agent_attempt_execution(request))
        for workflow_id in driving_workflow_ids(attempt):
            _record_driving_workflow(
                runtime,
                workflow_id,
                "PENDING",
                application_version="a-version-this-runtime-retired",
            )

        assert tuple(store.iter_driverless_attempts(PageLimit(1))) == (attempt,)
    finally:
        runtime.close()


def test_an_attempt_whose_workflow_never_reached_the_store_is_driverless(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime)
        store = _driverless_store(runtime)
        attempt = store.prepare(agent_attempt_execution(request))

        assert tuple(store.iter_driverless_attempts(PageLimit(1))) == (attempt,)
    finally:
        runtime.close()


def test_a_terminal_attempt_is_never_driverless(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime)
        store = _driverless_store(runtime)
        execute_agent_attempt(
            agent_attempt_execution(request),
            inspecting_executor(runtime),
            store,
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )

        assert tuple(store.iter_driverless_attempts(PageLimit(1))) == ()
    finally:
        runtime.close()


def test_driverless_iteration_advances_past_a_fully_driven_page(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        store = _driverless_store(runtime)
        ordered = _ordered_prepared_attempts(runtime, store, "attempt/keyset", 3)
        for attempt in ordered[:2]:
            _record_driving_workflow(runtime, _the_driving_workflow(attempt), "PENDING")

        assert tuple(store.iter_driverless_attempts(PageLimit(2))) == (ordered[2],)
    finally:
        runtime.close()


def test_driverless_iteration_loads_only_one_page_before_its_first_yield(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        store = _driverless_store(runtime)
        ordered = _ordered_prepared_attempts(runtime, store, "attempt/lazy", 3)
        observed_reads: list[str] = []

        def observe_reads(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if (
                "FROM agent_attempts" in statement
                or "FROM workflow_status" in statement
            ):
                observed_reads.append(statement)

        event.listen(runtime.engine, "before_cursor_execute", observe_reads)
        try:
            attempts = store.iter_driverless_attempts(PageLimit(2))
            assert next(attempts) == ordered[0]
            assert len(observed_reads) == 2
            assert next(attempts) == ordered[1]
            assert len(observed_reads) == 2
            assert next(attempts) == ordered[2]
            assert len(observed_reads) == 4
            with pytest.raises(StopIteration):
                next(attempts)
            assert len(observed_reads) == 4
        finally:
            event.remove(runtime.engine, "before_cursor_execute", observe_reads)
    finally:
        runtime.close()


def test_driverless_iteration_reads_later_pages_from_fresh_durable_truth(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        store = _driverless_store(runtime)
        ordered = _ordered_prepared_attempts(runtime, store, "attempt/fresh", 2)

        attempts = store.iter_driverless_attempts(PageLimit(1))
        assert next(attempts) == ordered[0]
        _record_driving_workflow(runtime, _the_driving_workflow(ordered[1]), "PENDING")

        assert tuple(attempts) == ()
    finally:
        runtime.close()


def test_driverless_iteration_restart_has_no_hidden_cursor(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        store = _driverless_store(runtime)
        ordered = _ordered_prepared_attempts(runtime, store, "attempt/restart", 2)

        interrupted = store.iter_driverless_attempts(PageLimit(1))
        assert next(interrupted) == ordered[0]
        del interrupted

        assert tuple(store.iter_driverless_attempts(PageLimit(1))) == ordered
    finally:
        runtime.close()


def test_driverless_iteration_bounds_ten_thousand_row_queries(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        columns = tuple(agent_attempts.c.keys())
        placeholders = ", ".join(f":{column}" for column in columns)
        insert = (
            f"INSERT INTO agent_attempts ({', '.join(columns)}) VALUES ({placeholders})"
        )

        # This load proof needs valid production identities and row encoding, not
        # ten thousand unrelated parent runs whose behavior it does not exercise.
        with sqlite3.connect(runtime.settings.database_path, timeout=30) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.executemany(
                insert,
                (
                    agent_attempt_store_module._attempt_values(
                        prepared_agent_attempt(
                            agent_attempt_execution(
                                agent_execution_request_v2(
                                    f"attempt/bounded-load/{index}"
                                )
                            )
                        )
                    )
                    for index in range(10_000)
                ),
            )

        observed_reads: list[tuple[str, int]] = []

        def observe_reads(
            _connection: object,
            _cursor: object,
            statement: str,
            parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if not isinstance(parameters, Sized):
                raise TypeError("SQL parameters are not a sized collection")
            if "FROM agent_attempts" in statement:
                observed_reads.append(("attempts", len(parameters)))
            elif "FROM workflow_status" in statement:
                observed_reads.append(("workflows", len(parameters)))

        event.listen(runtime.engine, "before_cursor_execute", observe_reads)
        try:
            discovered = sum(
                1
                for _attempt in _driverless_store(runtime).iter_driverless_attempts(
                    PageLimit(MAXIMUM_PAGE_ITEMS)
                )
            )
        finally:
            event.remove(runtime.engine, "before_cursor_execute", observe_reads)

        attempt_reads = tuple(
            parameters for owner, parameters in observed_reads if owner == "attempts"
        )
        workflow_reads = tuple(
            parameters for owner, parameters in observed_reads if owner == "workflows"
        )
        assert discovered == 10_000
        assert len(attempt_reads) == 101
        assert len(workflow_reads) == 100
        # One page of attempts, each naming the workflows that can still drive it
        # -- its own, and the Runner slot its row cannot rule out -- plus the
        # three driving statuses and the application version they must belong to.
        # Bounded by the page, not by the store's size.
        assert max(workflow_reads) == MAXIMUM_PAGE_ITEMS * 2 + 4
        assert len(observed_reads) == 201
    finally:
        runtime.close()


def test_a_changed_permission_policy_leaves_a_live_attempt_recoverable(
    tmp_path: Path,
) -> None:
    """The policy is bound at dispatch, so no part of it reaches durable identity.

    A deployment that widens or narrows what a provider may do must not orphan
    the attempts already in flight: a re-entering call reconstructs the very same
    attempt from the same request rather than minting a second one beside it.
    """

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/policy-bound-at-dispatch")
        executor = inspecting_executor(runtime)
        execution = agent_attempt_execution(request)
        may_read_one_path = PermissionPolicyRevision(
            frozenset(
                {
                    (
                        PermissionEffect.WORKSPACE_READ,
                        PermissionScope(PermissionScopeKind.PATH_PREFIX, "/lease/"),
                    )
                }
            )
        )
        assert may_read_one_path.revision_hash != GRANTS_NOTHING.revision_hash

        first = execute_agent_attempt(
            execution,
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
            permissions=GRANTS_NOTHING,
        )
        recovered = execute_agent_attempt(
            agent_attempt_execution(request),
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
            permissions=may_read_one_path,
        )

        assert isinstance(first, AgentAttemptSucceeded)
        assert recovered == first
        assert len(executor.results) == 1
        durable = DbosAgentAttemptStore(runtime.engine).load(execution.attempt_id)
        assert durable.request_hash == request.request_hash
    finally:
        runtime.close()
