from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import Request

import pytest

from atelier2.api.wire.resources import (
    CatalogNameResolutionResource,
    HealthResource,
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
    ProviderCanaryHttpRefused,
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
    server_down: bool = False
    configuration_answer: bytes | None = None
    configuration_unavailable: bool = False
    calls: list[tuple[str, str, bytes | None]] = field(default_factory=list)
    workflow_hashes: list[str] = field(default_factory=list)
    run_reads: int = 0

    def get(self, path: str) -> bytes:
        self.calls.append(("GET", path, None))
        if path == "/health":
            return (
                HealthResource(
                    status="serving", source_commit=SOURCE_COMMIT, source_tree="d" * 40
                )
                .model_dump_json()
                .encode()
            )
        if path.startswith("/agent-configuration-revisions"):
            if self.configuration_unavailable:
                raise ProviderCanaryServerUnavailable("configuration listing timed out")
            if self.configuration_answer is not None:
                return self.configuration_answer
            return encoded(items=self.configurations, next_after_revision_hash=None)
        if path.startswith("/workflow-revisions/by-name/"):
            name = path.rsplit("/", 1)[-1]
            workflow_hash = Sha256Hash.of(
                (self.workflow_directory / f"{name}.yaml").read_bytes()
            ).value
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
        self, path: str, body: bytes, *, media_type: str = "application/json"
    ) -> bytes:
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


def configuration(
    *,
    provider: str = "anthropic",
    executor: str = "claude-subscription/v1",
    capability: str = "headless",
    configuration_hash: str = CONFIGURATION_HASH,
) -> dict[str, object]:
    return {
        "model": "provider-model",
        "auth_profile_revision_hash": "e" * 64,
        "executor_revision": executor,
        "provider_id": provider,
        "auth_mode": "subscription",
        "requested_capability": capability,
        "agent_configuration_revision_hash": configuration_hash,
        "startable": True,
        "not_startable_reason": None,
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
            "agent-executor-binding-unavailable",
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


def test_an_unreadable_local_workflow_replaces_the_live_success_receipt(
    tmp_path: Path, workflow_directory: Path
) -> None:
    state_directory = tmp_path / "state"
    write_live_success(state_directory)
    (workflow_directory / "provider-canary-headless.yaml").unlink()

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory),
        http=FakeHttp((configuration(),), workflow_directory),
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
    ),
)
def test_discovery_failure_replaces_every_live_success_with_a_fail_receipt(
    tmp_path: Path,
    workflow_directory: Path,
    failure_mode: str,
    expected_problem: str,
) -> None:
    state_directory = tmp_path / "state"
    write_live_success(state_directory)
    http = FakeHttp((configuration(),), workflow_directory)
    if failure_mode == "unavailable":
        http.configuration_unavailable = True
    elif failure_mode == "unreadable":
        http.configuration_answer = b"not-json"
    else:
        http.configurations = ()

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.attempted == 0
    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode(expected_problem)
    assert receipt.observed_at == RecordedAt("2026-09-01T08:00:00Z")


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
    timeouts: list[float] = []
    workflow_name = "provider-canary-headless"
    workflow_hash = Sha256Hash.of(
        (workflow_directory / f"{workflow_name}.yaml").read_bytes()
    ).value

    def hung_start(request: Request, *, timeout: float) -> io.BytesIO:
        timeouts.append(timeout)
        full_url = request.full_url
        method = request.method
        if full_url.endswith("/health"):
            answer = HealthResource(
                status="serving", source_commit=SOURCE_COMMIT, source_tree="d" * 40
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
        timeout <= canary_settings.terminal_timeout_seconds for timeout in timeouts
    )
    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode("server-unavailable")


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
