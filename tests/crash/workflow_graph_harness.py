from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dbos import DBOS

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import (
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import (
    RunId,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    launching,
    publish_checked_model_registry,
)
from tests.scenarios.runs import submit_wait_answer
from tests.scenarios.workflows import ANY_JSON_SCHEMA

CRASHED = 86
OPEN_PR_OPERATION = PublishedRevision(
    RevisionKind.ADAPTER_OPERATION,
    json.dumps({"operation": AdapterOperationName.OPEN_PR.value}).encode("utf-8"),
)
"""The one operation a V3 Action node may name; the loopback adapter performs
whatever request the node's Agent predecessor handed it."""
OPEN_PR_GRANT = PublishedRevision(
    RevisionKind.TOOL,
    json.dumps({"capability": "open-pr"}).encode("utf-8"),
)
"""The one declared agent grant the crash harness publishes for its effect path."""
V3_PROVIDER_OUTPUT = b'"the exact provider bytes"'
_COUNTING_PROVIDER = (
    "from pathlib import Path; import os,sys; "
    "Path(sys.argv[1]).open('ab').write(b'x'); "
    "os.write(1, bytes.fromhex(sys.argv[2]))"
)


def runtime(database: Path, external: Path, version: str) -> DbosRuntime:
    provider = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        V3_PROVIDER_OUTPUT,
        command=launching(
            sys.executable,
            "-c",
            _COUNTING_PROVIDER,
            str(database.parent / "provider-count"),
            V3_PROVIDER_OUTPUT.hex(),
        ),
    )
    return DbosRuntime(
        DbosRuntimeSettings(
            database,
            version,
            agent_scratch_root=agent_scratch_root(database.parent),
        ),
        LoopbackEffectAdapterFactory(
            external,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-crash-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (provider,),
    )


def initialize(database: Path, external: Path, version: str) -> None:
    lease = runtime(database, external, version)
    try:
        lease.initialize_storage()
    finally:
        lease.close()


def seed_v3(
    database: Path,
    external: Path,
    version: str,
    run_id: str,
    document: bytes,
    revision_format_version: AgentConfigurationRevisionFormatVersion,
) -> None:
    lease = runtime(database, external, version)
    try:
        lease.initialize_storage()
        catalog = DbosAgentConfigurationCatalog(
            lease.engine, lease.agent_executor_registry
        )
        auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
        configuration = AgentConfigurationRevision(
            "opus",
            auth.revision_hash,
            AgentExecutorRevision("exact/v1"),
            AgentExecutionCapability.HEADLESS,
            revision_format_version,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(configuration),
            AgentConfigurationRevisionCreated,
        )
        publish_checked_model_registry(
            lease.engine, ProviderId("exact"), (configuration,)
        )
        catalog_store = DbosCatalogStore(lease.engine)
        for revision in (ANY_JSON_SCHEMA, OPEN_PR_OPERATION, OPEN_PR_GRANT):
            published = catalog_store.publish_revision(revision)
            assert isinstance(
                published, (PublishedRevisionCreated, PublishedRevisionExisting)
            )
        workflow = WorkflowRevision(document)
        DbosWorkflowRevisionPublisher(lease.engine).publish(workflow)
        started = DbosDurableRunStarter(
            lease.engine,
            lease.settings,
            lease.agent_executor_registry,
        ).start_published(
            StartPublishedRunRequestV2(
                RunId(run_id),
                workflow.revision_hash,
                AgentBindingSet(
                    (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
                ),
            )
        )
        assert isinstance(started, DurableRunCreated)
    finally:
        lease.close()


def submit_answer(
    database: Path,
    external: Path,
    version: str,
    run_id: str,
    node_id: str,
    answer: str,
) -> None:
    lease = runtime(database, external, version)
    try:
        lease.initialize_storage()
        with sqlite3.connect(database, timeout=30) as connection:
            revision_hash = str(
                connection.execute(
                    "SELECT revision_hash FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
        submit_wait_answer(
            lease.engine,
            lease.settings.application_version,
            SubmitWaitAnswerRequest(
                RunId(run_id),
                WorkflowRevisionHash(revision_hash),
                node_id,
                NodeExecutionId.for_node(
                    RunId(run_id), WorkflowRevisionHash(revision_hash), node_id
                ),
                WaitAnswerActor.OPERATOR,
                answer.encode("utf-8"),
            ),
        )
    finally:
        lease.close()


def _crash_once(marker: Path, operation_name: str) -> None:
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.write(descriptor, f"{operation_name}:before-record".encode())
    os.close(descriptor)
    os._exit(CRASHED)


def install_crash(marker: Path, operation_name: str) -> None:
    from dbos._sys_db import OperationResultInternal, SystemDatabase

    original = SystemDatabase.record_operation_result

    def injected(
        self: SystemDatabase,
        result: OperationResultInternal,
        *,
        completed_at_epoch_ms: int | None = None,
    ) -> None:
        if result.get("function_name") == operation_name:
            _crash_once(marker, operation_name)
        original(self, result, completed_at_epoch_ms=completed_at_epoch_ms)

    SystemDatabase.record_operation_result = injected


def install_successor_start_crash(marker: Path, successor_node_id: str) -> None:
    original = DBOS.start_workflow

    def injected(func: Callable[..., Any], *arguments: Any, **keywords: Any) -> Any:
        if (
            getattr(func, "__name__", "") == "durable_node"
            and len(arguments) == 3
            and arguments[2] == successor_node_id
        ):
            _crash_once(marker, "start-successor")
        return original(func, *arguments, **keywords)

    DBOS.start_workflow = injected


def execute_until(
    database: Path,
    external: Path,
    version: str,
    run_id: str,
    target_state: str,
    marker: Path | None,
    operation_name: str | None,
) -> None:
    lease = runtime(database, external, version)
    try:
        if marker is not None and operation_name is not None:
            if operation_name.startswith("start-successor:"):
                install_successor_start_crash(
                    marker, operation_name.removeprefix("start-successor:")
                )
            else:
                install_crash(marker, operation_name)
        lease.launch()
        deadline = time.monotonic() + 10
        observed = ""
        while time.monotonic() < deadline:
            with sqlite3.connect(database, timeout=30) as connection:
                observed = str(
                    connection.execute(
                        "SELECT state FROM runs WHERE run_id=?", (run_id,)
                    ).fetchone()[0]
                )
                failed = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT workflow_uuid FROM workflow_status "
                        "WHERE status IN ('ERROR','CANCELLED')"
                    )
                )
            if failed:
                DBOS.retrieve_workflow(str(failed[0])).get_result()
                raise AssertionError("failed durable workflow returned a result")
            if observed == target_state:
                return
            time.sleep(0.025)
        raise TimeoutError(f"run stayed {observed!r}, expected {target_state!r}")
    finally:
        lease.close()


def main() -> None:
    command, raw_database, raw_external, version, *arguments = sys.argv[1:]
    database = Path(raw_database)
    external = Path(raw_external)
    if command == "initialize":
        initialize(database, external, version)
    elif command == "seed-v3":
        run_id, document_hex, configuration_format = arguments
        seed_v3(
            database,
            external,
            version,
            run_id,
            bytes.fromhex(document_hex),
            AgentConfigurationRevisionFormatVersion(int(configuration_format)),
        )
    elif command == "answer":
        run_id, node_id, answer = arguments
        submit_answer(database, external, version, run_id, node_id, answer)
    else:
        run_id, raw_marker, raw_operation = arguments
        target_state = {
            "execute-v3-until-wait": "WAITING_INPUT",
            "execute-v3-until-complete": "COMPLETED",
        }[command]
        execute_until(
            database,
            external,
            version,
            run_id,
            target_state,
            None if raw_marker == "NONE" else Path(raw_marker),
            None if raw_operation == "NONE" else raw_operation,
        )
    print(json.dumps({"command": command, "ok": True}))


if __name__ == "__main__":
    main()
