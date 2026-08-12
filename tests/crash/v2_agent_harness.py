from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow import AGENT_COMMIT_STEP_NAME
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.agent_executions import AgentExecutorKey
from atelier2.ports.durable_runs import StartPublishedRunRequestV2

CRASHED = 86
DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
"""


@dataclass
class HarnessExecutor:
    output: bytes

    def execute(self, request: AgentExecutionRequestV2) -> AgentExecutionResult:
        del request
        return AgentExecutionResult(self.output)

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class HarnessFactory:
    identity: str
    output: bytes

    @property
    def key(self) -> AgentExecutorKey:
        return AgentExecutorKey(
            ProviderId("anthropic"), AgentExecutorRevision("claude-cli/v1")
        )

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return AgentExecutorOperationalIdentity(self.identity)

    def open(self) -> HarnessExecutor:
        return HarnessExecutor(self.output)


def runtime(database: Path, identity: str, output: bytes) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(database, "v2-agent-crash"),
        LoopbackEffectAdapterFactory(
            database.with_name("effects.sqlite"),
            AdapterRevision("loopback-v1"),
            EffectDestination("v2-agent-crash"),
        ),
        ExactOutputAgentExecutorFactory(),
        (HarnessFactory(identity, output),),
    )


def seed(database: Path, identity: str, output: bytes) -> None:
    lease = runtime(database, identity, output)
    try:
        lease.initialize_storage()
        auth = AuthProfileRevision(
            "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
        )
        configuration = AgentConfigurationRevision(
            "opus", auth.revision_hash, AgentExecutorRevision("claude-cli/v1")
        )
        catalog = DbosAgentConfigurationCatalog(
            lease.engine, lease.agent_executor_registry
        )
        catalog.publish_auth_profile_revision(auth)
        catalog.publish_agent_configuration_revision(configuration)
        revision = WorkflowRevision(DOCUMENT)
        DbosWorkflowRevisionPublisher(lease.engine).publish(revision)
        DbosDurableRunStarter(
            lease.engine, lease.settings, lease.agent_executor_registry
        ).start_published(
            StartPublishedRunRequestV2(
                RunId("v2/crash"),
                revision.revision_hash,
                AgentBindingSet(
                    (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
                ),
            )
        )
    finally:
        lease.close()


def _crash_once(marker: Path) -> None:
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.write(descriptor, AGENT_COMMIT_STEP_NAME.encode("utf-8"))
    os.close(descriptor)
    os._exit(CRASHED)


def install_crash(marker: Path) -> None:
    from dbos._sys_db import OperationResultInternal, SystemDatabase

    original = SystemDatabase.record_operation_result

    def injected(
        self: SystemDatabase,
        result: OperationResultInternal,
        *,
        completed_at_epoch_ms: int | None = None,
    ) -> None:
        if result.get("function_name") == AGENT_COMMIT_STEP_NAME:
            _crash_once(marker)
        original(self, result, completed_at_epoch_ms=completed_at_epoch_ms)

    SystemDatabase.record_operation_result = injected


def execute(
    database: Path,
    identity: str,
    output: bytes,
    marker: Path | None,
) -> None:
    lease = runtime(database, identity, output)
    try:
        if marker is not None:
            install_crash(marker)
        lease.launch()
        deadline = time.monotonic() + 10
        state = ""
        while time.monotonic() < deadline:
            with sqlite3.connect(database, timeout=30) as connection:
                state = str(
                    connection.execute(
                        "SELECT state FROM runs WHERE run_id='v2/crash'"
                    ).fetchone()[0]
                )
                failed = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT status FROM workflow_status "
                        "WHERE status IN ('ERROR','CANCELLED')"
                    )
                )
            if failed:
                raise RuntimeError(f"durable workflow failed with {failed!r}")
            if state == "COMPLETED":
                return
            time.sleep(0.025)
        raise TimeoutError(f"V2 run stayed {state!r}")
    finally:
        lease.close()


def main() -> None:
    command, raw_database, identity, output_hex, raw_marker = sys.argv[1:]
    database = Path(raw_database)
    output = bytes.fromhex(output_hex)
    if command == "seed":
        seed(database, identity, output)
    else:
        execute(
            database,
            identity,
            output,
            None if raw_marker == "NONE" else Path(raw_marker),
        )
    print(json.dumps({"command": command, "ok": True}))


if __name__ == "__main__":
    main()
