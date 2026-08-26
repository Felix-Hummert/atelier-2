"""Live-shaped adapters admit agent open-pr grants for reconciliation."""

from __future__ import annotations

from pathlib import Path

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.github import GitHubEffectAdapterFactory
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
)
from atelier2.contracts.runs import RunId
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.effects import EffectAdapter
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.open_pr_agent import (
    PR_SPEC,
    open_pr_agent_executor_factory,
    publish_open_pr_agent_run,
)


class _NonProvingFactory:
    def __init__(self, delegate: GitHubEffectAdapterFactory) -> None:
        self._delegate = delegate

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    @property
    def proves_absence(self) -> bool:
        return False

    def open(self) -> EffectAdapter:
        return self._delegate.open()


def test_an_agent_open_pr_grant_is_admitted_without_proving_absence(
    tmp_path: Path,
) -> None:
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "agent-open-pr-admission",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        _NonProvingFactory(
            GitHubEffectAdapterFactory(
                tmp_path / "github.sqlite",
                AdapterRevision("github-open-pr-v1"),
                EffectDestination("platform"),
            )
        ),
        ExactOutputAgentExecutorFactory(),
        (open_pr_agent_executor_factory(PR_SPEC),),
    )
    runtime.initialize_storage()
    try:
        workflow, bindings = publish_open_pr_agent_run(runtime, granted=True)
        result = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        ).start_published(
            StartPublishedRunRequestV2(
                RunId("v3/agent-open-pr-admitted"), workflow.revision_hash, bindings
            )
        )
    finally:
        runtime.close()

    assert isinstance(result, DurableRunCreated)
