from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
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
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.host import serving
from atelier2.host.serving import HostSettings
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentExecutorFactory,
    AgentExecutorFactoryV2,
    AgentProcessCompletion,
    AgentProcessInvocation,
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


class RuntimeCloser(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True)
class BrowserScratchRoot:
    path: Path
    created_by_harness: bool

    @classmethod
    def create(cls) -> BrowserScratchRoot:
        return cls(Path(tempfile.mkdtemp(prefix="atelier2-e2e-scratch-")), True)

    @classmethod
    def borrow(cls, path: Path) -> BrowserScratchRoot:
        return cls(path, False)

    def close(self) -> None:
        if self.created_by_harness:
            shutil.rmtree(self.path)


def close_runtime_and_scratch_root(
    runtime: RuntimeCloser | None, scratch_root: BrowserScratchRoot
) -> None:
    try:
        if runtime is not None:
            runtime.close()
    finally:
        scratch_root.close()


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

    @property
    def proves_absence(self) -> bool:
        return self._delegate.proves_absence

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
        self.observed = threading.Event()
        self.release = release

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        self.observed.set()
        if not self.release.wait(TIMEOUT_SECONDS):
            raise RuntimeError("browser did not observe the working attempt")
        return super().decode_process_completion(invocation, completion)


class BlockingAgentExecutorFactory(RecordingAgentExecutorFactoryV2):
    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        self.opened = BlockingAgentExecutor(
            self.output, [], self.lifecycle, self.provider, threading.Event()
        )
        return self.opened


class DelayedAgentExecutor(RecordingAgentExecutorV2):
    """Holds a V3 node in `working` long enough for the browser to draw it live."""

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        time.sleep(3.0)
        return super().decode_process_completion(invocation, completion)


class DelayedAgentExecutorFactory(RecordingAgentExecutorFactoryV2):
    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        self.opened = DelayedAgentExecutor(
            self.output, [], self.lifecycle, self.provider
        )
        return self.opened


class BrowserProofHarness:
    def __init__(
        self,
        app: ASGIApp,
        runtime: DbosRuntime,
        factory: BlockingAgentExecutorFactory,
        recompose: Callable[[], tuple[ASGIApp, DbosRuntime]],
        request_restart: Callable[[], None],
    ) -> None:
        self.app, self.runtime, self.factory = app, runtime, factory
        self.recompose = recompose
        self.request_restart = request_restart
        self.generation = 1
        self.expected_hash = hashlib.sha256(factory.output).hexdigest().encode("ascii")
        self.released = False
        self.start_response_observed = False
        self.run_response_counts: dict[str, int] = {}
        self.stream_counts: dict[str, int] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and path == "/__e2e/generation"
        ):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send(
                {
                    "type": "http.response.body",
                    "body": str(self.generation).encode("ascii"),
                }
            )
            return
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and path == "/__e2e/recompose"
        ):
            self.request_restart()
            await send(
                {
                    "type": "http.response.start",
                    "status": 202,
                    "headers": [(b"cache-control", b"no-store")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": str(self.generation + 1).encode("ascii"),
                }
            )
            return
        stream_number = 0
        if path.endswith("/events"):
            stream_number = self.stream_counts.get(path, 0) + 1
            self.stream_counts[path] = stream_number
        status = 0
        start_response_body = bytearray()

        async def proof_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body" and stream_number >= 2:
                body = message.get("body", b"").replace(self.expected_hash, b"0" * 64)
                message = {**message, "body": body}
            if (
                message["type"] == "http.response.body"
                and scope.get("method") == "POST"
                and path == "/atelier/api/v1/runs"
                and status in {200, 201}
            ):
                start_response_body.extend(message.get("body", b""))
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
                and not self.start_response_observed
                and scope["method"] == "POST"
                and path == "/atelier/api/v1/runs"
                and status in {200, 201}
            ):
                response = json.loads(start_response_body)
                bindings = response.get("agent_bindings")
                if not isinstance(bindings, list):
                    raise TypeError(
                        "the successful start did not return agent bindings"
                    )
                blocking_start = any(
                    isinstance(binding, dict)
                    and binding.get("provider_id") == self.factory.provider
                    and binding.get("executor_revision") == self.factory.revision
                    for binding in bindings
                )
                if blocking_start:
                    executor = self.factory.opened
                    if not isinstance(executor, BlockingAgentExecutor):
                        raise TypeError(
                            "the blocking start did not open its expected executor"
                        )
                    observed = await asyncio.to_thread(
                        executor.observed.wait, TIMEOUT_SECONDS
                    )
                    if not observed:
                        raise RuntimeError(
                            "the blocking start did not observe its process"
                        )
                    self.start_response_observed = True
            await send(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                self.release_after_observed(path, status)

        await self.app(scope, receive, proof_send)

    def release_after_observed(self, path: str, status: int) -> None:
        if (
            self.released
            or status != 200
            or not path.startswith("/atelier/api/v1/runs/")
        ):
            return
        self.run_response_counts[path] = self.run_response_counts.get(path, 0) + 1
        executor = self.factory.opened
        if executor is None or not executor.requests:
            return
        reference = encode_public_run_reference(executor.requests[0].run_id)
        if path != f"/atelier/api/v1/runs/{reference}":
            return
        if self.run_response_counts[path] < 3:
            return
        if not isinstance(executor, BlockingAgentExecutor):
            raise TypeError("the test executor changed while the browser observed it")
        with self.runtime.engine.connect() as connection:
            observed = connection.scalar(
                sa.select(sa.func.count())
                .select_from(agent_attempts)
                .where(agent_attempts.c.process_phase == "PROCESS_OBSERVED")
            )
        if not observed:
            raise RuntimeError("the blocking attempt was not durably observed")
        self.released = True
        executor.release.set()

    def close(self) -> None:
        self.runtime.close()

    def recompose_after_server_stop(self) -> None:
        self.runtime.close()
        self.app, self.runtime = self.recompose()
        self.generation += 1


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
        "e2e",
        "blocking/v1",
        "e2e-blocking-process",
        (
            "Provider terminal evidence:\n"
            + "Grüße 東京 — durable agent output remains readable after completion.\n"
            * 20
        ).encode(),
    )
    # The blocking provider exists so the browser can catch a V2 attempt
    # mid-flight. The immediate one finishes a V3 line without a hold. The
    # delayed one keeps a V3 node in `working` long enough for the graph
    # drawing to be photographed live.
    immediate = RecordingAgentExecutorFactoryV2(
        "e2e-v3", "immediate/v1", "e2e-immediate-process", b'"V3 provider bytes"'
    )
    delayed = DelayedAgentExecutorFactory(
        "e2e-v3-slow", "delayed/v1", "e2e-delayed-process", b"V3 provider bytes"
    )

    def runtime(
        settings: DbosRuntimeSettings,
        effect_factory: EffectAdapterFactory,
        agent_factory: AgentExecutorFactory,
        agent_factories_v2: tuple[AgentExecutorFactoryV2, ...],
    ) -> DbosRuntime:
        factories = (*agent_factories_v2, factory, immediate, delayed)
        # The e2e runtime root lives inside the repository checkout, which no
        # scratch root may, so the leased workspaces stand outside it.
        return DbosRuntime(
            replace(
                settings,
                agent_scratch_root=scratch_root.path,
            ),
            effect_factory,
            agent_factory,
            factories,
        )

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
        project_id=ProjectId("e2e-workshop"),
        project_root=Path(__file__).resolve().parents[2],
    )

    def compose() -> tuple[ASGIApp, DbosRuntime]:
        with patch.object(serving, "DbosRuntime", side_effect=runtime):
            return serving.compose_application(settings)

    restart_requested = threading.Event()
    server: uvicorn.Server | None = None

    def request_restart() -> None:
        restart_requested.set()
        if server is None:
            raise RuntimeError("the e2e server is not running")
        server.should_exit = True

    scratch_root = BrowserScratchRoot.create()
    runtime_to_close: RuntimeCloser | None = None
    try:
        app, live_runtime = compose()
        runtime_to_close = live_runtime
        harness = BrowserProofHarness(
            app, live_runtime, factory, compose, request_restart
        )
        runtime_to_close = harness
        while True:
            server = uvicorn.Server(
                uvicorn.Config(
                    harness,
                    host=settings.host,
                    port=settings.port,
                )
            )
            server.run()
            if not restart_requested.is_set():
                break
            restart_requested.clear()
            harness.recompose_after_server_stop()
    finally:
        close_runtime_and_scratch_root(runtime_to_close, scratch_root)


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
