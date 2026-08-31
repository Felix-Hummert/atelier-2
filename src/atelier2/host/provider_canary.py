"""Run the configured live provider vectors and leave bounded proof behind.

The served agent-configuration list is the deployment's answer about which
exact provider/executor/configuration vectors are startable now. This client
reads that list, resolves the matching admitted workflow, starts one fresh run
with the listed configuration hash, and polls the public run resource to a
terminal state. It owns no provider process and opens no store.

Each vector replaces one secret-free ``provider-probe-receipt/v1`` beneath the
operator's XDG state directory. The durable run remains the evidence owner; a
receipt carries only its identities and terminal hash, or a bounded problem
code. Replacement is a same-directory temporary file plus ``os.replace`` so a
reader sees either the previous complete receipt or the new complete receipt.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from pydantic import TypeAdapter, ValidationError

from atelier2.adapters.claude_subscription import (
    CLAUDE_ATELIER_DOORS_EXECUTOR_KEY,
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY,
)
from atelier2.adapters.codex_subscription import CODEX_SUBSCRIPTION_EXECUTOR_KEY
from atelier2.adapters.grok_subscription import (
    GROK_SUBSCRIPTION_EXECUTOR_KEY,
    GROK_WORKSPACE_TOOLS_EXECUTOR_KEY,
)
from atelier2.api.openapi import API_PREFIX
from atelier2.api.wire.resources import (
    AgentConfigurationRevisionListItemResource,
    AgentConfigurationRevisionPageResource,
    CatalogNameResolutionResource,
    HealthResource,
    RunResourceV3,
)
from atelier2.contracts.agents import AgentConfigurationRevisionHash
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.provider_probe_receipts import (
    ProviderProbeProblemCode,
    ProviderProbeReceipt,
    ProviderProbeResult,
    ProviderProbeVectorId,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.when import recorded_instant
from atelier2.host.address import ADDRESSABLE_SCHEMES, DEFAULT_SERVICE_URL
from atelier2.host.run_command import (
    AGENT_CONFIGURATION_PATH,
    JSON_MEDIA_TYPE,
    RUN_PATH,
    WORKFLOW_REVISION_PATH,
    AgentRoleBinding,
    start_request_body,
)

PROVIDER_CANARY_ROLE = "provider-canary"
PROVIDER_CANARY_RECEIPT_VALIDITY = timedelta(hours=26)
PROVIDER_CANARY_TERMINAL_TIMEOUT_SECONDS = 300.0
PROVIDER_CANARY_POLL_INTERVAL_SECONDS = 2.0
PROVIDER_CANARY_HTTP_TIMEOUT_SECONDS = 30.0
PROVIDER_CANARY_CONFIGURATION_PAGE_SIZE = 50
PROVIDER_CANARY_STATE_RELATIVE_PATH = Path("atelier2/provider-probes/live")

_MAXIMUM_PROBLEM_RESPONSE_BYTES = 4_096

_health_resource = TypeAdapter(HealthResource)
_configuration_page_resource = TypeAdapter(AgentConfigurationRevisionPageResource)
_catalog_name_resolution_resource = TypeAdapter(CatalogNameResolutionResource)
_run_resource = TypeAdapter[RunResourceV3](RunResourceV3)

_WORKFLOW_BY_EXECUTOR = {
    (
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
    ): "provider-canary-headless",
    (
        CODEX_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
        CODEX_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
    ): "provider-canary-headless",
    (
        GROK_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
        GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
    ): "provider-canary-headless",
    (
        CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY.provider_id.value,
        CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision.value,
    ): "provider-canary-workspace-tools",
    (
        GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.provider_id.value,
        GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision.value,
    ): "provider-canary-workspace-tools",
    (
        CLAUDE_ATELIER_DOORS_EXECUTOR_KEY.provider_id.value,
        CLAUDE_ATELIER_DOORS_EXECUTOR_KEY.executor_revision.value,
    ): "provider-canary-atelier-doors",
}


class ProviderCanaryServerUnavailable(RuntimeError):
    """The configured HTTP service could not answer at all."""


class ProviderCanaryHttpRefused(RuntimeError):
    """The service answered one typed refusal rather than the requested value."""

    def __init__(self, problem_code: str, detail: str = "") -> None:
        super().__init__(detail or problem_code)
        self.problem_code = problem_code


class ProviderCanaryAnswerUnreadable(RuntimeError):
    """An external answer did not satisfy the small shape this client reads."""


class ProviderCanaryRunTimedOut(RuntimeError):
    """A started provider canary did not reach terminal before its bound."""


class ProviderCanaryHttp(Protocol):
    def get(self, path: str) -> bytes: ...

    def post(
        self, path: str, body: bytes, *, media_type: str = JSON_MEDIA_TYPE
    ) -> bytes: ...


class ProviderCanaryClock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemProviderCanaryClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True, slots=True)
class ProviderCanarySettings:
    service_url: str
    workflow_directory: Path
    state_directory: Path
    terminal_timeout_seconds: float = PROVIDER_CANARY_TERMINAL_TIMEOUT_SECONDS
    poll_interval_seconds: float = PROVIDER_CANARY_POLL_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        address = urlsplit(self.service_url)
        if address.scheme not in ADDRESSABLE_SCHEMES or not address.netloc:
            raise ValueError(
                f"{self.service_url!r} is not the address of a served Atelier API"
            )
        if self.terminal_timeout_seconds <= 0:
            raise ValueError("provider canary terminal timeout must be positive")
        if self.poll_interval_seconds <= 0:
            raise ValueError("provider canary poll interval must be positive")


@dataclass(frozen=True, slots=True)
class ProviderCanaryFailure:
    vector: ProviderProbeVectorId
    problem_code: ProviderProbeProblemCode
    detail: str


@dataclass(frozen=True, slots=True)
class ProviderCanaryReport:
    attempted: int
    failures: tuple[ProviderCanaryFailure, ...]

    @property
    def failed(self) -> int:
        return len(self.failures)


@dataclass(frozen=True, slots=True)
class _CanaryVector:
    vector_id: ProviderProbeVectorId
    configuration_hash: AgentConfigurationRevisionHash
    workflow_name: str


class UrllibProviderCanaryHttp:
    """The narrow HTTP boundary used by the live command."""

    def __init__(
        self,
        service_url: str = DEFAULT_SERVICE_URL,
        timeout_seconds: float = PROVIDER_CANARY_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("provider canary HTTP timeout must be positive")
        self._api_url = service_url.rstrip("/") + API_PREFIX
        self._timeout_seconds = timeout_seconds

    def get(self, path: str) -> bytes:
        return self._request(Request(self._api_url + path, method="GET"))

    def post(
        self, path: str, body: bytes, *, media_type: str = JSON_MEDIA_TYPE
    ) -> bytes:
        return self._request(
            Request(
                self._api_url + path,
                data=body,
                method="POST",
                headers={"content-type": media_type, "accept": JSON_MEDIA_TYPE},
            )
        )

    def _request(self, request: Request) -> bytes:
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return response.read()
        except HTTPError as refused:
            document = refused.read(_MAXIMUM_PROBLEM_RESPONSE_BYTES + 1)
            problem_code, detail = _problem_answer(document, str(refused))
            raise ProviderCanaryHttpRefused(problem_code, detail) from refused
        except (URLError, TimeoutError, OSError) as unavailable:
            raise ProviderCanaryServerUnavailable(str(unavailable)) from unavailable


def default_provider_canary_state_directory(
    environment: Mapping[str, str] = os.environ,
) -> Path:
    state_home = environment.get("XDG_STATE_HOME")
    root = Path(state_home) if state_home else Path.home() / ".local/state"
    return root / PROVIDER_CANARY_STATE_RELATIVE_PATH


def execute_provider_canaries(
    settings: ProviderCanarySettings,
    *,
    http: ProviderCanaryHttp | None = None,
    clock: ProviderCanaryClock | None = None,
) -> ProviderCanaryReport:
    """Run each currently startable, known provider vector exactly once."""

    client = http or UrllibProviderCanaryHttp(settings.service_url)
    canary_clock = clock or SystemProviderCanaryClock()
    health = _decoded(_health_resource, client.get("/health"), "health")
    vectors = _configured_vectors(client)
    failures: list[ProviderCanaryFailure] = []
    for vector in vectors:
        failure = _execute_vector(settings, vector, health, client, canary_clock)
        if failure is not None:
            failures.append(failure)
    return ProviderCanaryReport(len(vectors), tuple(failures))


def _configured_vectors(client: ProviderCanaryHttp) -> tuple[_CanaryVector, ...]:
    vectors: list[_CanaryVector] = []
    after: str | None = None
    while True:
        query = urlencode(
            {
                "limit": PROVIDER_CANARY_CONFIGURATION_PAGE_SIZE,
                **({"after_revision_hash": after} if after is not None else {}),
            }
        )
        page = _decoded(
            _configuration_page_resource,
            client.get(f"{AGENT_CONFIGURATION_PATH}?{query}"),
            "agent-configuration page",
        )
        vectors.extend(
            vector
            for item in page.items
            if item.startable and (vector := _canary_vector(item)) is not None
        )
        after = page.next_after_revision_hash
        if after is None:
            return tuple(vectors)


def _canary_vector(
    configuration: AgentConfigurationRevisionListItemResource,
) -> _CanaryVector | None:
    workflow_name = _workflow_for(
        configuration.provider_id, configuration.executor_revision
    )
    if workflow_name is None:
        return None
    configuration_hash = AgentConfigurationRevisionHash(
        configuration.agent_configuration_revision_hash
    )
    kind = workflow_name.removeprefix("provider-canary-")
    return _CanaryVector(
        ProviderProbeVectorId(f"{kind}-{configuration_hash.value}"),
        configuration_hash,
        workflow_name,
    )


def _workflow_for(provider_id: str, executor_revision: str) -> str | None:
    return _WORKFLOW_BY_EXECUTOR.get((provider_id, executor_revision))


def _execute_vector(
    settings: ProviderCanarySettings,
    vector: _CanaryVector,
    health: HealthResource,
    client: ProviderCanaryHttp,
    clock: ProviderCanaryClock,
) -> ProviderCanaryFailure | None:
    workflow_document = (
        settings.workflow_directory / f"{vector.workflow_name}.yaml"
    ).read_bytes()
    workflow_hash = WorkflowRevisionHash.of(workflow_document)
    run_id = RunId(
        "provider-canary/"
        f"{vector.vector_id.value}/"
        f"{clock.now().astimezone(UTC).isoformat(timespec='microseconds')}"
    )
    try:
        resolved = _decoded(
            _catalog_name_resolution_resource,
            client.get(
                f"{WORKFLOW_REVISION_PATH}/by-name/"
                f"{quote(vector.workflow_name, safe='')}"
            ),
            "admitted workflow name",
        )
        if resolved.workflow_revision_hash != workflow_hash.value:
            raise ProviderCanaryAnswerUnreadable(
                f"the admitted {vector.workflow_name} revision does not identify "
                "the deployed workflow bytes"
            )
        binding = AgentRoleBinding(
            PROVIDER_CANARY_ROLE, vector.configuration_hash.value
        )
        started = _decoded(
            _run_resource,
            client.post(
                RUN_PATH,
                start_request_body(run_id.value, workflow_hash.value, (binding,)),
            ),
            "started run",
        )
        ended = _wait_for_terminal(
            client,
            started,
            clock,
            settings.terminal_timeout_seconds,
            settings.poll_interval_seconds,
        )
        if ended.state == "COMPLETED":
            assert ended.terminal_hash is not None
            receipt = _receipt(
                vector,
                workflow_hash,
                health.source_commit,
                run_id,
                clock,
                terminal_hash=Sha256Hash(ended.terminal_hash),
            )
            write_provider_canary_receipt_atomic(
                settings.state_directory / f"{vector.vector_id.value}.json", receipt
            )
            return None
        problem_code = ProviderProbeProblemCode(f"run-{ended.state.lower()}")
        detail = f"run {run_id.value} ended {ended.state}"
    except ProviderCanaryServerUnavailable as unavailable:
        problem_code = ProviderProbeProblemCode("server-unavailable")
        detail = str(unavailable)
    except ProviderCanaryRunTimedOut as timed_out:
        problem_code = ProviderProbeProblemCode("run-timeout")
        detail = str(timed_out)
    except ProviderCanaryHttpRefused as refused:
        problem_code = _bounded_problem_code(refused.problem_code)
        detail = str(refused)
    except ProviderCanaryAnswerUnreadable as unreadable:
        problem_code = ProviderProbeProblemCode("server-answer-unreadable")
        detail = str(unreadable)
    receipt = _receipt(
        vector,
        workflow_hash,
        health.source_commit,
        run_id,
        clock,
        problem_code=problem_code,
    )
    write_provider_canary_receipt_atomic(
        settings.state_directory / f"{vector.vector_id.value}.json", receipt
    )
    return ProviderCanaryFailure(vector.vector_id, problem_code, detail)


def _wait_for_terminal(
    client: ProviderCanaryHttp,
    started: RunResourceV3,
    clock: ProviderCanaryClock,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> RunResourceV3:
    deadline = clock.monotonic() + timeout_seconds
    current = started
    while current.state not in {"COMPLETED", "FAILED", "CANCELLED"}:
        if clock.monotonic() >= deadline:
            raise ProviderCanaryRunTimedOut(
                f"run {started.run_id} did not reach terminal within "
                f"{timeout_seconds:g} seconds"
            )
        current = _decoded(
            _run_resource,
            client.get(f"{RUN_PATH}/{started.public_run_reference}"),
            "run",
        )
        if current.state not in {"COMPLETED", "FAILED", "CANCELLED"}:
            clock.sleep(min(poll_interval_seconds, deadline - clock.monotonic()))
    if current.terminal_hash is None:
        raise ProviderCanaryAnswerUnreadable("a terminal run carries no terminal hash")
    return current


def _receipt(
    vector: _CanaryVector,
    workflow_hash: WorkflowRevisionHash,
    source_commit: str,
    run_id: RunId,
    clock: ProviderCanaryClock,
    *,
    terminal_hash: Sha256Hash | None = None,
    problem_code: ProviderProbeProblemCode | None = None,
) -> ProviderProbeReceipt:
    observed = clock.now().astimezone(UTC)
    return ProviderProbeReceipt(
        vector.vector_id,
        vector.configuration_hash,
        workflow_hash,
        source_commit,
        recorded_instant(observed),
        recorded_instant(observed + PROVIDER_CANARY_RECEIPT_VALIDITY),
        (
            ProviderProbeResult.SUCCEEDED
            if terminal_hash is not None
            else ProviderProbeResult.FAILED
        ),
        run_id,
        terminal_hash,
        problem_code,
    )


def write_provider_canary_receipt_atomic(
    destination: Path, receipt: ProviderProbeReceipt
) -> None:
    """Replace one receipt only after its complete bytes reach a sibling file."""

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(receipt.canonical_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _decoded[AnswerT](
    answer_type: TypeAdapter[AnswerT], document: bytes, name: str
) -> AnswerT:
    try:
        return answer_type.validate_json(document)
    except ValidationError as unreadable:
        raise ProviderCanaryAnswerUnreadable(
            f"the service did not answer {name}: {unreadable}"
        ) from unreadable


def _problem_answer(document: bytes, fallback: str) -> tuple[str, str]:
    if len(document) > _MAXIMUM_PROBLEM_RESPONSE_BYTES:
        return "http-refused", fallback
    try:
        decoded = json.loads(document)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "http-refused", fallback
    if not isinstance(decoded, dict):
        return "http-refused", fallback
    raw_type = decoded.get("type")
    detail = decoded.get("detail")
    problem_code = (
        raw_type.rsplit("/", 1)[-1]
        if isinstance(raw_type, str) and raw_type
        else "http-refused"
    )
    return problem_code, detail if isinstance(detail, str) else fallback


def _bounded_problem_code(raw: str) -> ProviderProbeProblemCode:
    try:
        return ProviderProbeProblemCode(raw)
    except (TypeError, ValueError):
        return ProviderProbeProblemCode("http-refused")
