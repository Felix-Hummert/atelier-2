from __future__ import annotations

import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from atelier2.api.problems import problem_resource
from atelier2.api.wire.resources import (
    CatalogNameResolutionResource,
    HealthResource,
    NodeDetailResource,
    NodeRailResource,
    RunCancellabilityResource,
    RunResourceV3,
)
from atelier2.contracts.agents import AgentConfigurationRevisionHash
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.provider_probe_receipts import (
    ProviderProbeProblemCode,
    ProviderProbeReceipt,
    ProviderProbeReceiptRefused,
    ProviderProbeResult,
    ProviderProbeVectorId,
    read_provider_probe_receipt,
)
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt
from atelier2.host import main
from atelier2.host.provider_canary import (
    PROVIDER_CANARY_DISCOVERY_TIMEOUT_SECONDS,
    PROVIDER_CANARY_HEALTH_WAIT_POLL_INTERVAL_SECONDS,
    PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES,
    PROVIDER_CANARY_MAXIMUM_VECTORS,
    ProviderCanaryDiscoveryFailed,
    ProviderCanaryHttpRefused,
    ProviderCanaryProcessTimedOut,
    ProviderCanaryServerUnavailable,
    ProviderCanarySettings,
    execute_provider_canaries,
    write_provider_canary_receipt_atomic,
)

SOURCE_COMMIT = "a" * 40
CONFIGURATION_HASH = "b" * 64
TERMINAL_HASH = "c" * 64
PUBLIC_RUN_REFERENCE = "run1.cHJvdmlkZXItY2FuYXJ5"


@dataclass
class FakeClock:
    instant: datetime = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    elapsed: float = 0.0

    def now(self) -> datetime:
        return self.instant + timedelta(seconds=self.elapsed)

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


@dataclass
class FakeHttp:
    configurations: tuple[dict[str, object], ...]
    workflow_directory: Path
    terminal_state: str = "COMPLETED"
    terminal_after_reads: int = 1
    refusal: str | None = None
    poll_refusal: str | None = None
    server_down: bool = False
    configuration_answer: bytes | None = None
    configuration_unavailable: bool = False
    configuration_refusal: str | None = None
    catalog_unavailable: bool = False
    health_unavailable: bool = False
    health_refusal: str | None = None
    health_answer: bytes | None = None
    health_answers_before_serving: list[bytes | Exception] = field(default_factory=list)
    health_source_commit: str = SOURCE_COMMIT
    configuration_pages: tuple[bytes, ...] | None = None
    node_detail_transcript_events: tuple[dict[str, object], ...] | None = None
    calls: list[tuple[str, str, bytes | None]] = field(default_factory=list)
    workflow_hashes: list[str] = field(default_factory=list)
    run_reads: int = 0
    configuration_page_reads: int = 0
    catalog_hashes: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.catalog_hashes = {
            name: Sha256Hash.of(
                (self.workflow_directory / f"{name}.yaml").read_bytes()
            ).value
            for name in (
                "provider-canary-headless",
                "provider-canary-workspace-tools",
                "provider-canary-atelier-doors",
            )
        }

    def get(self, path: str, *, timeout_seconds: float) -> bytes:
        assert timeout_seconds > 0
        self.calls.append(("GET", path, None))
        if path == "/health":
            if self.health_answers_before_serving:
                answer = self.health_answers_before_serving.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            if self.health_unavailable:
                raise ProviderCanaryServerUnavailable("health timed out")
            if self.health_refusal is not None:
                raise ProviderCanaryHttpRefused(self.health_refusal, "health refused")
            if self.health_answer is not None:
                return self.health_answer
            return (
                HealthResource(
                    status="serving",
                    source_commit=self.health_source_commit,
                    source_tree="d" * 40,
                    serve_started_at="2026-08-31T08:00:00Z",
                )
                .model_dump_json()
                .encode()
            )
        if path.startswith("/agent-configuration-revisions"):
            if self.configuration_unavailable:
                raise ProviderCanaryServerUnavailable("configuration listing timed out")
            if self.configuration_refusal is not None:
                raise ProviderCanaryHttpRefused(
                    self.configuration_refusal, "configuration listing refused"
                )
            if self.configuration_answer is not None:
                return self.configuration_answer
            if self.configuration_pages is not None:
                page = self.configuration_pages[self.configuration_page_reads]
                self.configuration_page_reads += 1
                return page
            return encoded(items=self.configurations, next_after_revision_hash=None)
        if path.startswith("/workflow-revisions/by-name/"):
            if self.catalog_unavailable:
                raise ProviderCanaryServerUnavailable("workflow catalog timed out")
            name = path.rsplit("/", 1)[-1]
            workflow_hash = self.catalog_hashes[name]
            self.workflow_hashes.append(workflow_hash)
            return (
                CatalogNameResolutionResource(
                    display_name=name,
                    lineage_id="4" * 64,
                    workflow_revision_hash=workflow_hash,
                    revision_number=1,
                )
                .model_dump_json()
                .encode()
            )
        if "/nodes/" in path:
            return node_detail_resource(
                node_id=path.rsplit("/", 1)[-1],
                transcript_events=self.node_detail_transcript_events,
            )
        if self.poll_refusal is not None:
            raise ProviderCanaryHttpRefused(self.poll_refusal, "poll refused")
        self.run_reads += 1
        state = (
            "STARTED"
            if self.run_reads < self.terminal_after_reads
            else self.terminal_state
        )
        return (
            run_resource("provider-canary-run", self.workflow_hashes[-1], state)
            .model_dump_json()
            .encode()
        )

    def post(
        self,
        path: str,
        body: bytes,
        *,
        timeout_seconds: float,
        media_type: str = "application/json",
    ) -> bytes:
        assert timeout_seconds > 0
        del media_type
        self.calls.append(("POST", path, body))
        if path == "/runs":
            if self.server_down:
                raise ProviderCanaryServerUnavailable("connection refused")
            if self.refusal is not None:
                raise ProviderCanaryHttpRefused(self.refusal, "start refused")
            request = json.loads(body)
            return (
                run_resource(
                    request["run_id"], request["workflow_revision_hash"], "STARTED"
                )
                .model_dump_json()
                .encode()
            )
        raise AssertionError(path)


def encoded(**document: object) -> bytes:
    return json.dumps(document).encode()


def run_resource(run_id: str, workflow_hash: str, state: str) -> RunResourceV3:
    terminal = state in {"COMPLETED", "FAILED", "CANCELLED"}
    rail_state = {
        "COMPLETED": NodeState.SUCCEEDED,
        "FAILED": NodeState.FAILED,
        "CANCELLED": NodeState.CANCELLED,
    }.get(state, NodeState.WORKING)
    return RunResourceV3.model_validate(
        {
            "workflow_format_version": 3,
            "run_id": run_id,
            "public_run_reference": PUBLIC_RUN_REFERENCE,
            "workflow_revision_hash": workflow_hash,
            "workflow_name": "provider canary probe",
            "agent_binding_set_hash": "1" * 64,
            "run_configuration_revision_hash": "2" * 64,
            "agent_bindings": (),
            "orders": (),
            "state_version": 1 if terminal else 0,
            "state": state,
            "current_node_id": "probe",
            "current_node_execution_id": "3" * 64,
            "node_rail": (
                NodeRailResource.model_validate(
                    {"node_id": "probe", "state": rail_state.value, "attempt": None}
                ),
            ),
            "cancellation": RunCancellabilityResource(
                cancellable=False,
                reason="already-ended" if terminal else "between-nodes",
                target_node_execution_id=None,
            ),
            "terminal_hash": TERMINAL_HASH if terminal else None,
            "latest_event_cursor": None,
        }
    )


def node_detail_resource(
    *, node_id: str, transcript_events: tuple[dict[str, object], ...] | None
) -> bytes:
    return (
        NodeDetailResource.model_validate(
            {
                "run_id": "5" * 64,
                "public_run_reference": PUBLIC_RUN_REFERENCE,
                "node_id": node_id,
                "state": NodeState.FAILED.value,
                "job_base64": None,
                "job_hash": None,
                "answer": None,
                "provenance": None,
                "refusal": "process-exited-unsuccessfully",
                "started_at": None,
                "ended_at": None,
                "transcript": (
                    None if transcript_events is None else {"events": transcript_events}
                ),
            }
        )
        .model_dump_json()
        .encode()
    )


def configuration(
    *,
    provider: str = "anthropic",
    executor: str = "claude-subscription/v1",
    capability: str = "headless",
    configuration_hash: str = CONFIGURATION_HASH,
    structurally_startable: bool = True,
    model_registered: bool = True,
    has_valid_receipt: bool = True,
) -> dict[str, object]:
    """A listed item shaped exactly as the server's own precedence computes it.

    Mirrors `AgentConfigurationRevisionListItem`'s fixed order: an
    unavailable executor refuses first, then a registry the cast would
    refuse (a superseded revision), then only a missing live receipt --
    `startable` never needs its own knob, it is `not_startable_reason is None`.
    """

    not_startable_reason = (
        "agent-executor-binding-unavailable"
        if not structurally_startable
        else "model-not-registered"
        if not model_registered
        else "provider-probe-receipt-missing"
        if not has_valid_receipt
        else None
    )
    return {
        "model": "provider-model",
        "auth_profile_revision_hash": "e" * 64,
        "executor_revision": executor,
        "provider_id": provider,
        "auth_mode": "subscription",
        "requested_capability": capability,
        "agent_configuration_revision_hash": configuration_hash,
        "startable": not_startable_reason is None,
        "structurally_startable": structurally_startable,
        "not_startable_reason": not_startable_reason,
    }


@pytest.fixture
def workflow_directory(tmp_path: Path) -> Path:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    for name in ("headless", "workspace-tools", "atelier-doors"):
        (workflows / f"provider-canary-{name}.yaml").write_text(
            f"format_version: 3\nname: provider-canary-{name}\n",
            encoding="utf-8",
        )
    return workflows


def settings(workflow_directory: Path, state_directory: Path) -> ProviderCanarySettings:
    return ProviderCanarySettings(
        service_url="http://127.0.0.1:8422",
        workflow_directory=workflow_directory,
        state_directory=state_directory,
        terminal_timeout_seconds=5,
        poll_interval_seconds=1,
    )


def read_receipts(state_directory: Path) -> tuple[ProviderProbeReceipt, ...]:
    receipts: list[ProviderProbeReceipt] = []
    for path in sorted(state_directory.glob("*.json")):
        verdict = read_provider_probe_receipt(path.read_bytes())
        assert not isinstance(verdict, ProviderProbeReceiptRefused)
        receipts.append(verdict)
    return tuple(receipts)


def successful_receipt() -> ProviderProbeReceipt:
    return ProviderProbeReceipt(
        vector=ProviderProbeVectorId(f"headless-{CONFIGURATION_HASH}"),
        configuration_hash=AgentConfigurationRevisionHash(CONFIGURATION_HASH),
        workflow_hash=WorkflowRevisionHash("d" * 64),
        source_commit=SOURCE_COMMIT,
        observed_at=RecordedAt("2026-08-31T08:00:00Z"),
        valid_until=RecordedAt("2026-09-01T10:00:00Z"),
        result=ProviderProbeResult.SUCCEEDED,
        run_reference=RunId("provider-canary/previous-run"),
        terminal_hash=Sha256Hash(TERMINAL_HASH),
    )


def write_live_success(state_directory: Path) -> Path:
    destination = state_directory / f"headless-{CONFIGURATION_HASH}.json"
    write_provider_canary_receipt_atomic(destination, successful_receipt())
    return destination


def test_provider_canary_is_an_operator_cli_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as ended:
        main(["provider-canary", "--help"])

    assert ended.value.code == 0
    assert "provider-probe-receipt/v1" in capsys.readouterr().out


def test_each_configured_vector_starts_exactly_one_matching_workflow_and_receipts_it(
    tmp_path: Path, workflow_directory: Path
) -> None:
    http = FakeHttp(
        (
            configuration(),
            configuration(
                provider="xai",
                executor="grok-subscription-tools/v1",
                capability="headless_with_tools",
                configuration_hash="f" * 64,
            ),
        ),
        workflow_directory,
    )
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    start_bodies = [
        body for _method, path, body in http.calls if path == "/runs" and body
    ]
    starts = [json.loads(body) for body in start_bodies]
    resolved = [
        path
        for method, path, _body in http.calls
        if method == "GET" and path.startswith("/workflow-revisions/by-name/")
    ]
    assert len(starts) == len(resolved) == 2
    assert starts[0] == {
        "workflow_format_version": 2,
        "run_id": starts[0]["run_id"],
        "workflow_revision_hash": http.workflow_hashes[0],
        "agent_bindings": [
            {
                "role": "provider-canary",
                "agent_configuration_revision_hash": CONFIGURATION_HASH,
            }
        ],
    }
    assert [start["agent_bindings"] for start in starts] == [
        [
            {
                "role": "provider-canary",
                "agent_configuration_revision_hash": CONFIGURATION_HASH,
            }
        ],
        [
            {
                "role": "provider-canary",
                "agent_configuration_revision_hash": "f" * 64,
            }
        ],
    ]
    assert resolved == [
        "/workflow-revisions/by-name/provider-canary-headless",
        "/workflow-revisions/by-name/provider-canary-workspace-tools",
    ]
    first_start_index = next(
        index
        for index, (method, path, _body) in enumerate(http.calls)
        if method == "POST" and path == "/runs"
    )
    assert all(
        index < first_start_index
        for index, (method, path, _body) in enumerate(http.calls)
        if method == "GET" and path.startswith("/workflow-revisions/by-name/")
    )
    assert report.failed == 0
    receipts = read_receipts(state_directory)
    assert len(receipts) == 2
    assert {receipt.result for receipt in receipts} == {ProviderProbeResult.SUCCEEDED}
    assert {
        receipt.terminal_hash.value for receipt in receipts if receipt.terminal_hash
    } == {TERMINAL_HASH}
    assert {receipt.source_commit for receipt in receipts} == {SOURCE_COMMIT}
    assert {receipt.valid_until.value for receipt in receipts} == {
        "2026-09-02T10:00:00Z"
    }


@pytest.mark.parametrize(
    "non_serving_answer",
    (
        pytest.param(
            ProviderCanaryServerUnavailable("connection refused"),
            id="connection-failure",
        ),
        pytest.param(
            encoded(
                status="starting", source_commit=SOURCE_COMMIT, source_tree="d" * 40
            ),
            id="non-serving-status",
        ),
        pytest.param(b"not-json", id="unreadable-body"),
    ),
)
def test_a_canary_retries_a_non_serving_health_answer_before_it_tries_any_vector(
    tmp_path: Path,
    workflow_directory: Path,
    non_serving_answer: bytes | Exception,
) -> None:
    state_directory = tmp_path / "state"
    http = FakeHttp((configuration(),), workflow_directory)
    http.health_answers_before_serving = [non_serving_answer]
    clock = FakeClock()

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=clock
    )

    assert report.attempted == 1
    assert report.failed == 0
    health_call_indexes = [
        index
        for index, (_method, path, _body) in enumerate(http.calls)
        if path == "/health"
    ]
    start_call_index = next(
        index
        for index, (method, path, _body) in enumerate(http.calls)
        if method == "POST" and path == "/runs"
    )
    assert len(health_call_indexes) == 2
    assert max(health_call_indexes) < start_call_index
    assert clock.elapsed >= PROVIDER_CANARY_HEALTH_WAIT_POLL_INTERVAL_SECONDS


def test_health_that_never_turns_serving_fails_loud_once_with_no_vector_attempted(
    tmp_path: Path, workflow_directory: Path
) -> None:
    state_directory = tmp_path / "state"
    http = FakeHttp((configuration(),), workflow_directory)
    http.health_unavailable = True

    with pytest.raises(
        ProviderCanaryDiscoveryFailed,
        match="health-wait-timeout.*health timed out",
    ):
        execute_provider_canaries(
            settings(workflow_directory, state_directory), http=http, clock=FakeClock()
        )

    assert not any(method == "POST" for method, _path, _body in http.calls)
    assert all(path == "/health" for _method, path, _body in http.calls)


def test_discovery_reprobes_every_registered_vector_whose_receipt_is_foreign(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """The self-healing property #942 claimed, restored under the amended rule.

    A redeploy invalidates every receipt by `source_commit`: the listing
    answers `startable: false` for every genuinely registered vector, exactly
    as it would the morning after -- but its reason is
    `provider-probe-receipt-missing`, not a superseded registry pointer, so
    discovery still finds it, still starts it through the exemption, and
    still writes it a fresh receipt.
    """

    http = FakeHttp(
        (
            configuration(has_valid_receipt=False),
            configuration(
                provider="xai",
                executor="grok-subscription-tools/v1",
                capability="headless_with_tools",
                configuration_hash="f" * 64,
                has_valid_receipt=False,
            ),
        ),
        workflow_directory,
    )
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    start_bodies = [
        body for _method, path, body in http.calls if path == "/runs" and body
    ]
    assert len(start_bodies) == 2
    assert report.attempted == 2
    assert report.failed == 0
    receipts = read_receipts(state_directory)
    assert len(receipts) == 2
    assert {receipt.result for receipt in receipts} == {ProviderProbeResult.SUCCEEDED}


def test_discovery_excludes_a_superseded_configuration_by_its_own_reason(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """A structurally startable but superseded revision is not a vector.

    The listing's own `not_startable_reason` names it `model-not-registered`
    -- computed by the same cast lookup a start makes -- not
    `provider-probe-receipt-missing`, so discovery excludes it honestly
    rather than offering it as though a fresh probe would fix it.
    """

    superseded_hash = "1" * 64
    http = FakeHttp(
        (
            configuration(configuration_hash=superseded_hash, model_registered=False),
            configuration(),
        ),
        workflow_directory,
    )
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.attempted == 1
    assert report.failed == 0
    (receipt,) = read_receipts(state_directory)
    assert receipt.configuration_hash == AgentConfigurationRevisionHash(
        CONFIGURATION_HASH
    )


def test_the_vector_cap_applies_after_filtering_not_before(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """51 raw candidates, one superseded (`startable: false`): 50 attempts.

    The cap is checked against the filtered vector count, never the raw
    listing count -- a listing this size with exactly one superseded
    configuration must attempt every genuine vector, not fail loud over a
    limit it never actually reached.
    """

    first_page = tuple(
        configuration(configuration_hash=f"{number:064x}")
        for number in range(PROVIDER_CANARY_MAXIMUM_VECTORS)
    )
    superseded = configuration(configuration_hash="f" * 64, model_registered=False)
    http = FakeHttp((*first_page, superseded), workflow_directory)
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.attempted == PROVIDER_CANARY_MAXIMUM_VECTORS
    assert report.failed == 0


@pytest.mark.parametrize(
    ("failure_mode", "expected_problem"),
    (
        ("refused", "agent-executor-binding-unavailable"),
        ("malformed", "server-answer-unreadable"),
        ("unavailable", "server-unavailable"),
    ),
)
def test_a_failed_startability_read_fails_closed_and_reads_once(
    tmp_path: Path,
    workflow_directory: Path,
    failure_mode: str,
    expected_problem: str,
) -> None:
    """The listing's own `startable` field is the sole judgment source.

    A refusal, a malformed answer, or an unreachable service each abort the
    whole run loud before any vector is admitted -- no POST /runs, every
    live receipt untouched -- and the listing is read exactly once, never
    retried.
    """

    state_directory = tmp_path / "state"
    destination = write_live_success(state_directory)
    previous = destination.read_bytes()
    http = FakeHttp((configuration(),), workflow_directory)
    if failure_mode == "refused":
        http.configuration_refusal = "agent-executor-binding-unavailable"
    elif failure_mode == "malformed":
        http.configuration_answer = b"not-json"
    else:
        http.configuration_unavailable = True

    with pytest.raises(ProviderCanaryDiscoveryFailed, match=expected_problem):
        execute_provider_canaries(
            settings(workflow_directory, state_directory), http=http, clock=FakeClock()
        )

    assert destination.read_bytes() == previous
    assert not any(method == "POST" for method, _path, _body in http.calls)
    assert (
        sum(
            1
            for method, path, _body in http.calls
            if method == "GET" and path.startswith("/agent-configuration-revisions")
        )
        == 1
    )


def test_a_timed_out_startability_read_fails_closed_before_any_start(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """A discovery deadline exceeded while reading the listing still fails
    the whole run loud, before any vector is admitted."""

    state_directory = tmp_path / "state"
    destination = write_live_success(state_directory)
    previous = destination.read_bytes()
    delegate = FakeHttp((configuration(),), workflow_directory)
    clock = FakeClock()

    class SlowConfigurationHttp:
        def get(self, path: str, *, timeout_seconds: float) -> bytes:
            answer = delegate.get(path, timeout_seconds=timeout_seconds)
            if path.startswith("/agent-configuration-revisions"):
                clock.elapsed += PROVIDER_CANARY_DISCOVERY_TIMEOUT_SECONDS + 1
            return answer

        def post(
            self,
            path: str,
            body: bytes,
            *,
            timeout_seconds: float,
            media_type: str = "application/json",
        ) -> bytes:
            raise AssertionError(
                "no start should be attempted after a timed-out startability read"
            )

    with pytest.raises(ProviderCanaryDiscoveryFailed, match="discovery-timeout"):
        execute_provider_canaries(
            settings(workflow_directory, state_directory),
            http=SlowConfigurationHttp(),
            clock=clock,
        )

    assert destination.read_bytes() == previous
    assert not any(method == "POST" for method, _path, _body in delegate.calls)


def test_discovery_finds_nothing_for_a_vector_the_service_marks_not_startable(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """`startable: false` refuses discovery -- the canary asks the live
    service's own judgment and derives nothing of its own from it."""

    http = FakeHttp(
        (configuration(structurally_startable=False),),
        workflow_directory,
    )
    state_directory = tmp_path / "state"

    with pytest.raises(ProviderCanaryDiscoveryFailed, match="no-startable"):
        execute_provider_canaries(
            settings(workflow_directory, state_directory), http=http, clock=FakeClock()
        )

    assert read_receipts(state_directory) == ()


def test_each_trigger_starts_a_new_run_even_on_the_same_day(
    tmp_path: Path, workflow_directory: Path
) -> None:
    http = FakeHttp((configuration(),), workflow_directory)
    clock = FakeClock()
    canary_settings = settings(workflow_directory, tmp_path / "state")

    execute_provider_canaries(canary_settings, http=http, clock=clock)
    clock.instant += timedelta(minutes=1)
    execute_provider_canaries(canary_settings, http=http, clock=clock)

    starts = [
        json.loads(body)
        for _method, path, body in http.calls
        if path == "/runs" and body is not None
    ]
    assert len(starts) == 2
    assert starts[0]["run_id"] != starts[1]["run_id"]
    assert starts[0]["run_id"].startswith("provider-canary/")
    assert starts[1]["run_id"].startswith("provider-canary/")


@pytest.mark.parametrize(
    ("failure_mode", "expected_problem"),
    (
        ("server-down", "server-unavailable"),
        ("timeout", "run-timeout"),
        (
            "refusal",
            "start-refused-agent-executor-binding-unavailable",
        ),
    ),
)
def test_server_down_timeout_and_start_refusal_each_leave_a_loud_fail_receipt(
    tmp_path: Path,
    workflow_directory: Path,
    failure_mode: str,
    expected_problem: str,
) -> None:
    http = FakeHttp((configuration(),), workflow_directory)
    if failure_mode == "server-down":
        http.server_down = True
    elif failure_mode == "timeout":
        http.terminal_after_reads = 100
    else:
        http.refusal = "agent-executor-binding-unavailable"
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code is not None
    assert receipt.problem_code.value == expected_problem
    assert receipt.terminal_hash is None


def test_a_typed_poll_refusal_is_named_by_its_own_code_not_start_refused(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """`start-refused-*` belongs to the POST /runs answer alone.

    A refusal met while merely watching an already-accepted run (a GET
    poll) is a different problem and keeps its own bare code -- conflating
    the two would say a start was refused when it plainly was not.
    """

    http = FakeHttp((configuration(),), workflow_directory)
    http.poll_refusal = "invalid-public-run-reference"
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.problem_code == ProviderProbeProblemCode(
        "invalid-public-run-reference"
    )


def test_a_failed_run_replaces_the_live_success_receipt(
    tmp_path: Path, workflow_directory: Path
) -> None:
    state_directory = tmp_path / "state"
    destination = write_live_success(state_directory)
    previous = destination.read_bytes()
    http = FakeHttp((configuration(),), workflow_directory, server_down=True)

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.failed == 1
    assert destination.read_bytes() != previous
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode("server-unavailable")


def test_a_failed_run_without_a_named_refusal_keeps_the_plain_problem_code(
    tmp_path: Path, workflow_directory: Path
) -> None:
    http = FakeHttp((configuration(),), workflow_directory, terminal_state="FAILED")
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode("run-failed")


def test_a_failed_run_whose_transcript_names_a_provider_refusal_gets_its_own_code(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """A rate-limited provider is not a broken vector, and the receipt says so.

    The run's own terminal state carries no more than `FAILED`; telling the two
    apart needs the failed node's own transcript (#1029).
    """

    http = FakeHttp(
        (configuration(),),
        workflow_directory,
        terminal_state="FAILED",
        node_detail_transcript_events=(
            {
                "event": "provider-terminal-refusal",
                "terminal_reason": "rate_limit_error",
                "api_error_status": "429",
                "text": "Not logged in · Please run /login",
                "redacted": False,
                "moment": {"origin": "v1-before-moments"},
            },
        ),
    )
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode("provider-refused")


def test_an_unreadable_local_workflow_replaces_the_live_success_receipt(
    tmp_path: Path, workflow_directory: Path
) -> None:
    state_directory = tmp_path / "state"
    write_live_success(state_directory)
    http = FakeHttp((configuration(),), workflow_directory)
    (workflow_directory / "provider-canary-headless.yaml").unlink()

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory),
        http=http,
        clock=FakeClock(),
    )

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode("workflow-unreadable")


@pytest.mark.parametrize(
    ("failure_mode", "expected_problem"),
    (
        ("unavailable", "server-unavailable"),
        ("unreadable", "server-answer-unreadable"),
        ("empty", "no-startable-provider-vectors"),
        ("catalog", "server-unavailable"),
        ("health-unavailable", "server-unavailable"),
        ("health-refused", "health-refused"),
        ("health-unreadable", "server-answer-unreadable"),
        ("health-provenance", "server-answer-unreadable"),
    ),
)
def test_discovery_failure_preserves_every_live_success_byte_for_byte(
    tmp_path: Path,
    workflow_directory: Path,
    failure_mode: str,
    expected_problem: str,
) -> None:
    state_directory = tmp_path / "state"
    destination = write_live_success(state_directory)
    previous = destination.read_bytes()
    http = FakeHttp((configuration(),), workflow_directory)
    if failure_mode == "unavailable":
        http.configuration_unavailable = True
    elif failure_mode == "unreadable":
        http.configuration_answer = b"not-json"
    elif failure_mode == "catalog":
        http.catalog_unavailable = True
    elif failure_mode == "health-unavailable":
        http.health_unavailable = True
    elif failure_mode == "health-refused":
        http.health_refusal = "health-refused"
    elif failure_mode == "health-unreadable":
        http.health_answer = b"not-json"
    elif failure_mode == "health-provenance":
        http.health_source_commit = "not-a-commit"
    else:
        http.configurations = ()

    with pytest.raises(ProviderCanaryDiscoveryFailed, match=expected_problem):
        execute_provider_canaries(
            settings(workflow_directory, state_directory), http=http, clock=FakeClock()
        )

    assert destination.read_bytes() == previous
    assert not any(method == "POST" for method, _path, _body in http.calls)


def test_empty_discovery_without_previous_receipts_is_a_loud_cli_failure(
    tmp_path: Path,
    workflow_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    http = FakeHttp((), workflow_directory)
    monkeypatch.setattr(
        "atelier2.host.execute_provider_canaries",
        lambda canary_settings: execute_provider_canaries(
            canary_settings, http=http, clock=FakeClock()
        ),
    )

    exit_status = main(
        [
            "provider-canary",
            "--workflow-directory",
            str(workflow_directory),
            "--state-directory",
            str(tmp_path / "state"),
        ]
    )

    assert exit_status == 1
    assert "no startable provider vectors" in capsys.readouterr().err


def test_a_hung_http_start_uses_the_terminal_bound_and_leaves_a_fail_receipt(
    tmp_path: Path,
    workflow_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[tuple[str, float]] = []
    workflow_name = "provider-canary-headless"
    workflow_hash = Sha256Hash.of(
        (workflow_directory / f"{workflow_name}.yaml").read_bytes()
    ).value

    def hung_start(request: Request, *, timeout: float) -> io.BytesIO:
        full_url = request.full_url
        timeouts.append((full_url, timeout))
        method = request.method
        if full_url.endswith("/health"):
            answer = HealthResource(
                status="serving",
                source_commit=SOURCE_COMMIT,
                source_tree="d" * 40,
                serve_started_at="2026-08-31T08:00:00Z",
            ).model_dump_json()
        elif "/agent-configuration-revisions?" in full_url:
            answer = encoded(
                items=(configuration(),), next_after_revision_hash=None
            ).decode()
        elif full_url.endswith(f"/workflow-revisions/by-name/{workflow_name}"):
            answer = CatalogNameResolutionResource(
                display_name=workflow_name,
                lineage_id="4" * 64,
                workflow_revision_hash=workflow_hash,
                revision_number=1,
            ).model_dump_json()
        elif full_url.endswith("/runs") and method == "POST":
            raise TimeoutError("HTTP start timed out")
        else:
            raise AssertionError((method, full_url))
        return io.BytesIO(answer.encode())

    monkeypatch.setattr("atelier2.host.provider_canary.urlopen", hung_start)
    state_directory = tmp_path / "state"
    canary_settings = ProviderCanarySettings(
        service_url="http://127.0.0.1:8422",
        workflow_directory=workflow_directory,
        state_directory=state_directory,
        terminal_timeout_seconds=5,
        poll_interval_seconds=1,
    )

    report = execute_provider_canaries(canary_settings, clock=FakeClock())

    assert timeouts
    assert all(
        timeout <= canary_settings.terminal_timeout_seconds
        for url, timeout in timeouts
        if url.endswith("/runs")
    )
    assert all(timeout <= 30 for _url, timeout in timeouts)
    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode("server-unavailable")


@pytest.mark.parametrize(
    ("failure_status", "failure_body", "expected_problem_code"),
    (
        (
            422,
            lambda: problem_resource("uncast-agent-roles").model_dump_json().encode(),
            "start-refused-uncast-agent-roles",
        ),
        (502, lambda: b"upstream exploded", "http-refused"),
    ),
    ids=("typed-start-refusal", "unclassifiable-http-refusal"),
)
def test_a_real_http_start_refusal_is_classified_by_the_owning_vocabulary(
    tmp_path: Path,
    workflow_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_status: int,
    failure_body: Callable[[], bytes],
    expected_problem_code: str,
) -> None:
    """The URN is parsed at its owner (`atelier2.api.problems`), never guessed.

    A typed start refusal (`uncast-agent-roles` among them) keeps its own
    honest token instead of degrading to `http-refused`; an answer this
    client genuinely cannot classify still keeps that same generic code,
    unchanged from before.
    """

    workflow_name = "provider-canary-headless"
    workflow_hash = Sha256Hash.of(
        (workflow_directory / f"{workflow_name}.yaml").read_bytes()
    ).value

    def answering(request: Request, *, timeout: float) -> io.BytesIO:
        full_url = request.full_url
        method = request.method
        if full_url.endswith("/health"):
            answer = (
                HealthResource(
                    status="serving",
                    source_commit=SOURCE_COMMIT,
                    source_tree="d" * 40,
                    serve_started_at="2026-08-31T08:00:00Z",
                )
                .model_dump_json()
                .encode()
            )
        elif "/agent-configuration-revisions?" in full_url:
            answer = encoded(items=(configuration(),), next_after_revision_hash=None)
        elif full_url.endswith(f"/workflow-revisions/by-name/{workflow_name}"):
            answer = (
                CatalogNameResolutionResource(
                    display_name=workflow_name,
                    lineage_id="4" * 64,
                    workflow_revision_hash=workflow_hash,
                    revision_number=1,
                )
                .model_dump_json()
                .encode()
            )
        elif full_url.endswith("/runs") and method == "POST":
            raise HTTPError(
                full_url,
                failure_status,
                "refused",
                Message(),
                io.BytesIO(failure_body()),
            )
        else:
            raise AssertionError((method, full_url))
        return io.BytesIO(answer)

    monkeypatch.setattr("atelier2.host.provider_canary.urlopen", answering)
    state_directory = tmp_path / "state"
    canary_settings = ProviderCanarySettings(
        service_url="http://127.0.0.1:8422",
        workflow_directory=workflow_directory,
        state_directory=state_directory,
        terminal_timeout_seconds=5,
        poll_interval_seconds=1,
    )

    report = execute_provider_canaries(canary_settings, clock=FakeClock())

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.problem_code == ProviderProbeProblemCode(expected_problem_code)


@pytest.mark.parametrize("limit_kind", ("pages", "vectors"))
def test_discovery_limits_are_loud_and_receipt_neutral(
    tmp_path: Path, workflow_directory: Path, limit_kind: str
) -> None:
    state_directory = tmp_path / "state"
    destination = write_live_success(state_directory)
    previous = destination.read_bytes()
    http = FakeHttp((), workflow_directory)
    if limit_kind == "pages":
        http.configuration_pages = tuple(
            encoded(items=(), next_after_revision_hash=f"{page_number + 1:064x}")
            for page_number in range(PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES)
        )
        expected_problem = "too-many-configuration-pages"
    else:
        first_page = tuple(
            configuration(configuration_hash=f"{number:064x}")
            for number in range(PROVIDER_CANARY_MAXIMUM_VECTORS)
        )
        http.configuration_pages = (
            encoded(items=first_page, next_after_revision_hash="e" * 64),
            encoded(
                items=(configuration(configuration_hash="f" * 64),),
                next_after_revision_hash=None,
            ),
        )
        expected_problem = "too-many-provider-vectors"

    with pytest.raises(ProviderCanaryDiscoveryFailed, match=expected_problem):
        execute_provider_canaries(
            settings(workflow_directory, state_directory), http=http, clock=FakeClock()
        )

    assert destination.read_bytes() == previous
    if limit_kind == "pages":
        assert http.configuration_page_reads == (
            PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES
        )


def test_overall_deadline_bounds_cumulative_work_and_never_enters_later_vector(
    tmp_path: Path, workflow_directory: Path
) -> None:
    clock = FakeClock()
    delegate = FakeHttp(
        (
            configuration(),
            configuration(configuration_hash="f" * 64),
        ),
        workflow_directory,
    )

    class ConsumingHttp:
        def get(self, path: str, *, timeout_seconds: float) -> bytes:
            clock.elapsed += min(1.0, timeout_seconds)
            return delegate.get(path, timeout_seconds=timeout_seconds)

        def post(
            self,
            path: str,
            body: bytes,
            *,
            timeout_seconds: float,
            media_type: str = "application/json",
        ) -> bytes:
            clock.elapsed += min(1.0, timeout_seconds)
            return delegate.post(
                path,
                body,
                timeout_seconds=timeout_seconds,
                media_type=media_type,
            )

    http = ConsumingHttp()
    state_directory = tmp_path / "state"
    canary_settings = ProviderCanarySettings(
        service_url="http://127.0.0.1:8422",
        workflow_directory=workflow_directory,
        state_directory=state_directory,
        terminal_timeout_seconds=5,
        poll_interval_seconds=1,
        process_timeout_seconds=5,
    )

    with pytest.raises(ProviderCanaryProcessTimedOut):
        execute_provider_canaries(canary_settings, http=http, clock=clock)

    starts = [path for method, path, _body in delegate.calls if method == "POST"]
    assert starts == ["/runs"]
    assert clock.elapsed <= canary_settings.process_timeout_seconds
    (first_receipt,) = read_receipts(state_directory)
    assert first_receipt.vector == ProviderProbeVectorId(
        f"headless-{CONFIGURATION_HASH}"
    )
    assert first_receipt.problem_code == ProviderProbeProblemCode("process-timeout")


def test_atomic_receipt_replacement_never_exposes_a_partly_written_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "vector.json"
    destination.write_bytes(b"previous-complete-receipt")
    receipt = ProviderProbeReceipt(
        vector=ProviderProbeVectorId("headless-vector"),
        configuration_hash=AgentConfigurationRevisionHash(CONFIGURATION_HASH),
        workflow_hash=WorkflowRevisionHash("d" * 64),
        source_commit=SOURCE_COMMIT,
        observed_at=RecordedAt("2026-09-01T08:00:00Z"),
        valid_until=RecordedAt("2026-09-02T10:00:00Z"),
        result=ProviderProbeResult.SUCCEEDED,
        run_reference=RunId("provider-canary/vector/2026-09-01T08:00:00Z"),
        terminal_hash=Sha256Hash(TERMINAL_HASH),
    )
    monkeypatch.setattr(
        "atelier2.host.provider_canary.os.replace",
        lambda _source, _destination: (_ for _ in ()).throw(OSError("interrupted")),
    )

    with pytest.raises(OSError, match="interrupted"):
        write_provider_canary_receipt_atomic(destination, receipt)

    assert destination.read_bytes() == b"previous-complete-receipt"
    assert list(tmp_path.iterdir()) == [destination]
