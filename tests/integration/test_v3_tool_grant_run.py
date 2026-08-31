"""A node pins a tool grant, the run redeems it, and the redemption is durable.

`AgentNodeV3.tools` was cut and locked: the format could say which tool a node
needs, the executable admission refused the form as one nothing binds, and no
run could act on it. This is the head where saying it means something -- and the
proof is the whole vertical, driven from the public start seam and read back from
the store, because each half alone would be a promise: an admitted `tools` the
run ignores, or a redemption nothing could have asked for.

What is measured here is exactly what an operator can see afterwards: the run
finished, the command the project's own manifest declares ran in that attempt's
own directory -- filled with the tree the run's own binding pinned, so the
manifest that declared the command and the ground it ran on are one commit -- and
the row that proves it carries the command, the exit code and the hash of what it
wrote, beside an agent receipt whose provider bytes are untouched by any of it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
    node_receipts_v3,
    published_revisions,
    run_events,
    runs,
    tool_redemptions,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode, AgentAttemptState
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.node_records_v3 import (
    NodeReceiptReason,
    read_stored_node_receipt_reason,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_forks import RunForkCommandId
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_run_forks import DurableRunForkCreated, ForkRunRequest
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    DurableRunFormatNotExecutable,
    StartPublishedRunRequestV2,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    publish_checked_model_registry,
)
from tests.scenarios.projects import declaring_verification, git_project
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("v3/redeems-its-grant")
FAILED_RUN = RunId("v3/red-verify-fails")
TIMEOUT_RUN = RunId("v3/verify-timeout")
UNKEPT_RUN = RunId("v3/candidate-unkeepable")
BOTH_LOST_RUN = RunId("v3/red-verify-and-unkeepable")
PROVIDER_OUTPUT = b'"the exact provider bytes"'
VERIFICATION_OUTPUT = b"all green"
VERIFICATION_EXIT_CODE = 0
FAILED_VERIFICATION_EXIT_CODE = 1
DECLARED_VERIFICATION_TIMEOUT_SECONDS = 0.2

COMMITTED_MARKER_NAME = "marker.txt"
COMMITTED_MARKER = "the tree this run was pinned to\n"

THE_GRANT = json.dumps(
    {"capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION.value}
).encode("utf-8")


def one_node_document(grant_revision: str) -> bytes:
    return (
        b"""format_version: 3
name: One agent that must verify the project
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
    tools:
      - {ref: project-verification, revision: %s}
"""
        % grant_revision.encode("ascii")
        + declared_output()
    )


def project_declaring_its_verification(
    root: Path,
    record: Path,
    exit_code: int = VERIFICATION_EXIT_CODE,
    *,
    verification_command: list[str] | None = None,
    timeout_seconds: float = 30,
) -> Path:
    """A project whose manifest states the one command that verifies it.

    The command records where it was started and reads a file only its own commit
    carries, so after the lease is gone both facts are still measurable: which
    directory the verification ran in, and that the pinned tree stood in it.
    A caller that passes `verification_command` owns the argv instead -- a
    timeout scenario has no output to record.
    """
    command = verification_command or [
        "/bin/sh",
        "-c",
        (
            f"pwd > {record}; cat {COMMITTED_MARKER_NAME} >> {record}; "
            f"printf '{VERIFICATION_OUTPUT.decode('ascii')}'; "
            f"exit {exit_code}"
        ),
    ]
    git_project(
        root,
        {
            **declaring_verification(command, timeout_seconds),
            COMMITTED_MARKER_NAME: COMMITTED_MARKER,
        },
    )
    return root


def granted_runtime(
    tmp_path: Path,
    exit_code: int,
    *,
    verification_command: list[str] | None = None,
    timeout_seconds: float = 30,
) -> Iterator[tuple[DbosRuntime, Path, Path]]:
    """A runtime that can run an agent and redeem a grant against one project."""
    cwd_record = tmp_path / "verification-cwd.txt"
    project_root = project_declaring_its_verification(
        tmp_path / "project",
        cwd_record,
        exit_code,
        verification_command=verification_command,
        timeout_seconds=timeout_seconds,
    )
    scratch_root = agent_scratch_root(tmp_path)
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-tool-grant-test",
            agent_scratch_root=scratch_root,
            project_id=ProjectId("granted"),
            bootstrap_project_root=project_root,
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (
            RecordingAgentExecutorFactoryV2(
                "exact", "exact/v1", "exact-op", PROVIDER_OUTPUT
            ),
        ),
    )
    started.initialize_storage()
    try:
        yield started, scratch_root, cwd_record
    finally:
        started.close()


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[tuple[DbosRuntime, Path, Path]]:
    yield from granted_runtime(tmp_path, VERIFICATION_EXIT_CODE)


@pytest.fixture
def failing_verification_runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, Path, Path]]:
    yield from granted_runtime(tmp_path, FAILED_VERIFICATION_EXIT_CODE)


@pytest.fixture
def unkeepable_candidate_and_failing_verification_runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, Path, Path]]:
    """A project whose check says no, and whose candidates could not be kept.

    Both losses at once, because the question is which of them decides the
    ending: a check that exited nonzero has already refused this work, and no
    later failure to keep it may rename that verdict or leave a redemption
    behind claiming a command that failed.
    """

    blocked = tmp_path / CANDIDATE_STORE_DIRECTORY_NAME
    blocked.symlink_to(tmp_path / "somewhere-else", target_is_directory=True)
    yield from granted_runtime(tmp_path, FAILED_VERIFICATION_EXIT_CODE)


@pytest.fixture
def unkeepable_candidate_runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, Path, Path]]:
    """A runtime whose project can work and verify, but cannot keep what it made.

    The store is blocked the way a project root can really be blocked -- a link
    standing where the candidate store belongs, which ADR 0011's placement rule
    refuses because it would take the work outside the root. Nothing is
    monkeypatched: the runtime builds its own store, and that store says no.
    """

    blocked = tmp_path / CANDIDATE_STORE_DIRECTORY_NAME
    blocked.symlink_to(tmp_path / "somewhere-else", target_is_directory=True)
    yield from granted_runtime(tmp_path, VERIFICATION_EXIT_CODE)


@pytest.fixture
def timeout_verification_runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, Path, Path]]:
    yield from granted_runtime(
        tmp_path,
        VERIFICATION_EXIT_CODE,
        verification_command=["/bin/sh", "-c", "sleep 30"],
        timeout_seconds=DECLARED_VERIFICATION_TIMEOUT_SECONDS,
    )


def publish_granted_node(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet, str]:
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    grant = PublishedRevision(RevisionKind.TOOL, THE_GRANT)
    with runtime.engine.begin() as connection:
        for revision in (grant, ANY_JSON_SCHEMA):
            connection.execute(
                published_revisions.insert().values(
                    kind=revision.kind.value,
                    revision_hash=revision.revision_hash.value,
                    document=revision.document,
                )
            )
    workflow = WorkflowRevision(one_node_document(grant.revision_hash.value))
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    return workflow, bindings, grant.revision_hash.value


def wait_for_state(runtime: DbosRuntime, state: RunState) -> None:
    deadline = time.monotonic() + 20
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
                )
            )
        if observed == state.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed!r}, expected {state.value!r}")


@pytest.mark.proves("a-granted-node-gets-its-project-verification-run-and-proven")
@pytest.mark.proves("what-a-project-declares-and-where-it-runs-are-one-commit")
def test_a_granted_node_runs_the_projects_verification_and_leaves_the_proof(
    runtime: tuple[DbosRuntime, Path, Path],
) -> None:
    started_runtime, scratch_root, cwd_record = runtime
    workflow, bindings, grant_revision = publish_granted_node(started_runtime)

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)

    with started_runtime.engine.connect() as connection:
        redemption = (
            connection.execute(
                sa.select(tool_redemptions).where(
                    tool_redemptions.c.run_id == RUN.value
                )
            )
            .mappings()
            .one()
        )
        provider_output = (
            connection.execute(
                sa.select(agent_receipts_v2.c.output_bytes).where(
                    agent_receipts_v2.c.run_id == RUN.value
                )
            )
            .scalars()
            .one()
        )

    assert str(redemption["node_id"]) == "implement"
    assert str(redemption["capability"]) == (
        ToolGrantCapability.RUN_PROJECT_VERIFICATION.value
    )
    assert str(redemption["tool_revision_hash"]) == grant_revision
    assert json.loads(str(redemption["command"]))[:2] == ["/bin/sh", "-c"]
    assert int(redemption["exit_code"]) == VERIFICATION_EXIT_CODE
    assert (
        str(redemption["standard_output_hash"])
        == Sha256Hash.of(VERIFICATION_OUTPUT).value
    )
    # The attempt owns the place, and the pin owns the material: the verification
    # started in that attempt's own leased directory -- not in the project it
    # verifies and not in the server's -- and the tree the binding pinned stood
    # there to be read.
    where, marker = cwd_record.read_text(encoding="utf-8").split("\n", 1)
    assert Path(where).parent == scratch_root
    assert marker == COMMITTED_MARKER
    # The provider's own bytes are the agent receipt's, and redeeming a grant
    # beside them changes neither what they are nor who answers for them.
    assert bytes(provider_output) == PROVIDER_OUTPUT
    # Proof that cannot be rewritten afterwards is what makes it proof.
    for rewrite in (
        tool_redemptions.update().values(exit_code=0),
        tool_redemptions.delete(),
    ):
        with (
            pytest.raises(IntegrityError, match="tool redemptions are immutable"),
            started_runtime.engine.begin() as connection,
        ):
            connection.execute(rewrite)


def test_a_completed_verification_tool_node_can_be_forked_without_an_effect_receipt(
    runtime: tuple[DbosRuntime, Path, Path],
) -> None:
    started_runtime, _scratch_root, _cwd_record = runtime
    workflow, bindings, _grant_revision = publish_granted_node(started_runtime)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    assert isinstance(
        starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        ),
        DurableRunCreated,
    )
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)

    forked = starter.fork_run(
        ForkRunRequest(RUN, "verification-is-not-an-effect", "implement")
    )

    assert isinstance(forked, DurableRunForkCreated)
    assert forked.fork.command_id == RunForkCommandId.for_request(
        RUN, "verification-is-not-an-effect"
    )


@pytest.mark.proves("a-nonzero-project-verification-fails-the-attempt-durably-named")
def test_a_nonzero_project_verification_fails_the_attempt_and_leaves_no_success(
    failing_verification_runtime: tuple[DbosRuntime, Path, Path],
) -> None:
    """A granted check that exits 1 is a named failure, not a completed run.

    The provider's bytes were a success the schema admits. The project's own
    command then exited 1. That ending must not write the success rows a
    zero-exit grant writes: no agent receipt, no `AGENT_COMPLETED`, and no
    `tool_redemptions` row -- not because a failed attempt has nowhere to put
    one since V39, but because a check that exited 1 redeemed nothing. What
    remains is the named failure.
    """
    started_runtime, _scratch_root, _cwd_record = failing_verification_runtime
    workflow, bindings, _grant_revision = publish_granted_node(started_runtime)

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(FAILED_RUN, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    deadline = time.monotonic() + 20
    observed = ""
    while time.monotonic() < deadline:
        with started_runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == FAILED_RUN.value)
                )
            )
        if observed in {RunState.FAILED.value, RunState.COMPLETED.value}:
            break
        time.sleep(0.025)
    assert observed == RunState.FAILED.value, f"run ended {observed!r}"

    with started_runtime.engine.connect() as connection:
        attempt = (
            connection.execute(
                sa.select(agent_attempts).where(
                    agent_attempts.c.run_id == FAILED_RUN.value
                )
            )
            .mappings()
            .one()
        )
        event_kinds = tuple(
            connection.scalars(
                sa.select(run_events.c.event_kind).where(
                    run_events.c.run_id == FAILED_RUN.value
                )
            )
        )
        payload = connection.scalar(
            sa.select(run_events.c.payload).where(
                run_events.c.run_id == FAILED_RUN.value,
                run_events.c.event_kind == RunEventKind.AGENT_FAILED.value,
            )
        )
        stored_reason = connection.scalar(
            sa.select(node_receipts_v3.c.reason).where(
                node_receipts_v3.c.node_execution_id == attempt["node_execution_id"]
            )
        )
        receipt_count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(agent_receipts_v2)
            .where(agent_receipts_v2.c.run_id == FAILED_RUN.value)
        )
        redemption_count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(tool_redemptions)
            .where(tool_redemptions.c.run_id == FAILED_RUN.value)
        )

    assert attempt["state"] == AgentAttemptState.FAILED.value
    assert attempt["failure_code"] == (
        AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED.value
    )
    assert event_kinds == (RunEventKind.AGENT_FAILED.value,)
    assert payload is not None
    assert bytes(payload) == (
        AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED.value.encode("ascii")
    )
    words, schema_revision, value_hash = read_stored_node_receipt_reason(
        str(stored_reason)
    )
    assert words.startswith(NodeReceiptReason.PROJECT_VERIFICATION_FAILED.value)
    assert f"exit {FAILED_VERIFICATION_EXIT_CODE}" in words
    assert schema_revision is None
    assert value_hash is None
    assert receipt_count == 0
    assert redemption_count == 0


@pytest.mark.proves(
    "a-verification-timeout-after-claim-fails-the-attempt-durably-named"
)
def test_a_verification_that_times_out_after_claim_fails_the_attempt_named(
    timeout_verification_runtime: tuple[DbosRuntime, Path, Path],
) -> None:
    """A granted check past its declared deadline is a named failure, not armed.

    The provider's bytes were a success the schema admits. The project's own
    command then exceeded `timeout_seconds`. That ending must not leave the
    attempt `LAUNCH_ARMED` (replay would be `AgentAttemptPossiblyRan`), and it
    must not invent an exit code for a command that never answered. What
    remains is the named failure, with the timeout in the receipt reason.
    """
    started_runtime, _scratch_root, _cwd_record = timeout_verification_runtime
    workflow, bindings, _grant_revision = publish_granted_node(started_runtime)

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(TIMEOUT_RUN, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    deadline = time.monotonic() + 20
    observed = ""
    while time.monotonic() < deadline:
        with started_runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == TIMEOUT_RUN.value)
                )
            )
        if observed in {RunState.FAILED.value, RunState.COMPLETED.value}:
            break
        time.sleep(0.025)
    assert observed == RunState.FAILED.value, f"run ended {observed!r}"

    with started_runtime.engine.connect() as connection:
        attempt = (
            connection.execute(
                sa.select(agent_attempts).where(
                    agent_attempts.c.run_id == TIMEOUT_RUN.value
                )
            )
            .mappings()
            .one()
        )
        event_kinds = tuple(
            connection.scalars(
                sa.select(run_events.c.event_kind).where(
                    run_events.c.run_id == TIMEOUT_RUN.value
                )
            )
        )
        payload = connection.scalar(
            sa.select(run_events.c.payload).where(
                run_events.c.run_id == TIMEOUT_RUN.value,
                run_events.c.event_kind == RunEventKind.AGENT_FAILED.value,
            )
        )
        stored_reason = connection.scalar(
            sa.select(node_receipts_v3.c.reason).where(
                node_receipts_v3.c.node_execution_id == attempt["node_execution_id"]
            )
        )
        receipt_count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(agent_receipts_v2)
            .where(agent_receipts_v2.c.run_id == TIMEOUT_RUN.value)
        )
        redemption_count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(tool_redemptions)
            .where(tool_redemptions.c.run_id == TIMEOUT_RUN.value)
        )

    assert attempt["state"] == AgentAttemptState.FAILED.value
    assert attempt["state"] != AgentAttemptState.LAUNCH_ARMED.value
    assert attempt["failure_code"] == (
        AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED.value
    )
    assert event_kinds == (RunEventKind.AGENT_FAILED.value,)
    assert payload is not None
    assert bytes(payload) == (
        AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED.value.encode("ascii")
    )
    words, schema_revision, value_hash = read_stored_node_receipt_reason(
        str(stored_reason)
    )
    assert words.startswith(NodeReceiptReason.PROJECT_VERIFICATION_FAILED.value)
    assert f"timeout {DECLARED_VERIFICATION_TIMEOUT_SECONDS} seconds" in words
    assert schema_revision is None
    assert value_hash is None
    assert receipt_count == 0
    assert redemption_count == 0


@pytest.mark.proves("a-tool-grant-this-runtime-cannot-redeem-is-refused-by-name")
def test_a_grant_no_registry_carries_refuses_the_start_and_leaves_no_run(
    runtime: tuple[DbosRuntime, Path, Path],
) -> None:
    """The third refusal is the resolution's, and it is measured rather than rebuilt.

    Nothing new answers here: `tools` is a declared reference, so the run
    configuration that freezes every reference before a run exists is what
    refuses an unpublished one -- at the public start, with no run to clean up.
    """
    started_runtime, _scratch_root, _cwd_record = runtime
    _workflow, bindings, _grant = publish_granted_node(started_runtime)
    ungranted = WorkflowRevision(one_node_document("f0" * 32))
    DbosWorkflowRevisionPublisher(started_runtime.engine).publish(ungranted)

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(
            RunId("v3/unpublished-grant"), ungranted.revision_hash, bindings
        )
    )

    assert isinstance(started, DurableRunFormatNotExecutable)
    with started_runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


def test_an_attempt_that_could_not_keep_its_work_says_so_in_its_node_receipt(
    unkeepable_candidate_runtime: tuple[DbosRuntime, Path, Path],
) -> None:
    """The receipt an operator reads has to name this loss, and not another one.

    Everything before the keeping went right here: the provider answered, the
    schema admitted the bytes, and the project's own granted check exited zero.
    Only the candidate store refused. The receipt is where that shows up for a
    human, so it is asked directly -- because a capture failure recorded as
    `project-verification-failed` would tell an operator to go and look at a
    check that passed, and the attempt's own code alone cannot reveal that.
    """
    started_runtime, _scratch_root, _cwd_record = unkeepable_candidate_runtime
    workflow, bindings, _grant_revision = publish_granted_node(started_runtime)

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(UNKEPT_RUN, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    deadline = time.monotonic() + 20
    observed = ""
    while time.monotonic() < deadline:
        with started_runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == UNKEPT_RUN.value)
                )
            )
        if observed in {RunState.FAILED.value, RunState.COMPLETED.value}:
            break
        time.sleep(0.025)
    assert observed == RunState.FAILED.value, f"run ended {observed!r}"

    with started_runtime.engine.connect() as connection:
        attempt = (
            connection.execute(
                sa.select(agent_attempts).where(
                    agent_attempts.c.run_id == UNKEPT_RUN.value
                )
            )
            .mappings()
            .one()
        )
        payload = connection.scalar(
            sa.select(run_events.c.payload).where(
                run_events.c.run_id == UNKEPT_RUN.value,
                run_events.c.event_kind == RunEventKind.AGENT_FAILED.value,
            )
        )
        stored_reason = connection.scalar(
            sa.select(node_receipts_v3.c.reason).where(
                node_receipts_v3.c.node_execution_id == attempt["node_execution_id"]
            )
        )
        redemption = (
            connection.execute(
                sa.select(tool_redemptions).where(
                    tool_redemptions.c.run_id == UNKEPT_RUN.value
                )
            )
            .mappings()
            .one()
        )
        receipt_count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(agent_receipts_v2)
            .where(agent_receipts_v2.c.run_id == UNKEPT_RUN.value)
        )

    assert attempt["state"] == AgentAttemptState.FAILED.value
    assert attempt["failure_code"] == (
        AgentAttemptFailureCode.CANDIDATE_CAPTURE_FAILED.value
    )
    assert payload is not None
    assert bytes(payload) == (
        AgentAttemptFailureCode.CANDIDATE_CAPTURE_FAILED.value.encode("ascii")
    )
    words, schema_revision, value_hash = read_stored_node_receipt_reason(
        str(stored_reason)
    )
    token, _separator, verdict = words.partition(": ")
    assert token == NodeReceiptReason.CANDIDATE_CAPTURE_FAILED.value
    assert CANDIDATE_STORE_DIRECTORY_NAME in verdict
    assert schema_revision is None
    assert value_hash is None
    # The check ran and passed, and its proof is durable beside the failure --
    # keyed by the attempt, which is why it can exist at all now: there is no
    # agent receipt here for it to hang from.
    assert str(redemption["attempt_id"]) == str(attempt["attempt_id"])
    assert str(redemption["node_id"]) == "implement"
    assert str(redemption["capability"]) == (
        ToolGrantCapability.RUN_PROJECT_VERIFICATION.value
    )
    assert int(redemption["exit_code"]) == VERIFICATION_EXIT_CODE
    assert (
        str(redemption["standard_output_hash"])
        == Sha256Hash.of(VERIFICATION_OUTPUT).value
    )
    assert receipt_count == 0


def test_a_check_that_said_no_decides_the_ending_even_when_the_work_is_lost(
    unkeepable_candidate_and_failing_verification_runtime: tuple[
        DbosRuntime, Path, Path
    ],
) -> None:
    """Two losses at once, and the first one owns the verdict.

    The project's command exited nonzero, so this attempt was already refused;
    the candidate store then could not have kept the work either. Reading that
    second loss as the ending would tell an operator to go and look at a store
    when what actually happened is that their tests failed -- and it would leave
    a `tool_redemptions` row recording a command that did not pass, which
    `docs/PRODUCT.md` says is never written and which V39's own CHECK refuses.
    """
    started_runtime, _scratch_root, _cwd_record = (
        unkeepable_candidate_and_failing_verification_runtime
    )
    workflow, bindings, _grant_revision = publish_granted_node(started_runtime)

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(BOTH_LOST_RUN, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    deadline = time.monotonic() + 20
    observed = ""
    while time.monotonic() < deadline:
        with started_runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == BOTH_LOST_RUN.value)
                )
            )
        if observed in {RunState.FAILED.value, RunState.COMPLETED.value}:
            break
        time.sleep(0.025)
    assert observed == RunState.FAILED.value, f"run ended {observed!r}"

    with started_runtime.engine.connect() as connection:
        attempt = (
            connection.execute(
                sa.select(agent_attempts).where(
                    agent_attempts.c.run_id == BOTH_LOST_RUN.value
                )
            )
            .mappings()
            .one()
        )
        stored_reason = connection.scalar(
            sa.select(node_receipts_v3.c.reason).where(
                node_receipts_v3.c.node_execution_id == attempt["node_execution_id"]
            )
        )
        redemption_count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(tool_redemptions)
            .where(tool_redemptions.c.run_id == BOTH_LOST_RUN.value)
        )

    assert attempt["failure_code"] == (
        AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED.value
    )
    words, _schema_revision, _value_hash = read_stored_node_receipt_reason(
        str(stored_reason)
    )
    assert words.startswith(NodeReceiptReason.PROJECT_VERIFICATION_FAILED.value)
    assert f"exit {FAILED_VERIFICATION_EXIT_CODE}" in words
    assert redemption_count == 0
