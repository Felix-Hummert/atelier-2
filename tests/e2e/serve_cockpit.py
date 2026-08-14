from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlalchemy as sa

from atelier2 import host
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.start_run import start_run
from atelier2.contracts.agents import AgentExecutionRequestV2
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    EffectUnknownOutcome,
    PerformedEffect,
)
from atelier2.contracts.runs import RunId, RunState, StartRunRequest, WorkflowRevision
from atelier2.host import HostSettings
from atelier2.ports.agent_executions import AgentExecutorFactory, AgentProcessInvocation
from atelier2.ports.effects import EffectAdapter, EffectAdapterFactory
from tests.scenarios.agents import (
    SCENARIO_PROVIDER_FRAME_BYTES,
    RecordingAgentExecutorFactoryV2,
    RecordingAgentExecutorV2,
)

WORKFLOW = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: wait, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: wait}
  - {id: agent, type: agent, job: prove-reconciliation, output: exact-request, next: action}
"""
RUN_IDS = ("found-run", "absent-run")
TIMEOUT_SECONDS = 10.0


class UnknownReadbackAdapter:
    def __init__(self, delegate: EffectAdapter) -> None:
        self._delegate = delegate

    def readback(self, intent: EffectIntent) -> EffectReadback:
        return EffectUnknownOutcome(intent.reference)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        return self._delegate.execute(intent)

    def close(self) -> None:
        self._delegate.close()


class UnknownReadbackFactory:
    def __init__(self, delegate: LoopbackEffectAdapterFactory) -> None:
        self._delegate = delegate

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    def open(self) -> UnknownReadbackAdapter:
        return UnknownReadbackAdapter(self._delegate.open())


class BlockingAgentExecutor(RecordingAgentExecutorV2):
    def prepare_process(
        self, request: AgentExecutionRequestV2
    ) -> AgentProcessInvocation:
        self.requests.append(request)
        return AgentProcessInvocation(
            (sys.executable, "-c", "import threading; threading.Event().wait()"),
            Path.cwd(),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )


class BlockingAgentExecutorFactory(RecordingAgentExecutorFactoryV2):
    def open(self) -> RecordingAgentExecutorV2:
        self.opened = BlockingAgentExecutor(b"", [], self.lifecycle, self.provider)
        return self.opened


def main() -> None:
    root = Path(os.environ["ATELIER2_E2E_ROOT"]).resolve()
    if root.name != ".playwright-runtime":
        raise RuntimeError("refusing to clear an unexpected e2e runtime path")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    port = int(os.environ["ATELIER2_E2E_PORT"])
    database = root / "atelier.sqlite"
    effects = root / "effects.sqlite"
    application_version = "r3-phase5-e2e"
    binding = LoopbackEffectAdapterFactory(
        effects,
        AdapterRevision("loopback-v1"),
        EffectDestination("r3-phase5-e2e"),
    )
    prepare = DbosRuntime(
        DbosRuntimeSettings(database, application_version),
        UnknownReadbackFactory(binding),
        ExactOutputAgentExecutorFactory(),
    )
    try:
        prepare.initialize_storage()
        revision = WorkflowRevision(WORKFLOW)
        starter = DbosDurableRunStarter(prepare.engine, prepare.settings)
        for run_id in RUN_IDS:
            start_run(StartRunRequest(RunId(run_id), revision), starter)
        prepare.launch()
        wait_for_reconciliation(prepare)
    finally:
        prepare.close()

    factory = BlockingAgentExecutorFactory(
        "e2e", "blocking/v1", "e2e-blocking-process", b""
    )

    def runtime(
        settings: DbosRuntimeSettings,
        effect_factory: EffectAdapterFactory,
        agent_factory: AgentExecutorFactory,
    ) -> DbosRuntime:
        return DbosRuntime(settings, effect_factory, agent_factory, (factory,))

    with patch.object(host, "DbosRuntime", side_effect=runtime):
        host.serve(
            HostSettings(
                database_path=database,
                effect_store_path=effects,
                effect_adapter_revision="loopback-v1",
                effect_destination="r3-phase5-e2e",
                application_version=application_version,
                source_commit="r3-phase5-e2e",
                source_tree="r3-phase5-e2e",
                frontend_dist=Path(os.environ["ATELIER2_E2E_FRONTEND_DIST"]),
                port=port,
            )
        )


def wait_for_reconciliation(runtime: DbosRuntime) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    observed: dict[str, str] = {}
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = {
                str(row.run_id): str(row.state)
                for row in connection.execute(sa.select(runs.c.run_id, runs.c.state))
            }
        if observed == {
            run_id: RunState.WAITING_RECONCILIATION.value for run_id in RUN_IDS
        }:
            return
        time.sleep(0.025)
    raise RuntimeError(f"e2e runs did not reach reconciliation: {observed!r}")


if __name__ == "__main__":
    main()
