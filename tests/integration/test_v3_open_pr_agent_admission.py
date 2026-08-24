"""Admission refuses an agent `open-pr` grant when the adapter cannot prove absence.

`#430`/`#431` precondition: an agent-authored `open-pr` grant is redeemed after
its attempt has already durably succeeded, and its redemption has no Action-only
`WAITING_RECONCILIATION` resting place. A destination that cannot prove absence
(live GitHub answers `EffectUnknownOutcome` on a not-found readback) therefore
cannot safely carry it, so a run carrying such a grant is refused at admission --
before it advances -- rather than left to raise after it reported success. The
Action `open-pr` path, which does have `WAITING_RECONCILIATION`, is untouched, and
a non-proving adapter admits any run that carries no agent `open-pr` grant.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.github import (
    GitHubEffectAdapterFactory,
    GitHubRepository,
    GitHubTokenCredential,
    LiveGitHubEffectAdapterFactory,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.runs import RunId
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
