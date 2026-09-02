"""Run the configured live provider vectors and leave bounded proof behind.

The served agent-configuration list is the deployment's answer about which
exact provider/executor/configuration vectors are structurally startable now
-- a factory registered, available, and declaring the capability, with no
live evidence asked at all. This client reads that list, resolves the
matching admitted workflow, starts one fresh run with the listed
configuration hash, and polls the public run resource to a terminal state. It
owns no provider process and opens no store.

Discovery is receipt-neutral: health, the bounded configuration list, and all
distinct admitted workflow names must resolve before any vector becomes an
attempt. Discovery refusal is visible through the process exit and journal and
leaves every last-known vector receipt byte-identical. Once one vector enters
execution, its outcome replaces that vector's secret-free
``provider-probe-receipt/v1`` beneath the operator's XDG state directory. The
durable run remains the evidence owner; a receipt carries only its identities
and terminal hash, or a bounded problem code. Replacement is a same-directory
temporary file plus ``os.replace`` so a reader sees either the previous
complete receipt or the new complete receipt.

The public start request has no separate ``idempotency_key``: its ``run_id`` is
the durable idempotency identity. Every trigger creates a timestamped identity,
so a deploy trigger may start another billed probe on the same day. This client
does not persist that identity before POST; a crash after the service accepts
the POST but before the receipt lands can therefore make the next trigger start
another billed run. Closing that gap requires persisting the planned ``run_id``
as the retry key before POST and replaying it until its outcome is receipted.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
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
    NodeDetailResource,
    RunResourceV3,
)
from atelier2.contracts.agent_transcripts import TranscriptEventKind
from atelier2.contracts.agents import AgentConfigurationRevisionHash
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.provider_probe_receipts import (
    _SOURCE_COMMIT as PROVIDER_PROBE_SOURCE_COMMIT_FORMAT,
)
from atelier2.contracts.provider_probe_receipts import (
    PROVIDER_CANARY_ATELIER_DOORS_WORKFLOW_NAME,
    PROVIDER_CANARY_HEADLESS_WORKFLOW_NAME,
    PROVIDER_CANARY_WORKSPACE_TOOLS_WORKFLOW_NAME,
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
PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES = 4
PROVIDER_CANARY_MAXIMUM_VECTORS = 50
PROVIDER_CANARY_DISCOVERY_TIMEOUT_SECONDS = 300.0
PROVIDER_CANARY_PROCESS_TIMEOUT_SECONDS = (
    PROVIDER_CANARY_DISCOVERY_TIMEOUT_SECONDS
    + PROVIDER_CANARY_MAXIMUM_VECTORS * PROVIDER_CANARY_TERMINAL_TIMEOUT_SECONDS
)
PROVIDER_CANARY_STATE_RELATIVE_PATH = Path("atelier2/provider-probes/live")

_MAXIMUM_PROBLEM_RESPONSE_BYTES = 4_096

_health_resource = TypeAdapter(HealthResource)
_configuration_page_resource = TypeAdapter(AgentConfigurationRevisionPageResource)
_catalog_name_resolution_resource = TypeAdapter(CatalogNameResolutionResource)
_run_resource = TypeAdapter[RunResourceV3](RunResourceV3)
_node_detail_resource = TypeAdapter[NodeDetailResource](NodeDetailResource)

# These executor keys choose the matching probe workflow only. The served
# structurally-startable configuration list remains the sole owner of which
# vectors run, using the same executor constants that the Serve composition
# registers.
_WORKFLOW_BY_EXECUTOR = {
    (
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_HEADLESS_WORKFLOW_NAME,
    (
        CODEX_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
        CODEX_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_HEADLESS_WORKFLOW_NAME,
    (
        GROK_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
        GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_HEADLESS_WORKFLOW_NAME,
    (
        CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY.provider_id.value,
        CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_WORKSPACE_TOOLS_WORKFLOW_NAME,
    (
        GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.provider_id.value,
        GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_WORKSPACE_TOOLS_WORKFLOW_NAME,
    (
        CLAUDE_ATELIER_DOORS_EXECUTOR_KEY.provider_id.value,
        CLAUDE_ATELIER_DOORS_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_ATELIER_DOORS_WORKFLOW_NAME,
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


class ProviderCanaryProcessTimedOut(RuntimeError):
    """The provider-canary process exhausted its total work budget."""


class ProviderCanaryDiscoveryFailed(RuntimeError):
    """No complete, nonempty startable provider-vector set was discovered."""


class ProviderCanaryWorkflowUnreadable(RuntimeError):
    """The deployed canary workflow bytes could not be read locally."""


class ProviderCanaryHttp(Protocol):
    def get(self, path: str, *, timeout_seconds: float) -> bytes: ...

    def post(
        self,
        path: str,
        body: bytes,
        *,
        timeout_seconds: float,
        media_type: str = JSON_MEDIA_TYPE,
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
    process_timeout_seconds: float = PROVIDER_CANARY_PROCESS_TIMEOUT_SECONDS

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
        if self.process_timeout_seconds <= 0:
            raise ValueError("provider canary process timeout must be positive")


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
    ) -> None:
        self._api_url = service_url.rstrip("/") + API_PREFIX

    def get(self, path: str, *, timeout_seconds: float) -> bytes:
        return self._request(
            Request(self._api_url + path, method="GET"),
            timeout_seconds=timeout_seconds,
        )

    def post(
        self,
        path: str,
        body: bytes,
        *,
        timeout_seconds: float,
        media_type: str = JSON_MEDIA_TYPE,
    ) -> bytes:
        return self._request(
            Request(
                self._api_url + path,
                data=body,
                method="POST",
                headers={"content-type": media_type, "accept": JSON_MEDIA_TYPE},
            ),
            timeout_seconds=timeout_seconds,
        )

    def _request(self, request: Request, *, timeout_seconds: float) -> bytes:
        if timeout_seconds <= 0:
            raise ValueError("provider canary HTTP timeout must be positive")
        try:
            with urlopen(
                request,
                timeout=min(PROVIDER_CANARY_HTTP_TIMEOUT_SECONDS, timeout_seconds),
            ) as response:
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


def _discovery_timeout() -> ProviderCanaryDiscoveryFailed:
    return ProviderCanaryDiscoveryFailed(
        "discovery-timeout: provider discovery exceeded its bounded deadline"
    )


def _process_timeout() -> ProviderCanaryProcessTimedOut:
    return ProviderCanaryProcessTimedOut(
        "provider canary exceeded its total process deadline"
    )


def _vector_timeout(
    vector: _CanaryVector, *, process_deadline: float, terminal_deadline: float
) -> Callable[[], RuntimeError]:
    if process_deadline <= terminal_deadline:
        return _process_timeout

    def timed_out() -> ProviderCanaryRunTimedOut:
        return ProviderCanaryRunTimedOut(
            f"provider vector {vector.vector_id.value} did not reach terminal "
            "within its configured deadline"
        )

    return timed_out


def _remaining_before_deadline(
    clock: ProviderCanaryClock,
    deadline: float,
    timeout_error: Callable[[], RuntimeError],
) -> float:
    remaining = deadline - clock.monotonic()
    if remaining <= 0:
        raise timeout_error()
    return remaining


def _raise_if_deadline_reached(
    clock: ProviderCanaryClock,
    deadline: float,
    timeout_error: Callable[[], RuntimeError],
) -> None:
    _remaining_before_deadline(clock, deadline, timeout_error)


def _get_before_deadline(
    client: ProviderCanaryHttp,
    path: str,
    *,
    clock: ProviderCanaryClock,
    deadline: float,
    timeout_error: Callable[[], RuntimeError],
) -> bytes:
    try:
        answer = client.get(
            path,
            timeout_seconds=_remaining_before_deadline(clock, deadline, timeout_error),
        )
    except (ProviderCanaryHttpRefused, ProviderCanaryServerUnavailable) as failure:
        if clock.monotonic() >= deadline:
            raise timeout_error() from failure
        raise
    _raise_if_deadline_reached(clock, deadline, timeout_error)
    return answer


def _post_before_deadline(
    client: ProviderCanaryHttp,
    path: str,
    body: bytes,
    *,
    clock: ProviderCanaryClock,
    deadline: float,
    timeout_error: Callable[[], RuntimeError],
) -> bytes:
    try:
        answer = client.post(
            path,
            body,
            timeout_seconds=_remaining_before_deadline(clock, deadline, timeout_error),
        )
    except (ProviderCanaryHttpRefused, ProviderCanaryServerUnavailable) as failure:
        if clock.monotonic() >= deadline:
            raise timeout_error() from failure
        raise
    _raise_if_deadline_reached(clock, deadline, timeout_error)
    return answer


def execute_provider_canaries(
    settings: ProviderCanarySettings,
    *,
    http: ProviderCanaryHttp | None = None,
    clock: ProviderCanaryClock | None = None,
) -> ProviderCanaryReport:
    """Run each currently startable, known provider vector exactly once."""

    client = http or UrllibProviderCanaryHttp(settings.service_url)
    canary_clock = clock or SystemProviderCanaryClock()
    started_at = canary_clock.monotonic()
    process_deadline = started_at + settings.process_timeout_seconds
    discovery_deadline = min(
        process_deadline, started_at + PROVIDER_CANARY_DISCOVERY_TIMEOUT_SECONDS
    )
    try:
        discovery_timeout = _discovery_timeout
        health = _decoded(
            _health_resource,
            _get_before_deadline(
                client,
                "/health",
                clock=canary_clock,
                deadline=discovery_deadline,
                timeout_error=discovery_timeout,
            ),
            "health",
        )
        if PROVIDER_PROBE_SOURCE_COMMIT_FORMAT.fullmatch(health.source_commit) is None:
            raise ProviderCanaryAnswerUnreadable(
                "the service health source commit cannot identify receipt provenance"
            )
        vectors = _configured_vectors(
            client, clock=canary_clock, deadline=discovery_deadline
        )
        if not vectors:
            raise ProviderCanaryDiscoveryFailed(
                "no-startable-provider-vectors: "
                "the service listed no startable provider vectors"
            )
        admitted_workflows = _resolve_admitted_workflows(
            vectors,
            client,
            clock=canary_clock,
            deadline=discovery_deadline,
        )
        _raise_if_deadline_reached(canary_clock, discovery_deadline, discovery_timeout)
    except ProviderCanaryDiscoveryFailed:
        raise
    except ProviderCanaryServerUnavailable as unavailable:
        raise ProviderCanaryDiscoveryFailed(
            f"server-unavailable: {unavailable}"
        ) from unavailable
    except ProviderCanaryHttpRefused as refused:
        raise ProviderCanaryDiscoveryFailed(
            f"{_bounded_problem_code(refused.problem_code).value}: {refused}"
        ) from refused
    except ProviderCanaryAnswerUnreadable as unreadable:
        raise ProviderCanaryDiscoveryFailed(
            f"server-answer-unreadable: {unreadable}"
        ) from unreadable
    failures: list[ProviderCanaryFailure] = []
    for vector in vectors:
        _raise_if_deadline_reached(canary_clock, process_deadline, _process_timeout)
        failure = _execute_vector(
            settings,
            vector,
            admitted_workflows[vector.workflow_name],
            health,
            client,
            canary_clock,
            process_deadline,
        )
        if failure is not None:
            failures.append(failure)
    return ProviderCanaryReport(len(vectors), tuple(failures))


def _configured_vectors(
    client: ProviderCanaryHttp,
    *,
    clock: ProviderCanaryClock,
    deadline: float,
) -> tuple[_CanaryVector, ...]:
    vectors: list[_CanaryVector] = []
    after: str | None = None
    for page_number in range(1, PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES + 1):
        query = urlencode(
            {
                "limit": PROVIDER_CANARY_CONFIGURATION_PAGE_SIZE,
                **({"after_revision_hash": after} if after is not None else {}),
            }
        )
        page = _decoded(
            _configuration_page_resource,
            _get_before_deadline(
                client,
                f"{AGENT_CONFIGURATION_PATH}?{query}",
                clock=clock,
                deadline=deadline,
                timeout_error=_discovery_timeout,
            ),
            "agent-configuration page",
        )
        # `structurally_startable`, not `startable`: discovery must find a
        # vector whose only problem is the receipt this very run would write.
        # Selecting on `startable` here would be the same surface-vs-start-door
        # divergence this gate exists to close, running the other way -- the
        # listing saying "not startable" while the one run that could produce
        # the missing evidence is refused from ever finding it.
        vectors.extend(
            vector
            for item in page.items
            if item.structurally_startable
            and (vector := _canary_vector(item)) is not None
        )
        if len(vectors) > PROVIDER_CANARY_MAXIMUM_VECTORS:
            raise ProviderCanaryDiscoveryFailed(
                "too-many-provider-vectors: the service listed more than "
                f"{PROVIDER_CANARY_MAXIMUM_VECTORS} known startable provider vectors"
            )
        after = page.next_after_revision_hash
        if after is None:
            return tuple(vectors)
        if page_number == PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES:
            raise ProviderCanaryDiscoveryFailed(
                "too-many-configuration-pages: the service requires more than "
                f"{PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES} configuration pages"
            )
    raise AssertionError("bounded configuration pagination did not terminate")


def _resolve_admitted_workflows(
    vectors: tuple[_CanaryVector, ...],
    client: ProviderCanaryHttp,
    *,
    clock: ProviderCanaryClock,
    deadline: float,
) -> dict[str, WorkflowRevisionHash]:
    admitted: dict[str, WorkflowRevisionHash] = {}
    for workflow_name in dict.fromkeys(vector.workflow_name for vector in vectors):
        resolved = _decoded(
            _catalog_name_resolution_resource,
            _get_before_deadline(
                client,
                f"{WORKFLOW_REVISION_PATH}/by-name/{quote(workflow_name, safe='')}",
                clock=clock,
                deadline=deadline,
                timeout_error=_discovery_timeout,
            ),
            "admitted workflow name",
        )
        admitted[workflow_name] = WorkflowRevisionHash(resolved.workflow_revision_hash)
    return admitted


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
    admitted_workflow_hash: WorkflowRevisionHash,
    health: HealthResource,
    client: ProviderCanaryHttp,
    clock: ProviderCanaryClock,
    process_deadline: float,
) -> ProviderCanaryFailure | None:
    vector_started_at = clock.monotonic()
    terminal_deadline = vector_started_at + settings.terminal_timeout_seconds
    deadline = min(process_deadline, terminal_deadline)
    timeout_error = _vector_timeout(
        vector, process_deadline=process_deadline, terminal_deadline=terminal_deadline
    )
    workflow_hash = admitted_workflow_hash
    run_id = _run_id(vector.vector_id, clock.now().astimezone(UTC))
    workflow_path = settings.workflow_directory / f"{vector.workflow_name}.yaml"
    try:
        _raise_if_deadline_reached(clock, deadline, timeout_error)
        try:
            workflow_document = workflow_path.read_bytes()
        except OSError as unreadable:
            raise ProviderCanaryWorkflowUnreadable(
                f"could not read {workflow_path}: {unreadable}"
            ) from unreadable
        _raise_if_deadline_reached(clock, deadline, timeout_error)
        workflow_hash = WorkflowRevisionHash.of(workflow_document)
        if admitted_workflow_hash != workflow_hash:
            raise ProviderCanaryAnswerUnreadable(
                f"the admitted {vector.workflow_name} revision does not identify "
                "the deployed workflow bytes"
            )
        binding = AgentRoleBinding(
            PROVIDER_CANARY_ROLE, vector.configuration_hash.value
        )
        started = _decoded(
            _run_resource,
            _post_before_deadline(
                client,
                RUN_PATH,
                # The shared public-run owner emits StartRunRequestResourceV2:
                # bindings are present and these workflows declare no orders.
                start_request_body(run_id.value, workflow_hash.value, (binding,)),
                clock=clock,
                deadline=deadline,
                timeout_error=timeout_error,
            ),
            "started run",
        )
        ended = _wait_for_terminal(
            client,
            started,
            clock,
            deadline,
            settings.poll_interval_seconds,
            timeout_error,
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
        problem_code = (
            _failed_run_problem_code(
                client, started, ended, clock, deadline, timeout_error
            )
            if ended.state == "FAILED"
            else ProviderProbeProblemCode(f"run-{ended.state.lower()}")
        )
        detail = f"run {run_id.value} ended {ended.state}"
    except ProviderCanaryServerUnavailable as unavailable:
        problem_code = ProviderProbeProblemCode("server-unavailable")
        detail = str(unavailable)
    except ProviderCanaryRunTimedOut as timed_out:
        problem_code = ProviderProbeProblemCode("run-timeout")
        detail = str(timed_out)
    except ProviderCanaryProcessTimedOut as timed_out:
        problem_code = ProviderProbeProblemCode("process-timeout")
        detail = str(timed_out)
    except ProviderCanaryHttpRefused as refused:
        problem_code = _bounded_problem_code(refused.problem_code)
        detail = str(refused)
    except ProviderCanaryAnswerUnreadable as unreadable:
        problem_code = ProviderProbeProblemCode("server-answer-unreadable")
        detail = str(unreadable)
    except ProviderCanaryWorkflowUnreadable as unreadable:
        problem_code = ProviderProbeProblemCode("workflow-unreadable")
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


def _run_id(vector: ProviderProbeVectorId, observed: datetime) -> RunId:
    return RunId(
        "provider-canary/"
        f"{vector.value}/"
        f"{observed.astimezone(UTC).isoformat(timespec='microseconds')}"
    )


def _wait_for_terminal(
    client: ProviderCanaryHttp,
    started: RunResourceV3,
    clock: ProviderCanaryClock,
    deadline: float,
    poll_interval_seconds: float,
    timeout_error: Callable[[], RuntimeError],
) -> RunResourceV3:
    current = started
    while current.state not in {"COMPLETED", "FAILED", "CANCELLED"}:
        _raise_if_deadline_reached(clock, deadline, timeout_error)
        current = _decoded(
            _run_resource,
            _get_before_deadline(
                client,
                f"{RUN_PATH}/{started.public_run_reference}",
                clock=clock,
                deadline=deadline,
                timeout_error=timeout_error,
            ),
            "run",
        )
        if current.state not in {"COMPLETED", "FAILED", "CANCELLED"}:
            clock.sleep(
                min(
                    poll_interval_seconds,
                    _remaining_before_deadline(clock, deadline, timeout_error),
                )
            )
            _raise_if_deadline_reached(clock, deadline, timeout_error)
    if current.terminal_hash is None:
        raise ProviderCanaryAnswerUnreadable("a terminal run carries no terminal hash")
    return current


def _failed_run_problem_code(
    client: ProviderCanaryHttp,
    started: RunResourceV3,
    ended: RunResourceV3,
    clock: ProviderCanaryClock,
    deadline: float,
    timeout_error: Callable[[], RuntimeError],
) -> ProviderProbeProblemCode:
    """`provider-refused` where the failed node's own transcript named one.

    A run's own terminal state carries no more than `FAILED`; telling a
    provider that read and refused a call apart from a genuinely broken vector
    needs the node's own transcript (#1029), so a rate limit does not read as a
    defect. Reading that transcript is itself a live call this classification
    alone should not fail over: any refusal to answer it just keeps the plain
    `run-failed` code the caller already had.
    """

    try:
        detail = _decoded(
            _node_detail_resource,
            _get_before_deadline(
                client,
                f"{RUN_PATH}/{started.public_run_reference}/nodes/"
                f"{ended.current_node_id}",
                clock=clock,
                deadline=deadline,
                timeout_error=timeout_error,
            ),
            "failed node detail",
        )
    except (
        ProviderCanaryHttpRefused,
        ProviderCanaryServerUnavailable,
        ProviderCanaryAnswerUnreadable,
    ):
        return ProviderProbeProblemCode("run-failed")
    if detail.transcript is not None and any(
        event.event == TranscriptEventKind.PROVIDER_TERMINAL_REFUSAL
        for event in detail.transcript.events
    ):
        return ProviderProbeProblemCode("provider-refused")
    return ProviderProbeProblemCode("run-failed")


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
