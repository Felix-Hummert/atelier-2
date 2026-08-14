from __future__ import annotations

import hashlib
from typing import assert_never

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.agent_catalog import (
    agent_configuration_from_record,
    auth_profile_from_record,
)
from atelier2.adapters.dbos.run_store import (
    run_from_record,
    run_from_record_with_bindings,
)
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_configuration_revisions,
    auth_profile_revisions,
    run_agent_bindings,
    runs,
    workflow_revisions,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow import QUEUE_NAME, WORKFLOW_NAME
from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.contracts.agents import (
    AgentBindingSet,
    ResolvedAgentBinding,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import (
    RevisionHashCollision,
    Run,
    RunId,
    RunIdentityConflict,
    RunState,
    StartRunRequest,
    WorkflowRevision,
)
from atelier2.contracts.workflows import AgentNodeV2, WorkflowGraph, WorkflowGraphV2
from atelier2.ports.agent_executions import AgentExecutorKey, AgentExecutorRegistry
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurableAgentConfigurationRevisionMissing,
    DurableAgentExecutorBindingUnavailable,
    DurableAgentExecutorCapabilityUnavailable,
    DurableInvalidAgentBindings,
    DurablePublishedRunResult,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableStateCorrupt,
    DurableWriteUnavailable,
    StartPublishedRunRequest,
    StartPublishedRunRequestV2,
)
from atelier2.ports.workflow_revisions import (
    DurableRevisionCollision,
    DurableRevisionCreated,
    DurableRevisionExisting,
    DurableRevisionPublicationResult,
)

WORKFLOW_ID_PREFIX = "atelier2-run-"


def bootstrap_workflow_id_for(run_id: RunId) -> str:
    return WORKFLOW_ID_PREFIX + hashlib.sha256(run_id.value.encode()).hexdigest()


class DbosDurableRunStarter:
    def __init__(
        self,
        engine: Engine,
        settings: DbosRuntimeSettings,
        agent_executor_registry: AgentExecutorRegistry | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._agent_executor_registry = (
            AgentExecutorRegistry()
            if agent_executor_registry is None
            else agent_executor_registry
        )

    def start(self, request: StartRunRequest) -> Run:
        graph = parse_executable_workflow_document(request.revision.document)
        if not isinstance(graph, WorkflowGraph):
            raise TypeError("the V1 direct-start contract requires a V1 workflow")
        client = DBOSClient(
            system_database_engine=self._engine, use_listen_notify=False
        )
        try:
            with self._engine.begin() as connection:
                self._insert_or_verify_revision(connection, request)
                existing = self._existing_run(connection, request.run_id)
                if existing is not None:
                    if existing.revision_hash != request.revision.revision_hash:
                        raise RunIdentityConflict(
                            "RunId already belongs to another workflow revision"
                        )
                    return existing

                workflow_id = bootstrap_workflow_id_for(request.run_id)
                connection.execute(
                    runs.insert().values(
                        run_id=request.run_id.value,
                        bootstrap_workflow_id=workflow_id,
                        revision_hash=request.revision.revision_hash.value,
                        workflow_format_version=1,
                        agent_binding_set_hash=None,
                        current_node_id=graph.start,
                        state=RunState.STARTED.value,
                        state_version=0,
                        last_event_sequence=0,
                        terminal_hash=None,
                    )
                )
                options: EnqueueOptions = {
                    "workflow_name": WORKFLOW_NAME,
                    "queue_name": QUEUE_NAME,
                    "workflow_id": workflow_id,
                    "app_version": self._settings.application_version,
                }
                client.enqueue_in_transaction(
                    connection,
                    options,
                    request.run_id.value,
                    request.revision.revision_hash.value,
                )
                return Run(
                    request.run_id,
                    request.revision.revision_hash,
                    RunState.STARTED,
                    graph.start,
                    0,
                    0,
                )
        finally:
            client.destroy()

    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult:
        try:
            with self._engine.connect() as read_connection:
                document = read_connection.scalar(
                    sa.select(workflow_revisions.c.document).where(
                        workflow_revisions.c.revision_hash
                        == request.revision_hash.value
                    )
                )
            if document is None:
                return DurableRunRevisionMissing()
            revision_document = bytes(document)
            revision = WorkflowRevision(revision_document)
            if revision.revision_hash != request.revision_hash:
                return DurableStateCorrupt()
            graph = parse_executable_workflow_document(revision.document)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

        client: DBOSClient | None = None
        try:
            client = DBOSClient(
                system_database_engine=self._engine, use_listen_notify=False
            )
            with canonical_write_transaction(self._engine) as connection:
                stored_document = connection.scalar(
                    sa.select(workflow_revisions.c.document).where(
                        workflow_revisions.c.revision_hash
                        == request.revision_hash.value
                    )
                )
                if (
                    stored_document is None
                    or bytes(stored_document) != revision_document
                ):
                    raise RuntimeError(
                        "published revision changed between parse and serialized start"
                    )
                stored_revision = WorkflowRevision(bytes(stored_document))
                if stored_revision.revision_hash != request.revision_hash:
                    raise RuntimeError(
                        "published revision bytes disagree with their hash"
                    )
                if isinstance(graph, WorkflowGraph):
                    if not isinstance(request, StartPublishedRunRequest):
                        return DurableInvalidAgentBindings()
                    resolved_bindings: tuple[ResolvedAgentBinding, ...] = ()
                    binding_set: AgentBindingSet | None = None
                elif isinstance(graph, WorkflowGraphV2):
                    if not isinstance(request, StartPublishedRunRequestV2):
                        return DurableInvalidAgentBindings()
                    expected_roles = {
                        node.role
                        for node in graph.nodes
                        if isinstance(node, AgentNodeV2)
                    }
                    requested_roles = {
                        binding.role.value
                        for binding in request.agent_bindings.bindings
                    }
                    if expected_roles != requested_roles:
                        return DurableInvalidAgentBindings()
                    binding_set = request.agent_bindings
                    existing_record = (
                        connection.execute(
                            sa.select(runs).where(runs.c.run_id == request.run_id.value)
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if existing_record is not None:
                        if (
                            str(existing_record["revision_hash"])
                            != request.revision_hash.value
                            or int(existing_record["workflow_format_version"]) != 2
                            or str(existing_record["agent_binding_set_hash"])
                            != binding_set.binding_set_hash.value
                        ):
                            return DurableRunIdentityConflict()
                        return DurableRunExisting(
                            run_from_record_with_bindings(connection, existing_record)
                        )
                    resolved: list[ResolvedAgentBinding] = []
                    for binding in request.agent_bindings.bindings:
                        configuration_record = (
                            connection.execute(
                                sa.select(agent_configuration_revisions).where(
                                    agent_configuration_revisions.c.revision_hash
                                    == binding.agent_configuration_revision_hash.value
                                )
                            )
                            .mappings()
                            .one_or_none()
                        )
                        if configuration_record is None:
                            return DurableAgentConfigurationRevisionMissing()
                        configuration = agent_configuration_from_record(
                            configuration_record
                        )
                        auth_record = (
                            connection.execute(
                                sa.select(auth_profile_revisions).where(
                                    auth_profile_revisions.c.revision_hash
                                    == configuration.auth_profile_revision_hash.value
                                )
                            )
                            .mappings()
                            .one_or_none()
                        )
                        if auth_record is None:
                            raise RuntimeError(
                                "agent configuration auth profile is missing"
                            )
                        auth = auth_profile_from_record(auth_record)
                        executor_key = AgentExecutorKey(
                            auth.provider_id, configuration.executor_revision
                        )
                        if not self._agent_executor_registry.contains(executor_key):
                            return DurableAgentExecutorBindingUnavailable()
                        if (
                            configuration.requested_capability
                            not in self._agent_executor_registry.declared_capabilities(
                                executor_key
                            )
                        ):
                            return DurableAgentExecutorCapabilityUnavailable()
                        resolved.append(
                            ResolvedAgentBinding(binding.role, configuration, auth)
                        )
                    resolved_bindings = tuple(resolved)
                else:
                    assert_never(graph)

                workflow_id = bootstrap_workflow_id_for(request.run_id)
                inserted = connection.execute(
                    runs.insert()
                    .prefix_with("OR IGNORE")
                    .values(
                        run_id=request.run_id.value,
                        bootstrap_workflow_id=workflow_id,
                        revision_hash=request.revision_hash.value,
                        workflow_format_version=graph.format_version,
                        agent_binding_set_hash=(
                            None
                            if binding_set is None
                            else binding_set.binding_set_hash.value
                        ),
                        current_node_id=graph.start,
                        state=RunState.STARTED.value,
                        state_version=0,
                        last_event_sequence=0,
                        terminal_hash=None,
                    )
                )
                existing_record = (
                    connection.execute(
                        sa.select(runs).where(runs.c.run_id == request.run_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing_record is None:
                    raise RuntimeError("inserted run is not readable")
                if isinstance(graph, WorkflowGraph):
                    run = run_from_record(existing_record)
                else:
                    assert binding_set is not None
                    terminal_hash = existing_record["terminal_hash"]
                    run = RunV2(
                        request.run_id,
                        request.revision_hash,
                        binding_set.binding_set_hash,
                        resolved_bindings,
                        RunState(str(existing_record["state"])),
                        str(existing_record["current_node_id"]),
                        int(existing_record["state_version"]),
                        int(existing_record["last_event_sequence"]),
                        (
                            None
                            if terminal_hash is None
                            else Sha256Hash(str(terminal_hash))
                        ),
                    )
                if inserted.rowcount == 0:
                    existing_set = existing_record["agent_binding_set_hash"]
                    requested_set = (
                        None
                        if binding_set is None
                        else binding_set.binding_set_hash.value
                    )
                    if (
                        run.revision_hash != request.revision_hash
                        or int(existing_record["workflow_format_version"])
                        != graph.format_version
                        or existing_set != requested_set
                    ):
                        return DurableRunIdentityConflict()
                    return DurableRunExisting(run)
                if binding_set is not None and binding_set.bindings:
                    connection.execute(
                        run_agent_bindings.insert(),
                        [
                            {
                                "run_id": request.run_id.value,
                                "revision_hash": request.revision_hash.value,
                                "binding_set_hash": binding_set.binding_set_hash.value,
                                "role": binding.role.value,
                                "agent_configuration_revision_hash": (
                                    binding.agent_configuration_revision_hash.value
                                ),
                            }
                            for binding in binding_set.bindings
                        ],
                    )
                options: EnqueueOptions = {
                    "workflow_name": WORKFLOW_NAME,
                    "queue_name": QUEUE_NAME,
                    "workflow_id": workflow_id,
                    "app_version": self._settings.application_version,
                }
                client.enqueue_in_transaction(
                    connection,
                    options,
                    request.run_id.value,
                    request.revision_hash.value,
                )
                return DurableRunCreated(run)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
        finally:
            if client is not None:
                client.destroy()

    @staticmethod
    def _insert_or_verify_revision(
        connection: sa.Connection, request: StartRunRequest
    ) -> None:
        connection.execute(
            workflow_revisions.insert()
            .prefix_with("OR IGNORE")
            .values(
                revision_hash=request.revision.revision_hash.value,
                document=request.revision.document,
            )
        )
        stored = connection.scalar(
            sa.select(workflow_revisions.c.document).where(
                workflow_revisions.c.revision_hash
                == request.revision.revision_hash.value
            )
        )
        if stored != request.revision.document:
            raise RevisionHashCollision(
                "stored workflow revision bytes disagree with their hash"
            )

    @staticmethod
    def _existing_run(connection: sa.Connection, run_id: RunId) -> Run | None:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
            .mappings()
            .one_or_none()
        )
        if record is None:
            return None
        return run_from_record(record)


class DbosWorkflowRevisionPublisher:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def publish(self, revision: WorkflowRevision) -> DurableRevisionPublicationResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                inserted = connection.execute(
                    workflow_revisions.insert()
                    .prefix_with("OR IGNORE")
                    .values(
                        revision_hash=revision.revision_hash.value,
                        document=revision.document,
                    )
                )
                stored = connection.scalar(
                    sa.select(workflow_revisions.c.document).where(
                        workflow_revisions.c.revision_hash
                        == revision.revision_hash.value
                    )
                )
                if stored is None:
                    raise RuntimeError("inserted workflow revision is not readable")
                durable = WorkflowRevision(bytes(stored))
                if durable.revision_hash != revision.revision_hash:
                    return DurableStateCorrupt()
                if durable.document != revision.document:
                    return DurableRevisionCollision()
                if inserted.rowcount == 1:
                    return DurableRevisionCreated(durable)
                return DurableRevisionExisting(durable)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
