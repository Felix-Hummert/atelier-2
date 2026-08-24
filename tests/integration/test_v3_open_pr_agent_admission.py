"""Admission refuses an agent `open-pr` grant when the adapter cannot prove absence.

`#430`/`#431` precondition: an agent-authored `open-pr` grant is redeemed after
its attempt has already durably succeeded, and its redemption has no Action-only
`WAITING_RECONCILIATION` resting place. A destination that cannot prove absence
(live GitHub answers `EffectUnknownOutcome` on a not-found readback) therefore
cannot safely carry it, so a run carrying such a grant is refused at admission --
before it advances -- rather than left to raise after it reported success. The
Action `open-pr` path, which does have `WAITING_RECONCILIATION`, is untouched, and
a non-proving adapter admits any run that carries no agent `open-pr` grant.

Admission guards only the runs a live-GitHub instance itself starts. A run
admitted earlier under the absence-proving loopback adapter can still owe its
redemption when the operator restarts the same database with live GitHub, so
composing the live adapter also scans the V3 runs and refuses to start while any
still owes an agent `open-pr` redemption. A run owes it while it is non-terminal,
and still owes it in the crash window between committing COMPLETED and the sink
node's own workflow redeeming the grant -- a run whose node workflow has finished
that redemption, and a grant-free run, never block the start.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.dbos.workflow_ids import node_workflow_id_for
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.github import (
    GitHubEffectAdapterFactory,
    GitHubRepository,
    GitHubTokenCredential,
    LiveGitHubEffectAdapterFactory,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.host.serving import (
    LiveGitHubOpenPrRunPending,
    _refuse_pending_agent_open_pr_runs,
)
from atelier2.ports.durable_runs import (
    DurableAgentPlatformEffectUnreconcilable,
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.open_pr_agent import (
    PR_SPEC,
    open_pr_agent_executor_factory,
    publish_open_pr_agent_run,
)

RUN = RunId("v3/agent-open-pr-admission")
AGENT_OPEN_PR_NODE = "implement"


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-agent-open-pr-admission-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        GitHubEffectAdapterFactory(
            tmp_path / "github.sqlite",
            AdapterRevision("github-open-pr-v1"),
            EffectDestination("platform"),
        ),
        ExactOutputAgentExecutorFactory(),
        (open_pr_agent_executor_factory(PR_SPEC),),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def _start(
    runtime: DbosRuntime, run: RunId, *, granted: bool, proves_absence: bool
) -> object:
    workflow, bindings = publish_open_pr_agent_run(runtime, granted=granted)
    starter = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=proves_absence,
    )
    return starter.start_published(
        StartPublishedRunRequestV2(run, workflow.revision_hash, bindings)
    )


def _run_row_count(runtime: DbosRuntime, run: RunId) -> int:
    with runtime.engine.connect() as connection:
        return int(
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(runs)
                .where(runs.c.run_id == run.value)
            )
            or 0
        )


def test_an_agent_open_pr_grant_is_refused_against_a_non_absence_proving_adapter(
    runtime: DbosRuntime,
) -> None:
    result = _start(runtime, RUN, granted=True, proves_absence=False)

    assert result == DurableAgentPlatformEffectUnreconcilable(AGENT_OPEN_PR_NODE)


def test_the_refused_agent_open_pr_run_never_advances(runtime: DbosRuntime) -> None:
    _start(runtime, RUN, granted=True, proves_absence=False)

    assert _run_row_count(runtime, RUN) == 0


def test_an_agent_open_pr_grant_is_admitted_when_the_adapter_proves_absence(
    runtime: DbosRuntime,
) -> None:
    result = _start(runtime, RUN, granted=True, proves_absence=True)

    assert isinstance(result, DurableRunCreated)


def test_a_run_without_an_agent_open_pr_grant_is_admitted_by_a_non_proving_adapter(
    runtime: DbosRuntime,
) -> None:
    result = _start(runtime, RUN, granted=False, proves_absence=False)

    assert isinstance(result, DurableRunCreated)


def test_the_loopback_and_fake_github_adapters_prove_absence(tmp_path: Path) -> None:
    revision = AdapterRevision("effect-v1")
    destination = EffectDestination("local")
    loopback = LoopbackEffectAdapterFactory(
        tmp_path / "loopback.sqlite", revision, destination
    )
    fake_github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite", revision, destination
    )

    assert loopback.proves_absence is True
    assert fake_github.proves_absence is True


def test_the_live_github_adapter_cannot_prove_absence(tmp_path: Path) -> None:
    live = LiveGitHubEffectAdapterFactory(
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
        GitHubRepository("FlexOr2", "atelier-2", "main"),
        GitHubTokenCredential(tmp_path / "github-credential"),
    )

    assert live.proves_absence is False


def _complete_run(runtime: DbosRuntime, run: RunId) -> None:
    """Lift the run to its `COMPLETED` end without running the workflow behind it.

    The startup scan reads the run's own durable state, so a test that a finished
    run no longer blocks a live-GitHub start only needs that state to say so.
    """
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.update(runs)
            .where(runs.c.run_id == run.value)
            .values(state=RunState.COMPLETED.value, terminal_hash="a" * 64)
        )


def test_live_github_startup_refuses_a_still_pending_agent_open_pr_run(
    runtime: DbosRuntime,
) -> None:
    # The cross-restart edge (`#430`/`#431`): a run admitted under the absence-
    # proving loopback adapter carries an agent `open-pr` grant and is still
    # non-terminal when the operator restarts the same database with live GitHub.
    # Recovery would resume its redemption against an adapter that cannot prove
    # absence, so the start refuses it by name instead.
    _start(runtime, RUN, granted=True, proves_absence=True)

    with pytest.raises(LiveGitHubOpenPrRunPending) as refusal:
        _refuse_pending_agent_open_pr_runs(runtime)

    assert RUN.value in str(refusal.value)


def test_live_github_startup_serves_when_no_run_carries_an_agent_open_pr_grant(
    runtime: DbosRuntime,
) -> None:
    _start(runtime, RUN, granted=False, proves_absence=True)

    _refuse_pending_agent_open_pr_runs(runtime)


def test_live_github_startup_serves_when_the_agent_open_pr_run_has_finished(
    runtime: DbosRuntime,
) -> None:
    _start(runtime, RUN, granted=True, proves_absence=True)
    _complete_run(runtime, RUN)

    _refuse_pending_agent_open_pr_runs(runtime)


def _current_node_workflow_id(runtime: DbosRuntime, run: RunId) -> str:
    """The durable node workflow id the run's current node execution mints.

    Derived from the run's own durable row through the same
    `node_workflow_id_for` the runtime mints at `start_node`, so a status seeded
    under it names the exact workflow recovery would resume.
    """
    with runtime.engine.connect() as connection:
        row = (
            connection.execute(
                sa.select(
                    runs.c.revision_hash,
                    runs.c.current_node_id,
                    runs.c.current_round_ordinal,
                ).where(runs.c.run_id == run.value)
            )
            .mappings()
            .one()
        )
    return node_workflow_id_for(
        NodeExecutionId.for_node(
            run,
            WorkflowRevisionHash(str(row["revision_hash"])),
            str(row["current_node_id"]),
            int(row["current_round_ordinal"]),
        )
    )


def _seed_workflow_status(runtime: DbosRuntime, workflow_id: str, status: str) -> None:
    """Leave the node workflow in the durable status a crash or a finish leaves.

    The durable runtime owns this table; a test that wants to ask about a
    workflow left mid-flight by a crash cannot reach that status by running one.
    """
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO workflow_status "
                "(workflow_uuid, status, created_at, updated_at, priority) "
                "VALUES (:workflow_id, :status, 0, 0, 0)"
            ),
            {"workflow_id": workflow_id, "status": status},
        )


def _completed_run_with_redemption_workflow(
    runtime: DbosRuntime, run: RunId, workflow_status: str
) -> None:
    """A COMPLETED grant run whose sink-node redemption workflow is `workflow_status`.

    The shape a crash in the post-COMPLETED, pre-redemption window leaves: the
    sink node's workflow committed the run COMPLETED and only afterwards redeems
    the grant, so the run reports success while its node workflow is still
    recoverable. `workflow_status` is the durable difference between a redemption
    still owed (recoverable) and one already finished.
    """
    _start(runtime, run, granted=True, proves_absence=True)
    node_workflow_id = _current_node_workflow_id(runtime, run)
    _complete_run(runtime, run)
    _seed_workflow_status(runtime, node_workflow_id, workflow_status)


def test_live_github_startup_refuses_a_completed_run_before_its_redemption_runs(
    runtime: DbosRuntime,
) -> None:
    # The residual crash window (`#430`): a run committed COMPLETED and crashed
    # before the sink node's own workflow redeemed its agent `open-pr` grant, so
    # its node workflow is still recoverable. Recovery on a live-GitHub restart
    # would resume it and end the run ERROR after it had reported success, so the
    # start refuses it even though its run state is already terminal.
    _completed_run_with_redemption_workflow(runtime, RUN, "PENDING")

    with pytest.raises(LiveGitHubOpenPrRunPending) as refusal:
        _refuse_pending_agent_open_pr_runs(runtime)

    assert RUN.value in str(refusal.value)


def test_live_github_startup_serves_when_the_completed_redemption_workflow_finished(
    runtime: DbosRuntime,
) -> None:
    _completed_run_with_redemption_workflow(runtime, RUN, "SUCCESS")

    _refuse_pending_agent_open_pr_runs(runtime)
