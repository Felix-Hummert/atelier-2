"""The offline hop a live V13 store needed and did not have.

The V13 fixture is a real predecessor store: the current create path, every later
addition removed, then a format-3 run that already wrote one event. That is the
#240 Z2 method — predecessor schema, not a version-row stub — expressed through
today's owner.

V14 and V15 each added a table, so dropping those tables was the whole reversal.
V16 changes `run_events` itself, V17 changes `agent_attempts`, and V18 changes
`runs`, so the fixture also restores those tables' published predecessor shapes
below. The literals are not second owners of the current tables: they are the
frozen artifacts the predecessor versions really carried, and the pinned V13
fingerprint refuses them the moment a character drifts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateIndex

from atelier2.adapters.dbos.run_store import event_from_record
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    _PRODUCT_TRIGGERS,
    PRODUCT_SCHEMA_HANDOFF,
    SCHEMA_VERSION,
    V13_SCHEMA_HANDOFF,
    MigrationRequired,
    _require_product_shape,
    agent_attempts,
    artifacts,
    atelier_schema_versions,
    catalog_lineage_members,
    catalog_lineages,
    context_packages_v3,
    initialize_schema,
    node_execution_requests_v3,
    node_receipts_v3,
    published_revisions,
    run_configuration_revisions,
    run_events,
    run_inputs_v3,
    runs,
    tool_redemptions,
    workflow_revisions,
)
from atelier2.contracts.catalog_v3 import CatalogLineage
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.host import main

ARCHIVED_RUN_ID = "live/erster-lauf-nach-der-nacht"
ARCHIVED_NODE_ID = "cook"
ARCHIVED_OUTPUT = b"lasagne, aufgetragen"

_PREDECESSOR_RUN_EVENTS_DDL = """
CREATE TABLE run_events (
    run_id TEXT NOT NULL,
    revision_hash TEXT NOT NULL,
    event_sequence INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    node_execution_id TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    payload BLOB NOT NULL,
    payload_hash TEXT NOT NULL,
    receipt_logical_key TEXT,
    receipt_result_hash TEXT,
    event_hash TEXT NOT NULL,
    agent_attempt_id TEXT,
    attempt_ordinal INTEGER,
    cancellation_command_id TEXT,
    replacement TEXT,
    cancellation_disposition TEXT,
    replacement_attempt_id TEXT,
    PRIMARY KEY (run_id, event_sequence),
    FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash),
    FOREIGN KEY(receipt_logical_key, run_id, revision_hash, receipt_result_hash) REFERENCES effect_receipts (logical_key, run_id, workflow_revision_hash, result_hash),
    CHECK (event_sequence > 0),
    CHECK (length(node_id) > 0),
    CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED', 'AGENT_CANCEL_REQUESTED', 'AGENT_CANCELLED', 'AGENT_INTERRUPTED', 'ACTION_RECONCILIATION_REQUIRED', 'ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED', 'WAITING_INPUT', 'WAIT_ANSWERED', 'SUBWORKFLOW_COMPLETED')),
    CHECK (length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK ((event_kind IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NOT NULL AND length(receipt_logical_key) > 0 AND receipt_result_hash IS NOT NULL AND length(receipt_result_hash) = 64 AND receipt_result_hash NOT GLOB '*[^0-9a-f]*' AND receipt_result_hash = payload_hash) OR (event_kind NOT IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NULL AND receipt_result_hash IS NULL)),
    CHECK ((agent_attempt_id IS NULL AND attempt_ordinal IS NULL AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (length(agent_attempt_id) = 64 AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' AND attempt_ordinal IN (1, 2) AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind = 'AGENT_CANCEL_REQUESTED' AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NOT NULL))))
)
"""


_PREDECESSOR_AGENT_ATTEMPTS_DDL = """
CREATE TABLE agent_attempts (
    attempt_id TEXT NOT NULL,
    node_execution_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    executor_operational_identity TEXT NOT NULL,
    run_id TEXT NOT NULL,
    workflow_revision_hash TEXT NOT NULL,
    node_id TEXT NOT NULL,
    attempt_ordinal INTEGER NOT NULL,
    state TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    process_phase TEXT NOT NULL,
    process_owner_id TEXT,
    watchdog_generation_id TEXT,
    cancellation_command_id TEXT,
    cancellation_expected_state_version INTEGER,
    replacement TEXT,
    redrive_state TEXT,
    cancellation_disposition TEXT,
    cancellation_workflow_id TEXT,
    failure_code TEXT,
    receipt_hash TEXT,
    PRIMARY KEY (attempt_id),
    UNIQUE (node_execution_id, attempt_ordinal),
    FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),
    CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024),
    CHECK (length(run_id) > 0),
    CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(node_id) BETWEEN 1 AND 1024),
    CHECK (attempt_ordinal IN (1, 2)),
    CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')),
    CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)),
    CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))),
    CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND process_phase = 'CLEANUP_ATTESTED' AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code = 'PROCESS_EXITED_UNSUCCESSFULLY' AND receipt_hash IS NULL)),
    UNIQUE (cancellation_workflow_id),
    UNIQUE (receipt_hash),
    FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)
"""

_PREDECESSOR_AGENT_ATTEMPTS_TRIGGER_DDL = """
CREATE TRIGGER agent_attempts_state_transition
BEFORE UPDATE ON agent_attempts
WHEN NOT (
  OLD.attempt_id = NEW.attempt_id
  AND OLD.node_execution_id = NEW.node_execution_id
  AND OLD.request_hash = NEW.request_hash
  AND OLD.executor_operational_identity = NEW.executor_operational_identity
  AND OLD.run_id = NEW.run_id
  AND OLD.workflow_revision_hash = NEW.workflow_revision_hash
  AND OLD.node_id = NEW.node_id
  AND OLD.attempt_ordinal = NEW.attempt_ordinal
  AND NEW.state_version > OLD.state_version
  AND (
    (OLD.state = 'PREPARED' AND OLD.state_version = 0
     AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
     AND NEW.state = 'PREPARED' AND NEW.state_version = 1
     AND NEW.process_phase = 'WATCHDOG_READY'
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
     AND NEW.cancellation_command_id IS NULL)
    OR
    (OLD.state = 'PREPARED'
     AND NEW.state = 'LAUNCH_ARMED'
     AND NEW.process_phase IN ('NONE', 'LAUNCH_AUTHORIZED')
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
     AND NEW.cancellation_command_id IS NULL)
    OR
    (OLD.state = 'LAUNCH_ARMED'
     AND OLD.process_phase = 'LAUNCH_AUTHORIZED'
     AND NEW.state = 'LAUNCH_ARMED'
     AND NEW.process_phase = 'PROCESS_OBSERVED'
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
     AND NEW.cancellation_command_id IS NULL)
    OR
    (OLD.state = 'LAUNCH_ARMED'
     AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
     AND NEW.state = 'SUCCEEDED'
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NOT NULL
     AND NEW.cancellation_command_id IS NULL
     AND EXISTS (
       SELECT 1 FROM agent_receipts_v2 AS receipt
       WHERE receipt.receipt_hash = NEW.receipt_hash
         AND receipt.request_hash = NEW.request_hash
         AND receipt.executor_operational_identity = NEW.executor_operational_identity
         AND receipt.node_execution_id = NEW.node_execution_id
         AND receipt.run_id = NEW.run_id
         AND receipt.workflow_revision_hash = NEW.workflow_revision_hash
         AND receipt.node_id = NEW.node_id
     ))
    OR
    (OLD.state = 'LAUNCH_ARMED'
     AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
     AND NEW.state = 'FAILED'
     AND NEW.failure_code = 'PROCESS_EXITED_UNSUCCESSFULLY'
     AND NEW.receipt_hash IS NULL
     AND NEW.cancellation_command_id IS NULL)
    OR
    (OLD.state IN ('PREPARED', 'LAUNCH_ARMED')
     AND OLD.cancellation_command_id IS NULL
     AND NEW.state = 'CANCEL_REQUESTED'
     AND NEW.cancellation_command_id IS NOT NULL
     AND NEW.cancellation_expected_state_version = OLD.state_version
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
    OR
    (OLD.state = 'CANCEL_REQUESTED'
     AND NEW.state = 'CANCEL_REQUESTED'
     AND OLD.cancellation_command_id = NEW.cancellation_command_id
     AND NEW.redrive_state = 'OWNER_NOT_LOCAL'
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
    OR
    (OLD.state = 'CANCEL_REQUESTED'
     AND NEW.state IN ('CANCELLED', 'INTERRUPTED')
     AND OLD.cancellation_command_id = NEW.cancellation_command_id
     AND NEW.process_phase = 'CLEANUP_ATTESTED'
     AND NEW.redrive_state = 'CLEANUP_ATTESTED'
     AND NEW.cancellation_disposition IS NOT NULL
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
  )
) BEGIN
  SELECT RAISE(ABORT, 'invalid agent attempt transition');
END
"""

ARCHIVED_ATTEMPT_ID = "ab" * 32
ARCHIVED_ATTEMPT_FAILURE_CODE = "PROCESS_EXITED_UNSUCCESSFULLY"


def _logical_dump(database_path: Path) -> tuple[str, ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(connection.iterdump())


def _restore_predecessor_run_events(connection: Connection) -> None:
    triggers = ("run_events_no_update", "run_events_no_delete")
    indexes = sorted(run_events.indexes, key=lambda index: index.name or "")
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    for index in indexes:
        connection.execute(sa.text(f"DROP INDEX {index.name}"))
    connection.execute(sa.text("DROP TABLE run_events"))
    connection.execute(sa.text(_PREDECESSOR_RUN_EVENTS_DDL))
    for index in indexes:
        connection.execute(CreateIndex(index))
    for trigger in triggers:
        connection.execute(sa.text(_PRODUCT_TRIGGERS[trigger]))


_PREDECESSOR_RUNS_DDL = """
CREATE TABLE runs (
run_id TEXT NOT NULL, 
bootstrap_workflow_id TEXT NOT NULL, 
revision_hash TEXT NOT NULL, 
workflow_format_version INTEGER NOT NULL, 
agent_binding_set_hash TEXT, 
current_node_id TEXT NOT NULL, 
state TEXT NOT NULL, 
state_version INTEGER NOT NULL, 
last_event_sequence INTEGER NOT NULL, 
terminal_hash TEXT, 
run_configuration_revision_hash TEXT, 
PRIMARY KEY (run_id), 
UNIQUE (run_id, revision_hash), 
UNIQUE (run_id, revision_hash, agent_binding_set_hash), 
CHECK (length(run_id) > 0), 
CHECK (length(current_node_id) > 0), 
CHECK (workflow_format_version IN (1, 2, 3)), 
CHECK ((workflow_format_version = 1 AND agent_binding_set_hash IS NULL) OR (workflow_format_version = 2 AND agent_binding_set_hash IS NOT NULL AND length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version = 3 AND (agent_binding_set_hash IS NULL OR (length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*')))), 
CHECK (state IN ('STARTED', 'WAITING_RECONCILIATION', 'WAITING_INPUT', 'COMPLETED')), 
CHECK (state_version >= 0), 
CHECK (last_event_sequence >= 0), 
CHECK ((state = 'COMPLETED' AND terminal_hash IS NOT NULL AND length(terminal_hash) = 64 AND terminal_hash NOT GLOB '*[^0-9a-f]*') OR (state <> 'COMPLETED' AND terminal_hash IS NULL)), 
CHECK ((workflow_format_version = 3 AND run_configuration_revision_hash IS NOT NULL AND length(run_configuration_revision_hash) = 64 AND run_configuration_revision_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version <> 3 AND run_configuration_revision_hash IS NULL)), 
UNIQUE (bootstrap_workflow_id), 
FOREIGN KEY(revision_hash) REFERENCES workflow_revisions (revision_hash), 
FOREIGN KEY(run_configuration_revision_hash) REFERENCES run_configuration_revisions (revision_hash)
)
"""


def _restore_predecessor_runs(connection: Connection) -> None:
    connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
    connection.execute(sa.text("DROP TRIGGER runs_binding_no_update"))
    connection.execute(sa.text("DROP TABLE runs"))
    connection.execute(sa.text(_PREDECESSOR_RUNS_DDL))
    connection.execute(sa.text(_PRODUCT_TRIGGERS["runs_binding_no_update"]))
    connection.execute(sa.text("PRAGMA foreign_keys=ON"))


def _restore_predecessor_agent_attempts(connection: Connection) -> None:
    triggers = ("agent_attempts_state_transition", "agent_attempts_no_delete")
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    connection.execute(sa.text("DROP TABLE agent_attempts"))
    connection.execute(sa.text(_PREDECESSOR_AGENT_ATTEMPTS_DDL))
    connection.execute(sa.text(_PREDECESSOR_AGENT_ATTEMPTS_TRIGGER_DDL))
    connection.execute(sa.text(_PRODUCT_TRIGGERS["agent_attempts_no_delete"]))


def _archived_completion(revision_hash: WorkflowRevisionHash) -> RunEvent:
    """The completion an old run really wrote: no attempt binding, no receipt."""
    run_id = RunId(ARCHIVED_RUN_ID)
    return RunEvent(
        run_id,
        revision_hash,
        1,
        ARCHIVED_NODE_ID,
        NodeExecutionId.for_node(run_id, revision_hash, ARCHIVED_NODE_ID),
        RunEventKind.AGENT_COMPLETED,
        ARCHIVED_OUTPUT,
    )


def _create_populated_v13_store(database_path: Path) -> None:
    """An exact V13 product store, not a version-row witness.

    A fresh store of the current schema with each later table and its triggers
    removed, and `run_events` restored to the shape it had before V16, is the
    published V13 shape. That is the same method as the #240 Z2 testimony
    (predecessor schema from before the V14 head), expressed through today's
    owner so the fixture cannot drift from the create path the hop will reopen.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    published = PublishedRevision(RevisionKind.WORKFLOW, b"name: lasagne\n")
    lineage = CatalogLineage(published.kind, published.revision_hash)
    configuration = "44" * 32
    package = "33" * 32
    request = "22" * 32
    execution = "11" * 32
    receipt = "ef" * 32
    with engine.connect() as connection:
        for table in (artifacts.name, run_inputs_v3.name, tool_redemptions.name):
            connection.execute(sa.text(f"DROP TRIGGER {table}_no_update"))
            connection.execute(sa.text(f"DROP TRIGGER {table}_no_delete"))
            connection.execute(sa.text(f"DROP TABLE {table}"))
        _restore_predecessor_run_events(connection)
        _restore_predecessor_agent_attempts(connection)
        _restore_predecessor_runs(connection)
        connection.execute(
            atelier_schema_versions.update()
            .where(atelier_schema_versions.c.version == SCHEMA_VERSION)
            .values(version=V13_SCHEMA_HANDOFF.version)
        )
        connection.execute(
            published_revisions.insert().values(
                kind=published.kind.value,
                revision_hash=published.revision_hash.value,
                document=published.document,
            )
        )
        connection.execute(
            catalog_lineages.insert().values(
                lineage_id=lineage.lineage_id.value,
                kind=published.kind.value,
                founding_revision_hash=published.revision_hash.value,
            )
        )
        connection.execute(
            catalog_lineage_members.insert().values(
                lineage_id=lineage.lineage_id.value,
                revision_number=1,
                revision_hash=published.revision_hash.value,
            )
        )
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=published.revision_hash.value,
                document=published.document,
            )
        )
        connection.execute(
            run_configuration_revisions.insert().values(
                revision_hash=configuration, preimage=b"one frozen resolution matrix"
            )
        )
        connection.execute(
            context_packages_v3.insert().values(
                package_hash=package, manifest=b"one supervised manifest"
            )
        )
        connection.execute(
            node_execution_requests_v3.insert().values(
                request_hash=request,
                node_execution_id=execution,
                run_configuration_revision_hash=configuration,
                context_package_hash=package,
                preimage=b"one node execution request",
            )
        )
        connection.execute(
            runs.insert().values(
                run_id=ARCHIVED_RUN_ID,
                bootstrap_workflow_id="bootstrap-archived-night-run",
                revision_hash=published.revision_hash.value,
                workflow_format_version=3,
                agent_binding_set_hash=None,
                current_node_id="cook",
                state="STARTED",
                state_version=1,
                last_event_sequence=1,
                terminal_hash=None,
                run_configuration_revision_hash=configuration,
            )
        )
        connection.execute(
            agent_attempts.insert().values(
                attempt_id=ARCHIVED_ATTEMPT_ID,
                node_execution_id=execution,
                request_hash=request,
                executor_operational_identity="operational/archived",
                run_id=ARCHIVED_RUN_ID,
                workflow_revision_hash=published.revision_hash.value,
                node_id="cook",
                attempt_ordinal=1,
                state="FAILED",
                state_version=2,
                process_phase="PROCESS_OBSERVED",
                process_owner_id="owner/archived",
                watchdog_generation_id="generation/archived",
                failure_code=ARCHIVED_ATTEMPT_FAILURE_CODE,
            )
        )
        archived = _archived_completion(
            WorkflowRevisionHash(published.revision_hash.value)
        )
        connection.execute(
            sa.text(
                "INSERT INTO run_events (run_id, revision_hash, event_sequence, "
                "node_id, node_execution_id, event_kind, payload, payload_hash, "
                "event_hash) VALUES (:run_id, :revision_hash, :event_sequence, "
                ":node_id, :node_execution_id, :event_kind, :payload, "
                ":payload_hash, :event_hash)"
            ),
            {
                "run_id": archived.run_id.value,
                "revision_hash": archived.revision_hash.value,
                "event_sequence": archived.event_sequence,
                "node_id": archived.node_id,
                "node_execution_id": archived.node_execution_id.value,
                "event_kind": archived.event_kind.value,
                "payload": archived.payload,
                "payload_hash": archived.payload_hash.value,
                "event_hash": archived.event_hash.value,
            },
        )
        connection.execute(
            node_receipts_v3.insert().values(
                node_execution_id=execution,
                disposition="succeeded",
                reason="completed",
                request_hash=request,
                context_package_hash=package,
                receipt_hash=receipt,
            )
        )
        connection.commit()
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _require_product_shape(connection, V13_SCHEMA_HANDOFF.version)


@pytest.mark.proves("an-exact-v13-store-migrates-and-opens-as-the-current-schema")
def test_an_exact_v13_store_migrates_and_opens_as_the_current_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 13"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert all(str(step) in shown.out for step in range(13, SCHEMA_VERSION + 1))
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        assert (
            connection.scalar(
                sa.select(runs.c.run_id).where(runs.c.run_id == ARCHIVED_RUN_ID)
            )
            == ARCHIVED_RUN_ID
        )
        assert (
            connection.scalar(sa.select(node_receipts_v3.c.disposition)) == "succeeded"
        )
        attempt = (
            connection.execute(
                sa.select(agent_attempts).where(
                    agent_attempts.c.attempt_id == ARCHIVED_ATTEMPT_ID
                )
            )
            .mappings()
            .one()
        )
        assert attempt["state"] == "FAILED"
        assert attempt["failure_code"] == ARCHIVED_ATTEMPT_FAILURE_CODE
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(run_inputs_v3))
            == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tool_redemptions))
            == 0
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(artifacts)) == 0
        archived = (
            connection.execute(
                sa.select(run_events).where(run_events.c.run_id == ARCHIVED_RUN_ID)
            )
            .mappings()
            .one()
        )
        expected = _archived_completion(
            WorkflowRevisionHash(str(archived["revision_hash"]))
        )
        assert bytes(archived["payload"]) == ARCHIVED_OUTPUT
        assert str(archived["event_hash"]) == expected.event_hash.value
        assert archived["agent_receipt_hash"] is None
        assert event_from_record(archived) == expected
    engine.dispose()


@pytest.mark.proves("an-unknown-or-future-schema-is-refused-by-name")
def test_an_unknown_or_future_schema_is_refused_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.begin() as connection:
        connection.execute(atelier_schema_versions.delete())
        connection.execute(
            atelier_schema_versions.insert().values(version=SCHEMA_VERSION + 1)
        )
    engine.dispose()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert str(SCHEMA_VERSION + 1) in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before


@pytest.mark.proves("a-current-schema-store-is-a-named-noop")
def test_a_current_schema_store_is_a_named_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "already current" in shown.out
    assert "nothing to migrate" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out
    assert _logical_dump(database_path) == before


def test_an_older_predecessor_without_a_step_is_refused_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
            CREATE TABLE predecessor_witness(value BLOB NOT NULL);
            INSERT INTO atelier_schema_versions VALUES(12);
            INSERT INTO predecessor_witness VALUES(X'00FF');
            """
        )
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "12" in shown.err
    assert "no migration step" in shown.err
    assert _logical_dump(database_path) == before


def test_a_locked_store_is_refused_without_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    before = _logical_dump(database_path)
    holder = sqlite3.connect(database_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert main(["migrate", "--database", str(database_path)]) == 1
    finally:
        holder.rollback()
        holder.close()
    assert "in use" in capsys.readouterr().err
    assert _logical_dump(database_path) == before


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE run_events_before_the_receipt_column(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW run_events_before_the_receipt_column AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_receipt_column_hop_rolls_back_every_earlier_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The last step refuses, so the two that already ran are undone with it.

    The receipt-column hop rebuilds `run_events` under a parking name, so any
    object already holding that name is a collision the hop refuses by name.
    It sits behind two completed steps in the same transaction, which is what
    makes this the whole hop's atomicity and not just this step's.
    """
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "run_events_before_the_receipt_column" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE agent_attempts_before_the_refusal_code(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW agent_attempts_before_the_refusal_code AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_failure_code_hop_rolls_back_every_earlier_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The final hop refuses, so the three that already ran are undone with it.

    The failure-code hop rebuilds `agent_attempts` under a parking name, so any
    object already holding that name is a collision the hop refuses by name --
    after the three earlier steps completed inside the same transaction.
    """
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "agent_attempts_before_the_refusal_code" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)


def test_a_failed_step_leaves_the_predecessor_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE run_inputs_v3(wrong TEXT)")
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)
