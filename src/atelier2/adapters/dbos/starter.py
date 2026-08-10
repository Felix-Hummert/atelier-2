from __future__ import annotations

import hashlib

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.runtime import DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs, workflow_revisions
from atelier2.adapters.dbos.workflow import QUEUE_NAME, WORKFLOW_NAME
from atelier2.contracts.runs import (
    RevisionHashCollision,
    Run,
    RunId,
    RunIdentityConflict,
    RunState,
    StartRunRequest,
    WorkflowRevisionHash,
)

WORKFLOW_ID_PREFIX = "atelier2-run-"


def dbos_workflow_id_for(run_id: RunId) -> str:
    return WORKFLOW_ID_PREFIX + hashlib.sha256(run_id.value.encode()).hexdigest()


class DbosDurableRunStarter:
    def __init__(self, engine: Engine, settings: DbosRuntimeSettings) -> None:
        self._engine = engine
        self._settings = settings

    def start(self, request: StartRunRequest) -> Run:
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

                workflow_id = dbos_workflow_id_for(request.run_id)
                connection.execute(
                    runs.insert().values(
                        run_id=request.run_id.value,
                        dbos_workflow_id=workflow_id,
                        revision_hash=request.revision.revision_hash.value,
                        state=RunState.STARTED.value,
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
                )
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
        record = connection.execute(
            sa.select(runs.c.revision_hash, runs.c.state).where(
                runs.c.run_id == run_id.value
            )
        ).one_or_none()
        if record is None:
            return None
        return Run(
            run_id,
            WorkflowRevisionHash(str(record.revision_hash)),
            RunState(str(record.state)),
        )
