from __future__ import annotations

import hashlib

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError

from atelier2.adapters.dbos.run_store import run_from_record
from atelier2.adapters.dbos.runtime import (
    DbosRuntimeSettings,
    canonical_write_transaction,
)
from atelier2.adapters.dbos.schema import runs, workflow_revisions
from atelier2.adapters.dbos.workflow import QUEUE_NAME, WORKFLOW_NAME
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.start_published_run import StartPublishedRunRequest
from atelier2.contracts.runs import (
    RevisionHashCollision,
    Run,
    RunId,
    RunIdentityConflict,
    RunState,
    StartRunRequest,
    WorkflowRevision,
)
from atelier2.ports.durable_runs import (
    DurablePublishedRunResult,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableStateCorrupt,
    DurableWriteUnavailable,
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
    def __init__(self, engine: Engine, settings: DbosRuntimeSettings) -> None:
        self._engine = engine
        self._settings = settings

    def start(self, request: StartRunRequest) -> Run:
        graph = parse_workflow_document(request.revision.document)
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
        self, request: StartPublishedRunRequest
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
            graph = parse_workflow_document(revision.document)
        except OperationalError:
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

        client = DBOSClient(
            system_database_engine=self._engine, use_listen_notify=False
        )
        try:
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
                workflow_id = bootstrap_workflow_id_for(request.run_id)
                inserted = connection.execute(
                    runs.insert()
                    .prefix_with("OR IGNORE")
                    .values(
                        run_id=request.run_id.value,
                        bootstrap_workflow_id=workflow_id,
                        revision_hash=request.revision_hash.value,
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
                run = run_from_record(existing_record)
                if inserted.rowcount == 0:
                    if run.revision_hash != request.revision_hash:
                        return DurableRunIdentityConflict()
                    return DurableRunExisting(run)
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
        except OperationalError:
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
        finally:
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
        except OperationalError:
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
