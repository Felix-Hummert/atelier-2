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
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
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
from atelier2.contracts.node_records_v3 import (
    NodeReceiptReason,
    read_stored_node_receipt_reason,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    DurableRunFormatNotExecutable,
    StartPublishedRunRequestV2,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.projects import declaring_verification, git_project
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("v3/redeems-its-grant")
FAILED_RUN = RunId("v3/red-verify-fails")
PROVIDER_OUTPUT = b'"the exact provider bytes"'
VERIFICATION_OUTPUT = b"all green"
VERIFICATION_EXIT_CODE = 0
FAILED_VERIFICATION_EXIT_CODE = 1

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
    root: Path, record: Path, exit_code: int = VERIFICATION_EXIT_CODE
) -> Path:
    """A project whose manifest states the one command that verifies it.

    The command records where it was started and reads a file only its own commit
    carries, so after the lease is gone both facts are still measurable: which
    directory the verification ran in, and that the pinned tree stood in it.
    """
    script = (
        f"pwd > {record}; cat {COMMITTED_MARKER_NAME} >> {record}; "
        f"printf '{VERIFICATION_OUTPUT.decode('ascii')}'; "
        f"exit {exit_code}"
    )
    git_project(
        root,
        {
            **declaring_verification(["/bin/sh", "-c", script]),
            COMMITTED_MARKER_NAME: COMMITTED_MARKER,
        },
    )
    return root


def granted_runtime(
    tmp_path: Path, exit_code: int
) -> Iterator[tuple[DbosRuntime, Path, Path]]:
    """A runtime that can run an agent and redeem a grant against one project."""
    cwd_record = tmp_path / "verification-cwd.txt"
    project_root = project_declaring_its_verification(
        tmp_path / "project", cwd_record, exit_code
    )
    scratch_root = agent_scratch_root(tmp_path)
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-tool-grant-test",
            agent_scratch_root=scratch_root,
            project_root=project_root,
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
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


@pytest.mark.proves("a-nonzero-project-verification-fails-the-attempt-durably-named")
def test_a_nonzero_project_verification_fails_the_attempt_and_leaves_no_success(
    failing_verification_runtime: tuple[DbosRuntime, Path, Path],
) -> None:
    """A granted check that exits 1 is a named failure, not a completed run.

    The provider's bytes were a success the schema admits. The project's own
    command then exited 1. That ending must not write the success rows a
    zero-exit grant writes: no agent receipt, no `AGENT_COMPLETED`, no
    `tool_redemptions` row (its foreign key is an agent receipt a failed
    attempt does not have). What remains is the named failure.
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
