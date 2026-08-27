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
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from unittest.mock import patch
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlalchemy as sa
import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.context import ApiContext
from atelier2.application.model_configuration import (
    ModelRegistryPublished,
    ModelRegistryUnchanged,
    ProjectModelDefaultsMissing,
    ProjectModelDefaultsPublished,
    ProjectModelDefaultsRead,
    ProjectModelDefaultsUnchanged,
)
from atelier2.application.publish_agent_configurations import (
    AgentConfigurationRevisionPublished,
    AgentConfigurationRevisionUnchanged,
    AuthProfileRevisionPublished,
    AuthProfileRevisionUnchanged,
)
from atelier2.application.publish_schema_revision import (
    SchemaPublicationCreated,
    SchemaPublicationExisting,
)
from atelier2.application.publish_workflow_revision import (
    PublicationCreated,
    PublicationExisting,
)
from atelier2.contracts.agents import (
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogAdmissionExisting,
    CatalogLineageFounded,
)
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
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.host import serving
from atelier2.host.conductor_workflow import (
    CONDUCTOR_BRIEF_SCHEMA,
    CONDUCTOR_REPORT_SCHEMA,
    conductor_workflow_document,
)
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
# The fake conductor's fixed episode report: valid against the production
# `CONDUCTOR_REPORT_SCHEMA`, so the browser proof sees exactly the reply a real
# doors-armed conductor would return -- same vector, unbilled.
CONDUCTOR_FAKE_ANSWER = "Nothing started: the workbench probe only asked for an answer."
CONDUCTOR_FAKE_REPORT = json.dumps(
    {"answer": CONDUCTOR_FAKE_ANSWER, "started_run_ids": []}
).encode()
CONDUCTOR_FAKE_PROVIDER = "e2e-conductor"
CONDUCTOR_FAKE_REVISION = "conductor-fake/v1"
# A held V3 attempt parks in `working` this long so the browser has ample margin
# to reach and confirm the cancel by keyboard; the operator's cancel ends it far
# sooner, so this only bounds a run nobody stops.
HELD_ATTEMPT_SECONDS = 30.0
# Long enough for the graph drawing to be photographed live, and interruptible
# by the generation that opened it so a recompose does not wait this bound out.
DELAYED_ATTEMPT_SECONDS = 3.0
MODEL_VALIDATION_RUN_ID = "provider-model-validation"


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


def replace_closed_generation_scratch_root(
    previous: BrowserScratchRoot,
) -> BrowserScratchRoot:
    """Blank root for the next generation; the previous one is removed.

    Call only after that generation's runtime has closed. Reusing the same
    root would hand leftover attempt directories to the next workspace owner,
    which reconciles them against a store that no longer has those attempts.
    """

    next_root = BrowserScratchRoot.create()
    try:
        previous.close()
    except BaseException:
        next_root.close()
        raise
    return next_root


def close_runtime_and_scratch_root(
    runtime: RuntimeCloser | None, scratch_root: BrowserScratchRoot
) -> None:
    if runtime is None:
        scratch_root.close()
        return
    try:
        runtime.close()
    except BaseException:
        # A failed shutdown may still have a live generation writing this
        # workspace; deleting it would drop that work on the floor.
        print(
            f"preserving scratch root {scratch_root.path}: runtime shutdown failed",
            file=sys.stderr,
        )
        raise
    scratch_root.close()


class FakeProviderHolds:
    """Tracks in-flight fake provider decodes across a served generation.

    Delayed, held, and blocking executors wait on a release signal instead of
    sleeping. Drain sets that signal and then blocks until every tracked
    decode has returned. DBOS shutdown only waits one second for workflows
    and then ThreadPoolExecutor.shutdown(wait=False), so closing the runtime
    or removing the scratch root before those decodes finish still races a
    live generation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._released = threading.Event()
        self._inflight = 0

    def current(self) -> threading.Event:
        return self._released

    @contextmanager
    def in_flight(self) -> Iterator[None]:
        with self._lock:
            self._inflight += 1
        try:
            yield
        finally:
            with self._lock:
                self._inflight -= 1
                self._idle.notify_all()

    def release_all(self) -> None:
        self._released.set()

    def wait_until_idle(self, timeout: float = TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"{self._inflight} in-flight fake decode(s) did not finish"
                    )
                self._idle.wait(remaining)

    def start_generation(self) -> None:
        with self._lock:
            if self._inflight:
                raise RuntimeError(
                    "cannot start a generation while "
                    f"{self._inflight} fake decode(s) are still in flight"
                )
            self._released = threading.Event()


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
        owner: BlockingAgentExecutorFactory,
    ) -> None:
        super().__init__(output, requests, lifecycle, name)
        self.observed = threading.Event()
        self.release = release
        self.owner = owner

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        if self.requests[-1].run_id.value == MODEL_VALIDATION_RUN_ID:
            return super().decode_process_completion(invocation, completion)
        self.owner.observed_executor = self
        tracking = (
            self.owner.holds.in_flight()
            if self.owner.holds is not None
            else nullcontext()
        )
        with tracking:
            self.observed.set()
            if not self.release.wait(TIMEOUT_SECONDS):
                raise RuntimeError("browser did not observe the working attempt")
            return super().decode_process_completion(invocation, completion)

    def close(self) -> None:
        super().close()
        if (
            self.requests
            and self.requests[-1].run_id.value == MODEL_VALIDATION_RUN_ID
            and self.owner.opened is self
        ):
            self.owner.opened = None


class BlockingAgentExecutorFactory(RecordingAgentExecutorFactoryV2):
    observed_executor: BlockingAgentExecutor | None = None

    def __init__(
        self,
        provider: str,
        revision: str,
        operational_identity_value: str,
        output: bytes,
        holds: FakeProviderHolds | None = None,
    ) -> None:
        super().__init__(provider, revision, operational_identity_value, output)
        self.holds = holds

    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        self.opened = BlockingAgentExecutor(
            self.output, [], self.lifecycle, self.provider, threading.Event(), self
        )
        return self.opened

    def release_in_flight(self) -> None:
        for executor in (self.observed_executor, self.opened):
            if executor is not None:
                executor.release.set()
        self.observed_executor = None
        type(self).observed_executor = None


def drain_inflight_fake_decodes(
    holds: FakeProviderHolds,
    blocking: BlockingAgentExecutorFactory | None = None,
) -> None:
    holds.release_all()
    if blocking is not None:
        blocking.release_in_flight()
    holds.wait_until_idle()


class DelayedAgentExecutor(RecordingAgentExecutorV2):
    """Holds a V3 node in `working` long enough for the browser to draw it live."""

    def __init__(
        self,
        output: bytes,
        requests: list[AgentExecutionRequestV2],
        lifecycle: list[str],
        name: str,
        holds: FakeProviderHolds,
    ) -> None:
        super().__init__(output, requests, lifecycle, name)
        self._holds = holds
        self._released = holds.current()
        self.holding = threading.Event()

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        if self.requests[-1].run_id.value == MODEL_VALIDATION_RUN_ID:
            return super().decode_process_completion(invocation, completion)
        with self._holds.in_flight():
            self.holding.set()
            self._released.wait(DELAYED_ATTEMPT_SECONDS)
            return super().decode_process_completion(invocation, completion)


class DelayedAgentExecutorFactory(RecordingAgentExecutorFactoryV2):
    def __init__(
        self,
        provider: str,
        revision: str,
        operational_identity_value: str,
        output: bytes,
        holds: FakeProviderHolds,
    ) -> None:
        super().__init__(provider, revision, operational_identity_value, output)
        self._holds = holds

    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        self.opened = DelayedAgentExecutor(
            self.output, [], self.lifecycle, self.provider, self._holds
        )
        return self.opened


class HeldAgentExecutor(RecordingAgentExecutorV2):
    """Holds a V3 node in `working` until an operator's cancel stops it.

    The browser needs a genuinely live V3 attempt to reach and confirm the cancel
    decision by keyboard (#439 P6). A short delay races the journey, so this one
    parks the node long enough for a person to open, confirm and watch the run
    stop -- bounded, so a forgotten run never hangs the server.
    """

    def __init__(
        self,
        output: bytes,
        requests: list[AgentExecutionRequestV2],
        lifecycle: list[str],
        name: str,
        holds: FakeProviderHolds,
    ) -> None:
        super().__init__(output, requests, lifecycle, name)
        self._holds = holds
        self._released = holds.current()
        self.holding = threading.Event()

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        if self.requests[-1].run_id.value == MODEL_VALIDATION_RUN_ID:
            return super().decode_process_completion(invocation, completion)
        with self._holds.in_flight():
            self.holding.set()
            self._released.wait(HELD_ATTEMPT_SECONDS)
            return super().decode_process_completion(invocation, completion)


class HeldAgentExecutorFactory(RecordingAgentExecutorFactoryV2):
    def __init__(
        self,
        provider: str,
        revision: str,
        operational_identity_value: str,
        output: bytes,
        holds: FakeProviderHolds,
    ) -> None:
        super().__init__(provider, revision, operational_identity_value, output)
        self._holds = holds

    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        self.opened = HeldAgentExecutor(
            self.output, [], self.lifecycle, self.provider, self._holds
        )
        return self.opened


def _published_schema_hash(result: object) -> str:
    match result:
        case SchemaPublicationCreated(revision) | SchemaPublicationExisting(revision):
            return revision.revision_hash.value
        case refused:
            raise RuntimeError(f"schema publication failed: {refused!r}")


class BrowserProofHarness:
    def __init__(
        self,
        app: ASGIApp,
        runtime: DbosRuntime,
        factory: BlockingAgentExecutorFactory,
        recompose: Callable[[], tuple[ASGIApp, DbosRuntime]],
        request_restart: Callable[[bool], None],
        reset_state: Callable[[], None],
        drain_inflight: Callable[[], None] | None = None,
    ) -> None:
        self.app, self.runtime, self.factory = app, runtime, factory
        self.recompose = recompose
        self.request_restart = request_restart
        self.reset_state = reset_state
        self.drain_inflight = drain_inflight or (lambda: None)
        self.generation = 1
        self.expected_hash = hashlib.sha256(factory.output).hexdigest().encode("ascii")
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
            and path == "/__e2e/release-blocking-attempt"
        ):
            released = await asyncio.to_thread(self.release_blocking_attempt)
            await send(
                {
                    "type": "http.response.start",
                    "status": 204 if released else 409,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and path == "/__e2e/seed-conductor"
        ):
            body = await asyncio.to_thread(self.seed_conductor)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and path == "/__e2e/recompose"
        ):
            # `?reset=true` additionally wipes and re-seeds durable state back to
            # the exact cold-boot baseline (#742): a spec that needs a guaranteed
            # unseeded server calls this itself instead of depending on file
            # listing order. Bare `/__e2e/recompose` keeps its original meaning --
            # a restart that a real redeploy's data survives -- unchanged, since
            # `cockpit.spec.ts` and `connection-restart.spec.ts` prove exactly
            # that.
            reset = (
                parse_qs(scope.get("query_string", b"").decode()).get(
                    "reset", ["false"]
                )[0]
                == "true"
            )
            self.request_restart(reset)
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

        async def proof_send(message: Message) -> None:
            if message["type"] == "http.response.body" and stream_number >= 2:
                body = message.get("body", b"").replace(self.expected_hash, b"0" * 64)
                message = {**message, "body": body}
            await send(message)

        await self.app(scope, receive, proof_send)

    def release_blocking_attempt(self) -> bool:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        executor = self.factory.observed_executor
        while executor is None:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
            executor = self.factory.observed_executor
        if not executor.observed.wait(max(0, deadline - time.monotonic())):
            return False
        executor.release.set()
        return True

    def close(self) -> None:
        self.runtime.close()

    def recompose_after_server_stop(self, reset: bool) -> None:
        self.drain_inflight()
        self.runtime.close()
        if reset:
            self.reset_state()
        self.app, self.runtime = self.recompose()
        self.generation += 1

    def seed_conductor(self) -> bytes:
        """Publish the whole conductor catalog through the production doors.

        Everything the workbench needs to see a connected conductor: the brief
        and report schemas, the production conductor document (built by its own
        owner, `atelier2.host.conductor_workflow`), its catalog lineage, an
        auth profile plus agent configuration bound to the fake conductor
        executor, and the project level-2 model default selecting that exact
        model. On demand rather than at startup, so one served
        instance proves BOTH workbench states: the honest refusal before this
        endpoint is called, the real episode after.
        """

        context: ApiContext = self.app.state.api_context  # type: ignore[attr-defined]
        use_cases = context.use_cases
        brief_hash = _published_schema_hash(
            use_cases.publish_schema_revision(CONDUCTOR_BRIEF_SCHEMA)
        )
        report_hash = _published_schema_hash(
            use_cases.publish_schema_revision(CONDUCTOR_REPORT_SCHEMA)
        )
        document = conductor_workflow_document(brief_hash, report_hash)
        match use_cases.publish_workflow_revision(document):
            case PublicationCreated(read) | PublicationExisting(read):
                revision = read.projection.revision
            case refused_publication:
                raise RuntimeError(
                    f"conductor publication failed: {refused_publication!r}"
                )
        match use_cases.found_catalog_lineage(
            RevisionKind.WORKFLOW,
            PublishedRevisionHash(revision.revision_hash.value),
            None,
            CatalogActor("e2e-harness"),
            CatalogActivatedAt("2026-01-01T00:00:00Z"),
        ):
            case CatalogLineageFounded(lineage) | CatalogAdmissionExisting(lineage):
                lineage_id = lineage.lineage_id.value
            case refused_admission:
                raise RuntimeError(f"conductor admission failed: {refused_admission!r}")

        match use_cases.publish_auth_profile_revision(
            "e2e-conductor-profile", 1, CONDUCTOR_FAKE_PROVIDER, "subscription"
        ):
            case AuthProfileRevisionPublished(profile) | AuthProfileRevisionUnchanged(
                profile
            ):
                auth_profile_hash = profile.revision_hash.value
            case refused_profile:
                raise RuntimeError(
                    f"conductor auth profile failed: {refused_profile!r}"
                )
        match use_cases.publish_agent_configuration_revision(
            "conductor-fake-model",
            auth_profile_hash,
            CONDUCTOR_FAKE_REVISION,
            AgentExecutionCapability.HEADLESS_WITH_TOOLS.value,
        ):
            case AgentConfigurationRevisionPublished(
                bound
            ) | AgentConfigurationRevisionUnchanged(bound):
                configuration_hash = bound.revision_hash.value
            case refused_configuration:
                raise RuntimeError(
                    f"conductor configuration failed: {refused_configuration!r}"
                )

        match use_cases.publish_model_registry(
            CONDUCTOR_FAKE_PROVIDER,
            1,
            (("conductor-fake-model", configuration_hash),),
        ):
            case ModelRegistryPublished(registry) | ModelRegistryUnchanged(registry):
                registry_hash = registry.revision_hash.value
            case refused_registry:
                raise RuntimeError(
                    f"conductor model registry failed: {refused_registry!r}"
                )
        match use_cases.validate_model_registry_entry(
            CONDUCTOR_FAKE_PROVIDER, configuration_hash
        ):
            case ModelRegistryPublished(registry) | ModelRegistryUnchanged(registry):
                registry_hash = registry.revision_hash.value
            case refused_validation:
                raise RuntimeError(
                    f"conductor model validation failed: {refused_validation!r}"
                )
        match use_cases.get_project_model_defaults("e2e-workshop"):
            case ProjectModelDefaultsRead(current_defaults):
                defaults_revision_number = current_defaults.revision_number + 1
                retained_defaults = tuple(
                    (
                        default.difficulty,
                        default.model_registry_revision_hash.value,
                        default.provider_id.value,
                        default.model_id,
                        default.agent_configuration_revision_hash.value,
                    )
                    for default in current_defaults.defaults
                    if default.difficulty != 2
                )
            case ProjectModelDefaultsMissing():
                defaults_revision_number = 1
                retained_defaults = ()
            case refused_defaults_read:
                raise RuntimeError(
                    f"conductor model defaults read failed: {refused_defaults_read!r}"
                )
        match use_cases.publish_project_model_defaults(
            "e2e-workshop",
            defaults_revision_number,
            retained_defaults
            + (
                (
                    2,
                    registry_hash,
                    CONDUCTOR_FAKE_PROVIDER,
                    "conductor-fake-model",
                    configuration_hash,
                ),
            ),
        ):
            case ProjectModelDefaultsPublished() | ProjectModelDefaultsUnchanged():
                pass
            case refused_defaults:
                raise RuntimeError(
                    f"conductor model defaults failed: {refused_defaults!r}"
                )
        return json.dumps(
            {
                "lineage_id": lineage_id,
                "workflow_revision_hash": revision.revision_hash.value,
                "configuration_hash": configuration_hash,
            }
        ).encode()


def seed_boot_baseline(database: Path, effects: Path, application_version: str) -> None:
    """(Re)creates the harness's cold-boot baseline against fresh database and
    effect-store files: the schema, and the two `RUN_IDS` runs already parked
    in `WAITING_RECONCILIATION` that `wait_for_reconciliation` and the Board's
    own "never empty" suite depend on. `main()` calls this once at process
    start; an `/__e2e/recompose?reset=true` (#742) calls it again after wiping
    both files, so a spec that needs a guaranteed-unseeded server reaches the
    exact same baseline a cold boot would give it, not an empty schema neither
    caller actually wants.
    """
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


def reset_to_boot_baseline(
    database: Path, effects: Path, application_version: str
) -> None:
    """Wipes both durable files (and their WAL/SHM sidecars) and reseeds them
    back to the exact cold-boot baseline (#742). A module-level function, not
    a closure inside `main()`, so it is independently callable and testable --
    `tests/e2e/test_serve_cockpit.py` drives it directly against a live,
    already-mutated harness rather than only through a real process restart.
    """
    for sqlite_path in (database, effects):
        sqlite_path.unlink(missing_ok=True)
        for sidecar_suffix in ("-wal", "-shm"):
            sqlite_path.with_name(sqlite_path.name + sidecar_suffix).unlink(
                missing_ok=True
            )
    seed_boot_baseline(database, effects, application_version)


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
    seed_boot_baseline(database, effects, application_version)

    holds = FakeProviderHolds()
    factory = BlockingAgentExecutorFactory(
        "e2e",
        "blocking/v1",
        "e2e-blocking-process",
        (
            "Provider terminal evidence:\n"
            + "Grüße 東京 — durable agent output remains readable after completion.\n"
            * 20
        ).encode(),
        holds,
    )
    # The blocking provider exists so the browser can catch a V2 attempt
    # mid-flight. The immediate one finishes a V3 line without a hold. The
    # delayed one keeps a V3 node in `working` long enough for the graph
    # drawing to be photographed live.
    immediate = RecordingAgentExecutorFactoryV2(
        "e2e-v3", "immediate/v1", "e2e-immediate-process", b'"V3 provider bytes"'
    )
    delayed = DelayedAgentExecutorFactory(
        "e2e-v3-slow",
        "delayed/v1",
        "e2e-delayed-process",
        b"V3 provider bytes",
        holds,
    )
    # Held long enough for the browser to stop it by hand (#439 P6 cancel proof).
    held = HeldAgentExecutorFactory(
        "e2e-v3-held", "held/v1", "e2e-held-process", b'"V3 provider bytes"', holds
    )
    # The workbench chat proof (#7): a doors-shaped executor answering with the
    # production report shape, so a browser can send a message and read the
    # conductor's reply without a billed call.
    conductor = RecordingAgentExecutorFactoryV2(
        CONDUCTOR_FAKE_PROVIDER,
        CONDUCTOR_FAKE_REVISION,
        "e2e-conductor-process",
        CONDUCTOR_FAKE_REPORT,
        capability_set=frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS}),
    )

    def runtime(
        settings: DbosRuntimeSettings,
        effect_factory: EffectAdapterFactory,
        agent_factory: AgentExecutorFactory,
        agent_factories_v2: tuple[AgentExecutorFactoryV2, ...],
    ) -> DbosRuntime:
        factories = (*agent_factories_v2, factory, immediate, delayed, held, conductor)
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
    reset_requested = threading.Event()
    server: uvicorn.Server | None = None

    def request_restart(reset: bool) -> None:
        if reset:
            reset_requested.set()
        restart_requested.set()
        if server is None:
            raise RuntimeError("the e2e server is not running")
        server.should_exit = True

    def drain_inflight() -> None:
        drain_inflight_fake_decodes(holds, factory)

    scratch_root = BrowserScratchRoot.create()

    def compose_next_generation() -> tuple[ASGIApp, DbosRuntime]:
        nonlocal scratch_root
        holds.start_generation()
        scratch_root = replace_closed_generation_scratch_root(scratch_root)
        return compose()

    runtime_to_close: RuntimeCloser | None = None
    try:
        app, live_runtime = compose()
        runtime_to_close = live_runtime
        harness = BrowserProofHarness(
            app,
            live_runtime,
            factory,
            compose_next_generation,
            request_restart,
            lambda: reset_to_boot_baseline(database, effects, application_version),
            drain_inflight,
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
            harness.recompose_after_server_stop(reset_requested.is_set())
            reset_requested.clear()
    finally:
        drain_inflight()
        close_runtime_and_scratch_root(runtime_to_close, scratch_root)


def wait_for_reconciliation(
    runtime: DbosRuntime, run_ids: tuple[str, ...] = RUN_IDS
) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    observed: dict[str, str] = {}
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = {
                str(row.run_id): str(row.state)
                for row in connection.execute(sa.select(runs.c.run_id, runs.c.state))
            }
        if all(
            observed.get(run_id) == RunState.WAITING_RECONCILIATION.value
            for run_id in run_ids
        ):
            return
        time.sleep(0.025)
    raise RuntimeError(f"e2e runs did not reach reconciliation: {observed!r}")


if __name__ == "__main__":
    main()
