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
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.artifact_store import DbosArtifactStore
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
from atelier2.adapters.dbos.workflow_ids import node_workflow_id_for
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
from atelier2.contracts.artifacts import Artifact, ArtifactHash
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import NodeExecutionId, RunEventKind
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.node_records_v3 import NodeReceiptReason
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_forks import RunForkCommandId
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.secret_redaction import REDACTION_MARKER
from atelier2.contracts.stored_node_receipt_reasons import (
    read_stored_node_receipt_reason,
)
from atelier2.contracts.tool_grants_v3 import (
    MAXIMUM_RECEIPTED_VERIFICATION_SUMMARY_BYTES,
    ToolGrantCapability,
)
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
    emitting,
    publish_checked_model_registry,
    working_and_emitting,
)
from tests.scenarios.projects import declaring_verification, git_project
from tests.scenarios.run_waiting import (
    wait_for_run_state,
    wait_for_workflow_completion,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("v3/redeems-its-grant")
FAILED_RUN = RunId("v3/red-verify-fails")
TIMEOUT_RUN = RunId("v3/verify-timeout")
UNKEPT_RUN = RunId("v3/candidate-unkeepable")
UNCHANGED_RUN = RunId("v3/candidate-unchanged")
BOTH_LOST_RUN = RunId("v3/red-verify-and-unkeepable")
PROVIDER_OUTPUT = b'"the exact provider bytes"'
VERIFICATION_OUTPUT = b"all green"
VERIFICATION_EXIT_CODE = 0
FAILED_VERIFICATION_EXIT_CODE = 1
DECLARED_VERIFICATION_TIMEOUT_SECONDS = 0.2

COMMITTED_MARKER_NAME = "marker.txt"
COMMITTED_MARKER = "the tree this run was pinned to\n"

WHAT_THE_AGENT_MADE = "made-by-the-agent.txt"
MADE_BY_THE_AGENT = "the change this attempt is about\n"
"""The one file this scenario's provider leaves behind in its lease.

Every sentence here is about what happens *after* an agent did something -- the
grant it redeems, the check that judges the work, whether the work is kept. An
attempt that changed nothing ends before any of that, under its own name, so a
provider that only printed an answer would prove none of them.
"""

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
    provider_changes_the_tree: bool = True,
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
                "exact",
                "exact/v1",
                "exact-op",
                PROVIDER_OUTPUT,
                command=working_and_emitting(
                    PROVIDER_OUTPUT, WHAT_THE_AGENT_MADE, MADE_BY_THE_AGENT
                )
                if provider_changes_the_tree
                else emitting(PROVIDER_OUTPUT),
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
def unchanged_tree_runtime(tmp_path: Path) -> Iterator[tuple[DbosRuntime, Path, Path]]:
    """A runtime whose provider answers and leaves the pinned tree exactly as it was.

    The declared verification writes the directory it ran in into the record
    file, so whether it ever started is a fact on disk rather than a mock's
    memory: no record, no check.
    """

    yield from granted_runtime(
        tmp_path, VERIFICATION_EXIT_CODE, provider_changes_the_tree=False
    )


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


PYTEST_STYLE_VERIFICATION_TAIL = b"1 failed, 4 passed in 0.10s\n"
"""What a real `pytest -q` leaves as its last line, for the run this fixture drives.

Bare, with no `=` border: `-q` prints the verdict unbracketed, unlike a plain
run or a wider terminal, which is why the reader that finds this line
(`pytest_summary_line`) accepts both shapes rather than assuming a border.
"""

PYTEST_STYLE_VERIFICATION_COMMAND = [
    "/bin/sh",
    "-c",
    f"printf '%s' '{PYTEST_STYLE_VERIFICATION_TAIL.decode('ascii')}'; exit 1",
]

_OVERSIZED_SUMMARY_FILLER = "x" * (MAXIMUM_RECEIPTED_VERIFICATION_SUMMARY_BYTES + 200)
OVERSIZED_SUMMARY_VERIFICATION_TAIL = (
    f"1 failed, 1 passed in {_OVERSIZED_SUMMARY_FILLER}s\n"
).encode("ascii")
"""A verdict-shaped line a project's own test runner is free to compose this long.

`pytest_summary_line`'s grammar bounds the shape of a verdict, never its
length: anything after `in ` is free text, so this is exactly as real a
summary as a short one, just longer than one receipt sentence keeps.
"""

OVERSIZED_SUMMARY_VERIFICATION_COMMAND = [
    "/bin/sh",
    "-c",
    f"printf '%s' '{OVERSIZED_SUMMARY_VERIFICATION_TAIL.decode('ascii')}'; exit 1",
]


@pytest.fixture
def oversized_summary_failing_verification_runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, Path, Path]]:
    yield from granted_runtime(
        tmp_path,
        FAILED_VERIFICATION_EXIT_CODE,
        verification_command=OVERSIZED_SUMMARY_VERIFICATION_COMMAND,
    )


@pytest.fixture
def pytest_style_failing_verification_runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, Path, Path]]:
    yield from granted_runtime(
        tmp_path,
        FAILED_VERIFICATION_EXIT_CODE,
        verification_command=PYTEST_STYLE_VERIFICATION_COMMAND,
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


def wait_for_failed_run_after_node_completion(
    runtime: DbosRuntime,
    run_id: RunId,
    workflow: WorkflowRevision,
) -> None:
    wait_for_workflow_completion(
        node_workflow_id_for(
            NodeExecutionId.for_node(run_id, workflow.revision_hash, "implement")
        ),
        f"the implement node for run {run_id.value!r} to finish",
    )
    with runtime.engine.connect() as connection:
        observed = str(
            connection.scalar(
                sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
            )
        )
    assert observed == RunState.FAILED.value, f"run ended {observed!r}"


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
    wait_for_run_state(started_runtime.engine, RUN, RunState.COMPLETED)

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
    wait_for_run_state(started_runtime.engine, RUN, RunState.COMPLETED)

    forked = starter.fork_run(
        ForkRunRequest(RUN, "verification-is-not-an-effect", "implement")
    )

    assert isinstance(forked, DurableRunForkCreated)
    assert forked.fork.command_id == RunForkCommandId.for_request(
        RUN, "verification-is-not-an-effect"
    )


@pytest.mark.proves("a-nonzero-project-verification-fails-the-attempt-durably-named")
@pytest.mark.proves("a-rejected-attempts-own-diff-is-kept-as-a-readable-artifact")
def test_a_nonzero_project_verification_fails_the_attempt_and_leaves_no_success(
    failing_verification_runtime: tuple[DbosRuntime, Path, Path],
) -> None:
    """A granted check that exits 1 is a named failure, not a completed run.

    The provider's bytes were a success the schema admits. The project's own
    command then exited 1. That ending must not write the success rows a
    zero-exit grant writes: no agent receipt, no `AGENT_COMPLETED`, and no
    `tool_redemptions` row -- not because a failed attempt has nowhere to put
    one since V39, but because a check that exited 1 redeemed nothing. What
    remains is the named failure -- and, because a reader who sees no receipt
    must still be able to tell a builder that answered from one that did not,
    the schema revision and value hash of the answer the provider gave stand in
    that failure's own receipt (#1156), which is the second sentence this run
    proves.
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
    wait_for_failed_run_after_node_completion(started_runtime, FAILED_RUN, workflow)

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
    # The provider's own answer is kept with the refusal, judged by the schema
    # that admitted it: a red check says nothing about whether the builder
    # answered, and a reader who cannot see the answer cannot tell a broken
    # build from a builder that did nothing (#1156).
    assert schema_revision is not None
    assert value_hash == Sha256Hash.of(PROVIDER_OUTPUT)
    assert receipt_count == 0
    assert redemption_count == 0


@pytest.mark.proves("an-attempt-that-changed-nothing-ends-before-it-pays-for-a-check")
def test_an_attempt_that_left_the_pinned_tree_alone_ends_without_running_the_check(
    unchanged_tree_runtime: tuple[DbosRuntime, Path, Path],
) -> None:
    """The ending #1156 exists for: named in seconds, not after a whole test suite.

    Three live runs ended `PROJECT_VERIFICATION_FAILED` after ten minutes of a
    project's own tests, on a tree that was almost certainly the pin. That is
    two lies at once -- a verification that decided nothing, and an attempt
    whose real fact was that it did nothing. The record file is the proof the
    command never started: the declared verification writes the directory it
    ran in into it, so a file that does not exist is a check that did not run.
    """
    started_runtime, _scratch_root, cwd_record = unchanged_tree_runtime
    workflow, bindings, _grant_revision = publish_granted_node(started_runtime)

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(UNCHANGED_RUN, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    wait_for_failed_run_after_node_completion(started_runtime, UNCHANGED_RUN, workflow)

    with started_runtime.engine.connect() as connection:
        attempt = (
            connection.execute(
                sa.select(agent_attempts).where(
                    agent_attempts.c.run_id == UNCHANGED_RUN.value
                )
            )
            .mappings()
            .one()
        )
        payload = connection.scalar(
            sa.select(run_events.c.payload).where(
                run_events.c.run_id == UNCHANGED_RUN.value,
                run_events.c.event_kind == RunEventKind.AGENT_FAILED.value,
            )
        )
        stored_reason = connection.scalar(
            sa.select(node_receipts_v3.c.reason).where(
                node_receipts_v3.c.node_execution_id == attempt["node_execution_id"]
            )
        )
        redemption_count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(tool_redemptions)
            .where(tool_redemptions.c.run_id == UNCHANGED_RUN.value)
        )

    assert attempt["state"] == AgentAttemptState.FAILED.value
    assert attempt["failure_code"] == AgentAttemptFailureCode.CANDIDATE_UNCHANGED.value
    assert payload is not None
    assert bytes(payload) == (
        AgentAttemptFailureCode.CANDIDATE_UNCHANGED.value.encode("ascii")
    )
    words, _schema_revision, _value_hash = read_stored_node_receipt_reason(
        str(stored_reason)
    )
    token, _separator, verdict = words.partition(": ")
    assert token == NodeReceiptReason.CANDIDATE_UNCHANGED.value
    # What the builder said stands beside the tree that contradicts it.
    assert PROVIDER_OUTPUT.decode("ascii") in verdict
    assert not cwd_record.exists()
    assert redemption_count == 0


@pytest.mark.proves("a-red-verifications-output-is-kept-as-a-readable-artifact")
@pytest.mark.proves("a-rejected-attempts-own-diff-is-kept-as-a-readable-artifact")
def test_a_nonzero_project_verification_keeps_its_output_and_names_it_in_the_refusal(
    pytest_style_failing_verification_runtime: tuple[DbosRuntime, Path, Path],
) -> None:
    """The named failure #1137 closed: a reader learns *why* without rerunning it.

    `exit 1` alone answers nothing about whether a test broke or the
    environment did (#1137's own live pass). The receipt now also names the
    command, pytest's own summary line and the address of an artifact holding
    the check's full retained output -- reachable through the same
    `GET /artifacts/{hash}` door #1089 already opened, not a new wire concept.

    Beside it stands the other half of the same question (#1156): what the
    check said no *to*. Both addresses are read back from the same live
    receipt, because a sentence naming an artifact nobody fetched would prove
    the words and not the evidence.
    """
    started_runtime, _scratch_root, _cwd_record = (
        pytest_style_failing_verification_runtime
    )
    workflow, bindings, _grant_revision = publish_granted_node(started_runtime)
    run_id = RunId("v3/red-verify-names-its-evidence")

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    wait_for_failed_run_after_node_completion(started_runtime, run_id, workflow)

    with started_runtime.engine.connect() as connection:
        attempt = (
            connection.execute(
                sa.select(agent_attempts).where(agent_attempts.c.run_id == run_id.value)
            )
            .mappings()
            .one()
        )
        stored_reason = connection.scalar(
            sa.select(node_receipts_v3.c.reason).where(
                node_receipts_v3.c.node_execution_id == attempt["node_execution_id"]
            )
        )

    words, _schema_revision, _value_hash = read_stored_node_receipt_reason(
        str(stored_reason)
    )
    assert words.startswith(NodeReceiptReason.PROJECT_VERIFICATION_FAILED.value)
    assert f"exit {FAILED_VERIFICATION_EXIT_CODE}" in words
    assert " ".join(PYTEST_STYLE_VERIFICATION_COMMAND) in words
    assert re.search(r"after \d+ s", words) is not None, words
    assert "1 failed, 4 passed in 0.10s" in words

    artifacts = DbosArtifactStore(started_runtime.engine)
    artifact_match = re.search(r"output artifact sha256:([0-9a-f]{64})", words)
    assert artifact_match is not None, words
    artifact = artifacts.read_artifact(ArtifactHash(artifact_match.group(1)))
    assert isinstance(artifact, Artifact)
    assert artifact.content == PYTEST_STYLE_VERIFICATION_TAIL

    diff_match = re.search(r"candidate diff artifact sha256:([0-9a-f]{64})", words)
    assert diff_match is not None, words
    kept_diff = artifacts.read_artifact(ArtifactHash(diff_match.group(1)))
    assert isinstance(kept_diff, Artifact)
    patch = kept_diff.content.decode("utf-8")
    assert WHAT_THE_AGENT_MADE in patch
    assert MADE_BY_THE_AGENT.strip() in patch
    # Nothing this provider wrote has a credential shape, so the patch is kept
    # whole: a receipt claiming a redaction here would say the reader is looking
    # at something other than what the check refused.
    assert REDACTION_MARKER not in patch
    assert "candidate diff redacted" not in words


@pytest.mark.proves("a-red-verifications-output-is-kept-as-a-readable-artifact")
def test_a_summary_line_past_the_receipted_bound_is_kept_as_its_own_tail(
    oversized_summary_failing_verification_runtime: tuple[DbosRuntime, Path, Path],
) -> None:
    """The command's own summary can grow past what one receipt sentence keeps.

    Mirrors `ProcessExitSignature`'s standard-error bound (#1137): a receipt is
    a sentence an operator reads at a glance, not the project's own log, so a
    verdict-shaped line longer than the bound is kept as its own tail rather
    than grown into the reason without limit.
    """
    started_runtime, _scratch_root, _cwd_record = (
        oversized_summary_failing_verification_runtime
    )
    workflow, bindings, _grant_revision = publish_granted_node(started_runtime)
    run_id = RunId("v3/red-verify-oversized-summary")

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    wait_for_failed_run_after_node_completion(started_runtime, run_id, workflow)

    with started_runtime.engine.connect() as connection:
        attempt = (
            connection.execute(
                sa.select(agent_attempts).where(agent_attempts.c.run_id == run_id.value)
            )
            .mappings()
            .one()
        )
        stored_reason = connection.scalar(
            sa.select(node_receipts_v3.c.reason).where(
                node_receipts_v3.c.node_execution_id == attempt["node_execution_id"]
            )
        )

    words, _schema_revision, _value_hash = read_stored_node_receipt_reason(
        str(stored_reason)
    )
    summary_length = len(OVERSIZED_SUMMARY_VERIFICATION_TAIL.rstrip(b"\n"))
    bound_prefix = (
        f"last {MAXIMUM_RECEIPTED_VERIFICATION_SUMMARY_BYTES} of "
        f"{summary_length} summary bytes: "
    )
    assert bound_prefix in words
    kept_summary = words.split(bound_prefix, 1)[1].split(";", 1)[0]
    assert kept_summary.endswith("s")
    assert len(kept_summary) == MAXIMUM_RECEIPTED_VERIFICATION_SUMMARY_BYTES


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
    wait_for_failed_run_after_node_completion(started_runtime, TIMEOUT_RUN, workflow)

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
    wait_for_failed_run_after_node_completion(started_runtime, UNKEPT_RUN, workflow)

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
    wait_for_failed_run_after_node_completion(started_runtime, BOTH_LOST_RUN, workflow)

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
