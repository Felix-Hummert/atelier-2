from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.runtime import (
    DbosRuntimeSettings,
    create_canonical_engine,
)
from atelier2.adapters.dbos.schema import (
    initialize_schema,
    node_artifacts_v3,
    node_receipt_access_v3,
    node_receipt_outputs_v3,
    node_receipts_v3,
    published_revisions,
    run_events,
    runs,
    workflow_revisions,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    bootstrap_workflow_id_for,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    BoundNodeRevisions,
    ContextPackage,
    DeclaredOutput,
    NodeArtifact,
    NodeExecutionRequest,
    NodeKindV3,
    NodeReceipt,
    PersistedReceiptDisposition,
    ReceiptOutput,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.durable_runs import (
    DurableStateCorrupt,
    DurableV3RunCreated,
    DurableV3RunExisting,
    DurableV3StartBindingInvalid,
    DurableV3StartConflict,
    StartV3RunWithReceiptRequest,
    V3StartRecord,
)

SCHEMA_REVISION = PublishedRevisionHash.of(b"the bound meal schema")
SAUCE_SCHEMA_REVISION = PublishedRevisionHash.of(b"the bound sauce schema")
WORKFLOW_DOCUMENT = f"""format_version: 3
name: Lasagne
description: Cook one supervised proof.
nodes:
  - id: cook
    type: action
    operation:
      ref: cook-operation
      revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    outputs:
      - name: meal
        schema:
          ref: meal-schema
          revision: {SCHEMA_REVISION.value}
      - name: sauce
        schema:
          ref: sauce-schema
          revision: {SAUCE_SCHEMA_REVISION.value}
""".encode()


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[tuple[Engine, DbosDurableRunStarter]]:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    try:
        yield (
            engine,
            DbosDurableRunStarter(
                engine,
                DbosRuntimeSettings(database, "cut-b-test"),
                AgentExecutorRegistry(),
            ),
        )
    finally:
        engine.dispose()


def request(
    *,
    revision: PublishedRevision | None = None,
    receipt_execution_id: NodeExecutionId | None = None,
) -> StartV3RunWithReceiptRequest:
    published = revision or PublishedRevision(RevisionKind.WORKFLOW, WORKFLOW_DOCUMENT)
    workflow_hash = WorkflowRevisionHash(published.revision_hash.value)
    run_id = RunId("run-lasagne")
    context = ContextPackage(b"one supervised context")
    node_request = NodeExecutionRequest(
        workflow_revision_hash=workflow_hash,
        run_configuration_revision_hash=RunConfigurationRevisionHash.of(
            b"one exact run configuration"
        ),
        run_id=run_id,
        node_id="cook",
        context_package_hash=context.package_hash,
        available_context=(),
        kind=NodeKindV3.ACTION,
        mode=None,
        inputs=(),
        bound_revisions=BoundNodeRevisions(),
        declared_outputs=(
            DeclaredOutput("meal", SCHEMA_REVISION),
            DeclaredOutput("sauce", SAUCE_SCHEMA_REVISION),
        ),
    )
    artifact = NodeArtifact(
        run_id=run_id,
        node_id="cook",
        node_execution_id=NodeExecutionId.for_node(run_id, workflow_hash, "cook"),
        output_name="meal",
        schema_revision=SCHEMA_REVISION,
        value=b"lasagne",
    )
    sauce_artifact = NodeArtifact(
        run_id=run_id,
        node_id="cook",
        node_execution_id=NodeExecutionId.for_node(run_id, workflow_hash, "cook"),
        output_name="sauce",
        schema_revision=SAUCE_SCHEMA_REVISION,
        value=b"tomato",
    )
    receipt = NodeReceipt(
        node_execution_id=receipt_execution_id
        or NodeExecutionId.for_node(run_id, workflow_hash, "cook"),
        disposition=PersistedReceiptDisposition.SUCCEEDED,
        reason="completed",
        request_hash=node_request.request_hash,
        context_package_hash=context.package_hash,
        outputs=(
            ReceiptOutput("meal", SCHEMA_REVISION, artifact.value_hash),
            ReceiptOutput("sauce", SAUCE_SCHEMA_REVISION, sauce_artifact.value_hash),
        ),
        access_receipt_hashes=(
            Sha256Hash.of(b"ingredients access"),
            Sha256Hash.of(b"oven access"),
        ),
    )
    return StartV3RunWithReceiptRequest(
        published, node_request, (artifact, sauce_artifact), receipt
    )


def table_count(engine: Engine, table: sa.Table) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(sa.select(sa.func.count()).select_from(table)) or 0
        )


def test_v3_start_writes_exact_revision_run_and_receipt_atomically(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    engine, starter = storage
    exact = request()

    result = starter.start_v3_with_receipt(exact)

    assert result == DurableV3RunCreated(
        exact.node_request.run_id,
        exact.revision.revision_hash,
        exact.receipt.receipt_hash,
    )
    with engine.connect() as connection:
        assert connection.execute(
            sa.select(
                published_revisions.c.kind,
                published_revisions.c.revision_hash,
                published_revisions.c.document,
            )
        ).one() == (
            RevisionKind.WORKFLOW.value,
            exact.revision.revision_hash.value,
            WORKFLOW_DOCUMENT,
        )
        assert connection.execute(
            sa.select(
                workflow_revisions.c.revision_hash,
                workflow_revisions.c.document,
            )
        ).one() == (
            exact.revision.revision_hash.value,
            WORKFLOW_DOCUMENT,
        )
        assert connection.execute(
            sa.select(
                runs.c.run_id,
                runs.c.revision_hash,
                runs.c.workflow_format_version,
                runs.c.current_node_id,
                runs.c.state,
                runs.c.terminal_hash,
            )
        ).one() == (
            "run-lasagne",
            exact.revision.revision_hash.value,
            3,
            "cook",
            "STARTED",
            None,
        )
        assert connection.execute(
            sa.select(
                node_artifacts_v3.c.run_id,
                node_artifacts_v3.c.node_id,
                node_artifacts_v3.c.node_execution_id,
                node_artifacts_v3.c.output_name,
                node_artifacts_v3.c.schema_revision_hash,
                node_artifacts_v3.c.value,
                node_artifacts_v3.c.value_hash,
                node_artifacts_v3.c.artifact_hash,
            ).order_by(node_artifacts_v3.c.output_name)
        ).all() == [
            (
                "run-lasagne",
                "cook",
                exact.receipt.node_execution_id.value,
                "meal",
                SCHEMA_REVISION.value,
                b"lasagne",
                exact.artifacts[0].value_hash.value,
                exact.artifacts[0].artifact_hash.value,
            ),
            (
                "run-lasagne",
                "cook",
                exact.receipt.node_execution_id.value,
                "sauce",
                SAUCE_SCHEMA_REVISION.value,
                b"tomato",
                exact.artifacts[1].value_hash.value,
                exact.artifacts[1].artifact_hash.value,
            ),
        ]
        assert connection.execute(
            sa.select(
                node_receipts_v3.c.node_execution_id,
                node_receipts_v3.c.disposition,
                node_receipts_v3.c.reason,
                node_receipts_v3.c.request_hash,
                node_receipts_v3.c.context_package_hash,
                node_receipts_v3.c.receipt_hash,
            )
        ).one() == (
            exact.receipt.node_execution_id.value,
            "succeeded",
            "completed",
            exact.node_request.request_hash.value,
            exact.node_request.context_package_hash.value,
            exact.receipt.receipt_hash.value,
        )
        assert connection.execute(
            sa.select(
                node_receipt_outputs_v3.c.node_execution_id,
                node_receipt_outputs_v3.c.position,
                node_receipt_outputs_v3.c.output_name,
                node_receipt_outputs_v3.c.schema_revision_hash,
                node_receipt_outputs_v3.c.value_hash,
            ).order_by(node_receipt_outputs_v3.c.position)
        ).all() == [
            (
                exact.receipt.node_execution_id.value,
                0,
                "meal",
                SCHEMA_REVISION.value,
                exact.artifacts[0].value_hash.value,
            ),
            (
                exact.receipt.node_execution_id.value,
                1,
                "sauce",
                SAUCE_SCHEMA_REVISION.value,
                exact.artifacts[1].value_hash.value,
            ),
        ]
        assert connection.execute(
            sa.select(
                node_receipt_access_v3.c.position,
                node_receipt_access_v3.c.access_receipt_hash,
            ).order_by(node_receipt_access_v3.c.position)
        ).all() == [
            (0, exact.receipt.access_receipt_hashes[0].value),
            (1, exact.receipt.access_receipt_hashes[1].value),
        ]
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(run_events)) == 0
        )


def test_identical_v3_start_retry_returns_the_same_exact_binding(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    engine, starter = storage
    exact = request()

    starter.start_v3_with_receipt(exact)
    retried = starter.start_v3_with_receipt(exact)

    assert retried == DurableV3RunExisting(
        exact.node_request.run_id,
        exact.revision.revision_hash,
        exact.receipt.receipt_hash,
    )
    assert table_count(engine, published_revisions) == 1
    assert table_count(engine, workflow_revisions) == 1
    assert table_count(engine, runs) == 1
    assert table_count(engine, node_artifacts_v3) == 2
    assert table_count(engine, node_receipts_v3) == 1
    assert table_count(engine, node_receipt_outputs_v3) == 2
    assert table_count(engine, node_receipt_access_v3) == 2


def test_receipt_for_another_node_execution_is_refused_without_a_write(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    engine, starter = storage
    exact = request(receipt_execution_id=NodeExecutionId(Sha256Hash.of(b"other").value))

    assert isinstance(
        starter.start_v3_with_receipt(exact), DurableV3StartBindingInvalid
    )
    assert table_count(engine, published_revisions) == 0
    assert table_count(engine, workflow_revisions) == 0
    assert table_count(engine, runs) == 0
    assert table_count(engine, node_artifacts_v3) == 0
    assert table_count(engine, node_receipts_v3) == 0
    assert table_count(engine, node_receipt_outputs_v3) == 0
    assert table_count(engine, node_receipt_access_v3) == 0


def test_non_success_receipt_cannot_bind_artifacts_or_write_partial_state(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    engine, starter = storage
    exact = request()
    failed_receipt = NodeReceipt(
        node_execution_id=exact.receipt.node_execution_id,
        disposition=PersistedReceiptDisposition.FAILED,
        reason="supervised operation failed",
        request_hash=exact.receipt.request_hash,
        context_package_hash=exact.receipt.context_package_hash,
        outputs=(),
        access_receipt_hashes=exact.receipt.access_receipt_hashes,
    )

    assert isinstance(
        starter.start_v3_with_receipt(replace(exact, receipt=failed_receipt)),
        DurableV3StartBindingInvalid,
    )
    assert table_count(engine, runs) == 0
    assert table_count(engine, node_artifacts_v3) == 0
    assert table_count(engine, node_receipts_v3) == 0


@pytest.mark.parametrize(
    "disposition",
    (
        PersistedReceiptDisposition.FAILED,
        PersistedReceiptDisposition.CANCELLED,
        PersistedReceiptDisposition.BLOCKED,
    ),
)
def test_non_success_receipt_without_artifacts_is_stored_atomically(
    storage: tuple[Engine, DbosDurableRunStarter],
    disposition: PersistedReceiptDisposition,
) -> None:
    engine, starter = storage
    exact = request()
    terminal_receipt = NodeReceipt(
        node_execution_id=exact.receipt.node_execution_id,
        disposition=disposition,
        reason=f"supervised operation {disposition.value}",
        request_hash=exact.receipt.request_hash,
        context_package_hash=exact.receipt.context_package_hash,
        outputs=(),
        access_receipt_hashes=exact.receipt.access_receipt_hashes,
    )
    terminal = replace(exact, artifacts=(), receipt=terminal_receipt)

    assert starter.start_v3_with_receipt(terminal) == DurableV3RunCreated(
        terminal.node_request.run_id,
        terminal.revision.revision_hash,
        terminal.receipt.receipt_hash,
    )
    with engine.connect() as connection:
        assert connection.scalar(sa.select(node_receipts_v3.c.disposition)) == (
            disposition.value
        )
    assert table_count(engine, runs) == 1
    assert table_count(engine, node_artifacts_v3) == 0
    assert table_count(engine, node_receipts_v3) == 1
    assert table_count(engine, node_receipt_outputs_v3) == 0
    assert table_count(engine, node_receipt_access_v3) == 2


def test_artifact_schema_must_match_the_authored_and_requested_output(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    engine, starter = storage
    exact = request()
    mismatched = NodeArtifact(
        run_id=exact.artifacts[0].run_id,
        node_id=exact.artifacts[0].node_id,
        node_execution_id=exact.artifacts[0].node_execution_id,
        output_name=exact.artifacts[0].output_name,
        schema_revision=PublishedRevisionHash.of(b"different schema"),
        value=exact.artifacts[0].value,
    )

    assert isinstance(
        starter.start_v3_with_receipt(replace(exact, artifacts=(mismatched,))),
        DurableV3StartBindingInvalid,
    )
    assert table_count(engine, runs) == 0
    assert table_count(engine, node_artifacts_v3) == 0
    assert table_count(engine, node_receipts_v3) == 0


def test_receipt_output_order_must_match_the_authored_artifact_order(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    engine, starter = storage
    exact = request()
    reordered_receipt = NodeReceipt(
        node_execution_id=exact.receipt.node_execution_id,
        disposition=exact.receipt.disposition,
        reason=exact.receipt.reason,
        request_hash=exact.receipt.request_hash,
        context_package_hash=exact.receipt.context_package_hash,
        outputs=tuple(reversed(exact.receipt.outputs)),
        access_receipt_hashes=exact.receipt.access_receipt_hashes,
    )

    assert isinstance(
        starter.start_v3_with_receipt(replace(exact, receipt=reordered_receipt)),
        DurableV3StartBindingInvalid,
    )
    assert table_count(engine, runs) == 0
    assert table_count(engine, node_artifacts_v3) == 0
    assert table_count(engine, node_receipts_v3) == 0


def test_published_revision_identity_collision_is_a_typed_conflict(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    engine, starter = storage
    exact = request()
    with engine.begin() as connection:
        connection.execute(
            published_revisions.insert().values(
                kind=exact.revision.kind.value,
                revision_hash=exact.revision.revision_hash.value,
                document=b"different bytes under the same identity",
            )
        )

    assert starter.start_v3_with_receipt(exact) == DurableV3StartConflict(
        V3StartRecord.PUBLISHED_REVISION
    )
    assert table_count(engine, workflow_revisions) == 0
    assert table_count(engine, runs) == 0
    assert table_count(engine, node_artifacts_v3) == 0
    assert table_count(engine, node_receipts_v3) == 0


def test_existing_run_under_another_revision_is_a_run_conflict(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    engine, starter = storage
    exact = request()
    other_revision = PublishedRevision(
        RevisionKind.WORKFLOW,
        exact.revision.document.replace(b"Lasagne", b"Moussaka"),
    )
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=other_revision.revision_hash.value,
                document=other_revision.document,
            )
        )
        connection.execute(
            runs.insert().values(
                run_id=exact.node_request.run_id.value,
                bootstrap_workflow_id=bootstrap_workflow_id_for(
                    exact.node_request.run_id
                ),
                revision_hash=other_revision.revision_hash.value,
                workflow_format_version=3,
                agent_binding_set_hash=None,
                current_node_id=exact.node_request.node_id,
                state=RunState.STARTED.value,
                state_version=0,
                last_event_sequence=0,
                terminal_hash=None,
            )
        )

    assert starter.start_v3_with_receipt(exact) == DurableV3StartConflict(
        V3StartRecord.RUN
    )
    assert table_count(engine, published_revisions) == 0
    assert table_count(engine, node_artifacts_v3) == 0
    assert table_count(engine, node_receipts_v3) == 0


def test_split_run_identity_collision_is_a_typed_conflict(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    engine, starter = storage
    exact = request()
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=exact.revision.revision_hash.value,
                document=exact.revision.document,
            )
        )
        connection.execute(
            runs.insert(),
            [
                {
                    "run_id": exact.node_request.run_id.value,
                    "bootstrap_workflow_id": "different-bootstrap",
                    "revision_hash": exact.revision.revision_hash.value,
                    "workflow_format_version": 3,
                    "agent_binding_set_hash": None,
                    "current_node_id": exact.node_request.node_id,
                    "state": RunState.STARTED.value,
                    "state_version": 0,
                    "last_event_sequence": 0,
                    "terminal_hash": None,
                },
                {
                    "run_id": "different-run",
                    "bootstrap_workflow_id": bootstrap_workflow_id_for(
                        exact.node_request.run_id
                    ),
                    "revision_hash": exact.revision.revision_hash.value,
                    "workflow_format_version": 3,
                    "agent_binding_set_hash": None,
                    "current_node_id": exact.node_request.node_id,
                    "state": RunState.STARTED.value,
                    "state_version": 0,
                    "last_event_sequence": 0,
                    "terminal_hash": None,
                },
            ],
        )

    assert starter.start_v3_with_receipt(exact) == DurableV3StartConflict(
        V3StartRecord.RUN
    )
    assert table_count(engine, node_artifacts_v3) == 0
    assert table_count(engine, node_receipts_v3) == 0


@pytest.mark.parametrize(
    "failpoint",
    (
        "published_revisions",
        "workflow_revisions",
        "runs",
        "node_artifacts_v3",
        "node_receipts_v3",
        "node_receipt_outputs_v3",
        "node_receipt_access_v3",
    ),
)
def test_every_v3_start_write_failure_rolls_the_exact_set_back(
    storage: tuple[Engine, DbosDurableRunStarter], failpoint: str
) -> None:
    engine, starter = storage
    exact = request()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TRIGGER fail_{failpoint} BEFORE INSERT ON {failpoint} "
            "BEGIN SELECT RAISE(ABORT, 'injected cut-b failure'); END"
        )

    assert isinstance(starter.start_v3_with_receipt(exact), DurableStateCorrupt)
    assert table_count(engine, published_revisions) == 0
    assert table_count(engine, workflow_revisions) == 0
    assert table_count(engine, runs) == 0
    assert table_count(engine, node_artifacts_v3) == 0
    assert table_count(engine, node_receipts_v3) == 0
    assert table_count(engine, node_receipt_outputs_v3) == 0
    assert table_count(engine, node_receipt_access_v3) == 0
