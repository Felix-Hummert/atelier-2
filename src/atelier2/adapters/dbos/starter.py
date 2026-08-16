from __future__ import annotations

import hashlib
from typing import assert_never

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Connection, Engine, RowMapping
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
    node_artifacts_v3,
    node_receipt_access_v3,
    node_receipt_outputs_v3,
    node_receipts_v3,
    published_revisions,
    run_agent_bindings,
    runs,
    workflow_revisions,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow import QUEUE_NAME, WORKFLOW_NAME
from atelier2.adapters.yaml_workflows import (
    InvalidWorkflowDocument,
    WorkflowFormatNotExecutable,
    parse_executable_workflow_document,
    parse_workflow_document,
)
from atelier2.contracts.agents import (
    AgentBindingSet,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    PersistedReceiptDisposition,
    ReceiptOutput,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.contracts.workflows import AgentNodeV2, WorkflowGraph, WorkflowGraphV2
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
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
    DurableRunFormatNotExecutable,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableStateCorrupt,
    DurableV3RunCreated,
    DurableV3RunExisting,
    DurableV3StartBindingInvalid,
    DurableV3StartConflict,
    DurableV3StartWithReceiptResult,
    DurableWriteUnavailable,
    StartPublishedRunRequest,
    StartPublishedRunRequestV2,
    StartV3RunWithReceiptRequest,
    V3StartRecord,
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


class _V3StartConflictError(Exception):
    def __init__(self, record: V3StartRecord) -> None:
        super().__init__(record.value)
        self.record = record


def _v3_start_is_bound(request: StartV3RunWithReceiptRequest) -> bool:
    revision = request.revision
    node_request = request.node_request
    receipt = request.receipt
    if revision.kind is not RevisionKind.WORKFLOW:
        return False
    if node_request.workflow_revision_hash.value != revision.revision_hash.value:
        return False
    try:
        graph = parse_workflow_document(revision.document)
        if not isinstance(graph, WorkflowGraphV3):
            return False
        node = graph.node(node_request.node_id)
    except (InvalidWorkflowDocument, KeyError, TypeError, ValueError):
        return False
    if node.type != node_request.kind.value or node.depends_on:
        return False
    expected_execution = NodeExecutionId.for_node(
        node_request.run_id,
        node_request.workflow_revision_hash,
        node_request.node_id,
    )
    if (
        receipt.node_execution_id != expected_execution
        or receipt.request_hash != node_request.request_hash
        or receipt.context_package_hash != node_request.context_package_hash
    ):
        return False
    artifacts = request.artifacts
    if len({artifact.output_name for artifact in artifacts}) != len(artifacts):
        return False
    if any(
        artifact.run_id != node_request.run_id
        or artifact.node_id != node_request.node_id
        or artifact.node_execution_id != expected_execution
        for artifact in artifacts
    ):
        return False
    declared_outputs = tuple(
        (output.name, output.schema_revision)
        for output in node_request.declared_outputs
    )
    authored_outputs = tuple(
        (output.name, output.schema_reference.revision) for output in node.outputs
    )
    if authored_outputs != tuple(
        (name, schema.value) for name, schema in declared_outputs
    ):
        return False
    expected_receipt_outputs = tuple(
        ReceiptOutput(
            artifact.output_name,
            artifact.schema_revision,
            artifact.value_hash,
        )
        for artifact in artifacts
    )
    if receipt.disposition is PersistedReceiptDisposition.SUCCEEDED:
        return (
            declared_outputs
            == tuple(
                (artifact.output_name, artifact.schema_revision)
                for artifact in artifacts
            )
            and receipt.outputs == expected_receipt_outputs
        )
    return not artifacts and not receipt.outputs


def _v3_run_record_matches(
    run_record: RowMapping, request: StartV3RunWithReceiptRequest
) -> bool:
    node_request = request.node_request
    return (
        str(run_record["run_id"]) == node_request.run_id.value
        and str(run_record["bootstrap_workflow_id"])
        == bootstrap_workflow_id_for(node_request.run_id)
        and str(run_record["revision_hash"]) == request.revision.revision_hash.value
        and int(str(run_record["workflow_format_version"])) == 3
        and run_record["agent_binding_set_hash"] is None
        and str(run_record["current_node_id"]) == node_request.node_id
        and str(run_record["state"]) == RunState.STARTED.value
        and int(str(run_record["state_version"])) == 0
        and int(str(run_record["last_event_sequence"])) == 0
        and run_record["terminal_hash"] is None
    )


def _stored_v3_start_matches(
    connection: Connection,
    request: StartV3RunWithReceiptRequest,
) -> bool:
    node_request = request.node_request
    receipt = request.receipt
    run_record = (
        connection.execute(
            sa.select(runs).where(runs.c.run_id == node_request.run_id.value)
        )
        .mappings()
        .one_or_none()
    )
    receipt_record = (
        connection.execute(
            sa.select(node_receipts_v3).where(
                node_receipts_v3.c.node_execution_id == receipt.node_execution_id.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if run_record is None or receipt_record is None:
        return False
    if not _v3_run_record_matches(run_record, request):
        return False
    if (
        str(receipt_record["disposition"]) != receipt.disposition.value
        or str(receipt_record["reason"]) != receipt.reason
        or str(receipt_record["request_hash"]) != receipt.request_hash.value
        or str(receipt_record["context_package_hash"])
        != receipt.context_package_hash.value
        or str(receipt_record["receipt_hash"]) != receipt.receipt_hash.value
    ):
        return False
    stored_artifacts = tuple(
        tuple(record)
        for record in connection.execute(
            sa.select(
                node_artifacts_v3.c.run_id,
                node_artifacts_v3.c.node_id,
                node_artifacts_v3.c.node_execution_id,
                node_artifacts_v3.c.output_name,
                node_artifacts_v3.c.schema_revision_hash,
                node_artifacts_v3.c.value,
                node_artifacts_v3.c.value_hash,
                node_artifacts_v3.c.artifact_hash,
            )
            .where(
                node_artifacts_v3.c.node_execution_id == receipt.node_execution_id.value
            )
            .order_by(node_artifacts_v3.c.output_name)
        )
    )
    expected_artifacts = tuple(
        sorted(
            (
                artifact.run_id.value,
                artifact.node_id,
                artifact.node_execution_id.value,
                artifact.output_name,
                artifact.schema_revision.value,
                artifact.value,
                artifact.value_hash.value,
                artifact.artifact_hash.value,
            )
            for artifact in request.artifacts
        )
    )
    stored_outputs = tuple(
        tuple(record)
        for record in connection.execute(
            sa.select(
                node_receipt_outputs_v3.c.position,
                node_receipt_outputs_v3.c.output_name,
                node_receipt_outputs_v3.c.schema_revision_hash,
                node_receipt_outputs_v3.c.value_hash,
            )
            .where(
                node_receipt_outputs_v3.c.node_execution_id
                == receipt.node_execution_id.value
            )
            .order_by(node_receipt_outputs_v3.c.position)
        )
    )
    expected_outputs = tuple(
        (
            position,
            output.name,
            output.schema_revision.value,
            output.value_hash.value,
        )
        for position, output in enumerate(receipt.outputs)
    )
    stored_access = tuple(
        tuple(record)
        for record in connection.execute(
            sa.select(
                node_receipt_access_v3.c.position,
                node_receipt_access_v3.c.access_receipt_hash,
            )
            .where(
                node_receipt_access_v3.c.node_execution_id
                == receipt.node_execution_id.value
            )
            .order_by(node_receipt_access_v3.c.position)
        )
    )
    expected_access = tuple(
        (position, access_hash.value)
        for position, access_hash in enumerate(receipt.access_receipt_hashes)
    )
    return (
        stored_artifacts == expected_artifacts
        and stored_outputs == expected_outputs
        and stored_access == expected_access
    )


class DbosDurableRunStarter:
    def __init__(
        self,
        engine: Engine,
        settings: DbosRuntimeSettings,
        agent_executor_registry: AgentExecutorRegistry,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._agent_executor_registry = agent_executor_registry

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
        except WorkflowFormatNotExecutable:
            return DurableRunFormatNotExecutable()
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

    def start_v3_with_receipt(
        self, request: StartV3RunWithReceiptRequest
    ) -> DurableV3StartWithReceiptResult:
        """Persist one supervised V3 start without claiming an executable engine."""
        if not _v3_start_is_bound(request):
            return DurableV3StartBindingInvalid()
        revision = request.revision
        node_request = request.node_request
        receipt = request.receipt
        try:
            with canonical_write_transaction(self._engine) as connection:
                matching_runs = (
                    connection.execute(
                        sa.select(runs).where(
                            sa.or_(
                                runs.c.run_id == node_request.run_id.value,
                                runs.c.bootstrap_workflow_id
                                == bootstrap_workflow_id_for(node_request.run_id),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                if len(matching_runs) > 1:
                    raise _V3StartConflictError(V3StartRecord.RUN)
                existing_run = matching_runs[0] if matching_runs else None
                if existing_run is not None:
                    if not _v3_run_record_matches(existing_run, request):
                        raise _V3StartConflictError(V3StartRecord.RUN)
                    stored_revision = (
                        connection.execute(
                            sa.select(published_revisions).where(
                                published_revisions.c.kind == revision.kind.value,
                                published_revisions.c.revision_hash
                                == revision.revision_hash.value,
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    stored_backing = (
                        connection.execute(
                            sa.select(workflow_revisions).where(
                                workflow_revisions.c.revision_hash
                                == revision.revision_hash.value
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if (
                        stored_revision is None
                        or bytes(stored_revision["document"]) != revision.document
                    ):
                        raise _V3StartConflictError(V3StartRecord.PUBLISHED_REVISION)
                    if (
                        stored_backing is None
                        or bytes(stored_backing["document"]) != revision.document
                    ):
                        raise _V3StartConflictError(V3StartRecord.WORKFLOW_BACKING)
                    if not _stored_v3_start_matches(connection, request):
                        raise _V3StartConflictError(V3StartRecord.RUN)
                    return DurableV3RunExisting(
                        node_request.run_id,
                        revision.revision_hash,
                        receipt.receipt_hash,
                    )

                receipt_collision = connection.scalar(
                    sa.select(sa.literal(True)).where(
                        sa.exists(
                            sa.select(node_receipts_v3.c.node_execution_id).where(
                                sa.or_(
                                    node_receipts_v3.c.node_execution_id
                                    == receipt.node_execution_id.value,
                                    node_receipts_v3.c.receipt_hash
                                    == receipt.receipt_hash.value,
                                )
                            )
                        )
                    )
                )
                if receipt_collision:
                    raise _V3StartConflictError(V3StartRecord.RECEIPT)
                artifact_hashes = tuple(
                    artifact.artifact_hash.value for artifact in request.artifacts
                )
                artifact_collision = connection.scalar(
                    sa.select(sa.literal(True)).where(
                        sa.exists(
                            sa.select(node_artifacts_v3.c.node_execution_id).where(
                                sa.or_(
                                    node_artifacts_v3.c.node_execution_id
                                    == receipt.node_execution_id.value,
                                    node_artifacts_v3.c.artifact_hash.in_(
                                        artifact_hashes or ("",)
                                    ),
                                )
                            )
                        )
                    )
                )
                if artifact_collision:
                    raise _V3StartConflictError(V3StartRecord.ARTIFACT)

                connection.execute(
                    published_revisions.insert()
                    .prefix_with("OR IGNORE")
                    .values(
                        kind=revision.kind.value,
                        revision_hash=revision.revision_hash.value,
                        document=revision.document,
                    )
                )
                stored_revision = (
                    connection.execute(
                        sa.select(published_revisions).where(
                            published_revisions.c.kind == revision.kind.value,
                            published_revisions.c.revision_hash
                            == revision.revision_hash.value,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    stored_revision is None
                    or bytes(stored_revision["document"]) != revision.document
                ):
                    raise _V3StartConflictError(V3StartRecord.PUBLISHED_REVISION)

                connection.execute(
                    workflow_revisions.insert()
                    .prefix_with("OR IGNORE")
                    .values(
                        revision_hash=revision.revision_hash.value,
                        document=revision.document,
                    )
                )
                stored_backing = (
                    connection.execute(
                        sa.select(workflow_revisions).where(
                            workflow_revisions.c.revision_hash
                            == revision.revision_hash.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    stored_backing is None
                    or bytes(stored_backing["document"]) != revision.document
                ):
                    raise _V3StartConflictError(V3StartRecord.WORKFLOW_BACKING)

                connection.execute(
                    runs.insert().values(
                        run_id=node_request.run_id.value,
                        bootstrap_workflow_id=bootstrap_workflow_id_for(
                            node_request.run_id
                        ),
                        revision_hash=revision.revision_hash.value,
                        workflow_format_version=3,
                        agent_binding_set_hash=None,
                        current_node_id=node_request.node_id,
                        state=RunState.STARTED.value,
                        state_version=0,
                        last_event_sequence=0,
                        terminal_hash=None,
                    )
                )
                if request.artifacts:
                    connection.execute(
                        node_artifacts_v3.insert(),
                        [
                            {
                                "run_id": artifact.run_id.value,
                                "node_id": artifact.node_id,
                                "node_execution_id": artifact.node_execution_id.value,
                                "output_name": artifact.output_name,
                                "schema_revision_hash": artifact.schema_revision.value,
                                "value": artifact.value,
                                "value_hash": artifact.value_hash.value,
                                "artifact_hash": artifact.artifact_hash.value,
                            }
                            for artifact in request.artifacts
                        ],
                    )
                connection.execute(
                    node_receipts_v3.insert().values(
                        node_execution_id=receipt.node_execution_id.value,
                        disposition=receipt.disposition.value,
                        reason=receipt.reason,
                        request_hash=receipt.request_hash.value,
                        context_package_hash=receipt.context_package_hash.value,
                        receipt_hash=receipt.receipt_hash.value,
                    )
                )
                if receipt.outputs:
                    connection.execute(
                        node_receipt_outputs_v3.insert(),
                        [
                            {
                                "node_execution_id": receipt.node_execution_id.value,
                                "position": position,
                                "output_name": output.name,
                                "schema_revision_hash": output.schema_revision.value,
                                "value_hash": output.value_hash.value,
                            }
                            for position, output in enumerate(receipt.outputs)
                        ],
                    )
                if receipt.access_receipt_hashes:
                    connection.execute(
                        node_receipt_access_v3.insert(),
                        [
                            {
                                "node_execution_id": receipt.node_execution_id.value,
                                "position": position,
                                "access_receipt_hash": access_hash.value,
                            }
                            for position, access_hash in enumerate(
                                receipt.access_receipt_hashes
                            )
                        ],
                    )
                if not _stored_v3_start_matches(connection, request):
                    raise _V3StartConflictError(V3StartRecord.RECEIPT)
                return DurableV3RunCreated(
                    node_request.run_id,
                    revision.revision_hash,
                    receipt.receipt_hash,
                )
        except _V3StartConflictError as conflict:
            return DurableV3StartConflict(conflict.record)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()


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
