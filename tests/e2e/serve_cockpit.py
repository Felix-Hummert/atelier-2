from __future__ import annotations

import hashlib
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlalchemy as sa
import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import agent_attempts, runs
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.references import encode_public_run_reference
from atelier2.contracts.agents import AgentExecutionRequestV2, AgentExecutionResult
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    EffectUnknownOutcome,
    PerformedEffect,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.host import serving
from atelier2.host.serving import HostSettings
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentExecutorFactory,
    AgentExecutorFactoryV2,
    AgentProcessCompletion,
)
from atelier2.ports.effects import EffectAdapter, EffectAdapterFactory
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    RecordingAgentExecutorV2,
)
from tests.scenarios.runs import start_published_v1_run

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
    def __init__(
        self,
        output: bytes,
        requests: list[AgentExecutionRequestV2],
        lifecycle: list[str],
        name: str,
        release: threading.Event,
    ) -> None:
        super().__init__(output, requests, lifecycle, name)
        self.release = release

    def decode_process_completion(
        self, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        if not self.release.wait(TIMEOUT_SECONDS):
            raise RuntimeError("browser did not observe the working attempt")
        return super().decode_process_completion(completion)


class BlockingAgentExecutorFactory(RecordingAgentExecutorFactoryV2):
    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        self.opened = BlockingAgentExecutor(
            self.output, [], self.lifecycle, self.provider, threading.Event()
        )
        return self.opened


class BrowserProofHarness:
    def __init__(
        self, app: ASGIApp, engine: sa.Engine, factory: BlockingAgentExecutorFactory
    ) -> None:
        self.app, self.engine, self.factory = app, engine, factory
        self.expected_hash = hashlib.sha256(factory.output).hexdigest().encode("ascii")
        self.released = False
        self.observed_run_responses = 0
        self.stream_counts: dict[str, int] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        stream_number = 0
        if path.endswith("/events"):
            stream_number = self.stream_counts.get(path, 0) + 1
            self.stream_counts[path] = stream_number
        status = 0

        async def proof_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body" and stream_number >= 2:
                body = message.get("body", b"").replace(self.expected_hash, b"0" * 64)
                message = {**message, "body": body}
            await send(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                self.release_after_observed(path, status)

        await self.app(scope, receive, proof_send)

    def release_after_observed(self, path: str, status: int) -> None:
        executor = self.factory.opened
        if self.released or status != 200 or executor is None or not executor.requests:
            return
        reference = encode_public_run_reference(executor.requests[0].run_id)
        if path != f"/atelier/api/v1/runs/{reference}":
            return
        with self.engine.connect() as connection:
            observed = connection.scalar(
                sa.select(sa.func.count())
                .select_from(agent_attempts)
                .where(agent_attempts.c.process_phase == "PROCESS_OBSERVED")
            )
        if observed:
            self.observed_run_responses += 1
            if self.observed_run_responses < 3:
                return
            self.released = True
            if not isinstance(executor, BlockingAgentExecutor):
                raise RuntimeError(
                    "the test executor changed while the browser observed it"
                )
            executor.release.set()


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
        for run_id in RUN_IDS:
            start_published_v1_run(
                prepare.engine, prepare.settings, RunId(run_id), revision
            )
        prepare.launch()
        wait_for_reconciliation(prepare)
    finally:
        prepare.close()

    factory = BlockingAgentExecutorFactory(
        "e2e", "blocking/v1", "e2e-blocking-process", "Grüße 東京".encode()
    )

    def runtime(
        settings: DbosRuntimeSettings,
        effect_factory: EffectAdapterFactory,
        agent_factory: AgentExecutorFactory,
        agent_factories_v2: tuple[AgentExecutorFactoryV2, ...],
    ) -> DbosRuntime:
        factories = (*agent_factories_v2, factory)
        return DbosRuntime(settings, effect_factory, agent_factory, factories)

    settings = HostSettings(
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
    with patch.object(serving, "DbosRuntime", side_effect=runtime):
        app, live_runtime = serving.compose_application(settings)
    try:
        uvicorn.Server(
            uvicorn.Config(
                BrowserProofHarness(app, live_runtime.engine, factory),
                host=settings.host,
                port=settings.port,
            )
        ).run()
    finally:
        live_runtime.close()


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
