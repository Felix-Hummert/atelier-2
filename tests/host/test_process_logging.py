from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptState,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    WatchdogGenerationId,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.host.logging import PROCESS_LOGGER_NAME, configure_process_logging
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationResult,
    AgentAttemptClaimedByThisCall,
    AgentAttemptFailed,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentExecutionFailure,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from tests.scenarios.agents import agent_attempt_execution
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

ALWAYS_KEYS = ("ts", "level", "logger", "message")
ATTEMPT_KEYS = ("event", "run_id", "node_id", "attempt_id")
HTTP_KEYS = ("event", "exception")


@pytest.fixture
def process_log() -> Iterator[io.StringIO]:
    """Install the process formatter on a private stream and restore after."""

    stream = io.StringIO()
    watched = ("", "atelier2", "uvicorn", "uvicorn.error", "uvicorn.access", "dbos")
    snapshot = tuple(_logger_snapshot(name) for name in watched)
    configure_process_logging(stream)
    try:
        yield stream
    finally:
        for name, level, handlers, propagate, disabled in snapshot:
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled


def test_a_failed_attempt_emits_exactly_one_parseable_json_line(
    tmp_path: Path, process_log: io.StringIO
) -> None:
    execution = _failed_attempt_execution()
    workspaces = _MemoryWorkspaces(tmp_path)

    outcome = execute_agent_attempt(
        execution,
        _FailingExecutor(),
        _FailingAttemptStore(execution),
        _SilentSupervisor(),
        workspaces,
    )

    assert isinstance(outcome, AgentAttemptFailed)
    records = _parse_json_lines(process_log.getvalue())
    assert len(records) == 1
    record = records[0]
    assert tuple(record) == ALWAYS_KEYS + ATTEMPT_KEYS
    assert datetime.fromisoformat(record["ts"]).tzinfo is not None
    assert record["level"] == "warning"
    assert record["logger"] == PROCESS_LOGGER_NAME
    assert record["event"] == "agent_attempt_failed"
    assert record["run_id"] == execution.request.run_id.value
    assert record["node_id"] == execution.request.node_id
    assert record["attempt_id"] == execution.attempt_id.value
    assert execution.request.run_id.value in record["message"]
    assert execution.request.node_id in record["message"]
    assert execution.attempt_id.value in record["message"]
    assert "Traceback" not in record["message"]


def test_a_dbos_line_uses_the_same_json_keys(process_log: io.StringIO) -> None:
    logging.getLogger("dbos").info("durable runtime started")

    records = _parse_json_lines(process_log.getvalue())
    assert len(records) == 1
    record = records[0]
    assert tuple(record) == ALWAYS_KEYS
    assert datetime.fromisoformat(record["ts"]).tzinfo is not None
    assert record["level"] == "info"
    assert record["logger"] == "dbos"
    assert record["message"] == "durable runtime started"


def test_serve_configures_json_logging_before_the_runtime_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order: list[str] = []
    captured: dict[str, object] = {}

    def fake_configure(stream: object = None) -> None:
        del stream
        order.append("configure")

    def fake_compose(settings: object) -> tuple[object, object]:
        del settings
        order.append("compose")
        return object(), type("Runtime", (), {"close": lambda self: None})()

    class FakeConfig:
        def __init__(self, app: object, **kwargs: object) -> None:
            del app
            captured.update(kwargs)

    class FakeServer:
        def __init__(self, config: object) -> None:
            del config

        def run(self) -> None:
            order.append("run")

    monkeypatch.setattr(
        "atelier2.host.serving.configure_process_logging", fake_configure
    )
    monkeypatch.setattr("atelier2.host.serving.compose_application", fake_compose)
    monkeypatch.setattr("atelier2.host.serving.uvicorn.Config", FakeConfig)
    monkeypatch.setattr("atelier2.host.serving.uvicorn.Server", FakeServer)

    from atelier2.host.serving import serve
    from tests.host.test_local_host import served_settings

    serve(served_settings(tmp_path))

    assert order == ["configure", "compose", "run"]
    assert captured["access_log"] is False
    assert captured["log_config"] is None


def test_an_unhandled_http_exception_emits_http_internal_error(
    process_log: io.StringIO,
) -> None:
    app = create_app(
        source_commit="commit",
        source_tree="tree",
        ports=api_ports(),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
    )

    @app.get("/__probe-unhandled")
    def _probe() -> None:
        raise RuntimeError("probe fault")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/__probe-unhandled")

    assert response.status_code == 500
    records = _parse_json_lines(process_log.getvalue())
    assert len(records) == 1
    record = records[0]
    assert tuple(record) == ALWAYS_KEYS + HTTP_KEYS
    assert datetime.fromisoformat(record["ts"]).tzinfo is not None
    assert record["level"] == "error"
    assert record["logger"] == PROCESS_LOGGER_NAME
    assert record["event"] == "http_internal_error"
    assert "RuntimeError" in record["message"]
    assert "Traceback" not in record["message"]
    assert "RuntimeError" in record["exception"]
    assert "probe fault" in record["exception"]


def _parse_json_lines(text: str) -> list[dict[str, Any]]:
    lines = [line for line in text.splitlines() if line]
    return [json.loads(line) for line in lines]


def _logger_snapshot(
    name: str,
) -> tuple[str, int, list[logging.Handler], bool, bool]:
    logger = logging.getLogger(name)
    return (
        name,
        logger.level,
        list(logger.handlers),
        logger.propagate,
        logger.disabled,
    )


def _failed_attempt_execution() -> AgentAttemptExecution:
    run_id = RunId("diagnose/failed-attempt")
    revision = WorkflowRevisionHash("2" * 64)
    node_id = "builder"
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision, node_id),
        run_id,
        revision,
        node_id,
        ResolvedAgentBinding(AgentRole("builder"), configuration, auth),
        AgentExecutorOperationalIdentity("controlled-process"),
        b"build",
    )
    return agent_attempt_execution(request)


class _FailingExecutor:
    def prepare_process(self, request: object) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(("false",), standard_output_frame_bytes=1)

    def decode_process_completion(
        self, invocation: object, completion: AgentProcessCompletion
    ) -> AgentExecutionFailure:
        del invocation, completion
        return AgentExecutionFailure(
            AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
        )

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return


class _FailingAttemptStore:
    def __init__(self, execution: AgentAttemptExecution) -> None:
        self._attempt = _prepared(execution)

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        del execution
        return self._attempt

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimedByThisCall:
        del execution
        return AgentAttemptClaimedByThisCall(self._attempt)

    def complete_known_failure(
        self, execution: AgentAttemptExecution
    ) -> AgentAttemptFailed:
        failed = AgentAttempt(
            execution.attempt_id,
            execution.request.node_execution_id,
            execution.request.request_hash,
            execution.request.executor_operational_identity,
            execution.request.run_id,
            execution.request.workflow_revision_hash,
            execution.request.node_id,
            execution.ordinal,
            AgentAttemptState.FAILED,
            2,
            failure_code=AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        )
        return AgentAttemptFailed(failed)

    def load(self, attempt_id: AgentAttemptId) -> AgentAttempt:
        del attempt_id
        return self._attempt

    def complete_success(
        self, execution: AgentAttemptExecution, result: AgentExecutionResult
    ) -> AgentAttemptSucceeded:
        raise AssertionError((execution, result))

    def request_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationResult:
        raise AssertionError(request)

    def attest_cancellation_cleanup(
        self,
        request: CancelAgentAttemptRequest,
        disposition: AgentAttemptCancellationDisposition,
        process_owner_id: AgentProcessOwnerId | None,
        watchdog_generation_id: WatchdogGenerationId | None,
    ) -> AgentAttemptCancellationAccepted:
        raise AssertionError(
            (request, disposition, process_owner_id, watchdog_generation_id)
        )

    def mark_cancellation_owner_not_local(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttempt:
        raise AssertionError(request)

    def bind_watchdog(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt:
        raise AssertionError((execution, process_owner_id, watchdog_generation_id))

    def observe_process(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt:
        raise AssertionError((execution, process_owner_id, watchdog_generation_id))


class _SilentSupervisor:
    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        return _prepared(execution)

    def launch_and_wait(
        self, execution: AgentAttemptExecution, invocation: AgentProcessInvocation
    ) -> AgentProcessCompletion:
        del execution, invocation
        return AgentProcessCompletion(1, b"", b"")

    def cancel(
        self, attempt: AgentAttempt
    ) -> tuple[
        AgentAttemptCancellationDisposition,
        AgentProcessOwnerId,
        WatchdogGenerationId,
    ]:
        raise AssertionError(attempt)

    def recover(
        self, attempt: AgentAttempt
    ) -> tuple[
        AgentAttemptCancellationDisposition,
        AgentProcessOwnerId,
        WatchdogGenerationId,
    ]:
        raise AssertionError(attempt)

    def release(self, attempt: AgentAttempt) -> None:
        del attempt

    def finalize(self, execution: AgentAttemptExecution) -> None:
        del execution


class _MemoryWorkspaces:
    def __init__(self, root: Path) -> None:
        self._root = root

    def preflight(self) -> None:
        return

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        directory = (self._root / attempt_id.value).resolve()
        directory.mkdir()
        standing = directory.stat()
        return AgentAttemptWorkspaceLease(
            attempt_id, directory, standing.st_dev, standing.st_ino
        )

    def release(self, attempt_id: AgentAttemptId) -> None:
        del attempt_id


def _prepared(execution: AgentAttemptExecution) -> AgentAttempt:
    return AgentAttempt(
        execution.attempt_id,
        execution.request.node_execution_id,
        execution.request.request_hash,
        execution.request.executor_operational_identity,
        execution.request.run_id,
        execution.request.workflow_revision_hash,
        execution.request.node_id,
        execution.ordinal,
        AgentAttemptState.PREPARED,
        0,
    )
