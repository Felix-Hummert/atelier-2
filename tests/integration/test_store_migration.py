"""The offline hop a live V13 store needed and did not have.

The V13 fixture is a real predecessor store: the current create path, every later
addition removed, then a format-3 run that already wrote one event. That is the
#240 Z2 method — predecessor schema, not a version-row stub — expressed through
today's owner.

V14 and V15 each added a table, so dropping those tables was the whole reversal.
Every version after them instead reshapes a table V13 already had -- V21 is the
capability CHECK on `agent_configuration_revisions` -- so the fixture also
restores each of those tables' published V13 shape below. The literals are not
second owners of the current tables: they are the frozen artifacts V13 really
carried, and the pinned V13 fingerprint refuses them the moment a character
drifts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

import pytest
import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from dbos._serialization import DefaultSerializer
from sqlalchemy.engine import Connection

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.names import ANSWER_WORKFLOW_NAME, QUEUE_NAME
from atelier2.adapters.dbos.published_schema_shapes import PUBLISHED_TABLE_SHAPES
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.run_transitions import event_from_record
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    _AGENT_ATTEMPTS_TRIGGERS,
    _PREDECESSOR_WAIT_ANSWERS,
    _PREDECESSOR_WAIT_UNCANCELLABLE_RUN_EVENTS,
    _PRODUCT_TRIGGERS,
    _ROUND_SCOPED_EVENT_INDEX,
    _RUN_EVENTS_TRIGGERS,
    _V17_AGENT_ATTEMPT_TRIGGERS,
    _V23_AGENT_ATTEMPT_TRIGGERS,
    _V24_AGENT_ATTEMPT_TRIGGERS,
    _V27_AGENT_ATTEMPT_STATE_TRANSITION,
    _VERSION_TWENTY,
    _WAIT_ANSWERS_TRIGGERS,
    PRODUCT_SCHEMA_HANDOFF,
    SCHEMA_VERSION,
    V13_SCHEMA_HANDOFF,
    V21_SCHEMA_HANDOFF,
    V22_SCHEMA_HANDOFF,
    V23_SCHEMA_HANDOFF,
    V24_SCHEMA_HANDOFF,
    V25_SCHEMA_HANDOFF,
    V26_SCHEMA_HANDOFF,
    V27_SCHEMA_HANDOFF,
    V28_SCHEMA_HANDOFF,
    V29_SCHEMA_HANDOFF,
    V31_SCHEMA_HANDOFF,
    V32_SCHEMA_HANDOFF,
    V33_SCHEMA_HANDOFF,
    V34_SCHEMA_HANDOFF,
    V35_SCHEMA_HANDOFF,
    MigrationRequired,
    _rebuild_product_table,
    _require_product_shape,
    agent_attempts,
    agent_configuration_revisions,
    agent_receipts_v2,
    artifacts,
    atelier_schema_versions,
    attempt_instants,
    auth_profile_revisions,
    catalog_lineage_members,
    catalog_lineages,
    context_packages_v3,
    event_instants,
    host_occupancy_bindings,
    host_occupancy_revisions,
    host_project_root_revisions,
    host_project_source_connection_revisions,
    initialize_schema,
    node_execution_requests_v3,
    node_receipts_v3,
    published_revisions,
    queue_items,
    run_agent_bindings,
    run_configuration_revisions,
    run_events,
    run_inputs_v3,
    run_instants,
    runs,
    tool_redemptions,
    wait_answers,
    webhook_delivery_cursor,
    workflow_revisions,
)
from atelier2.adapters.dbos.workflow_ids import answer_workflow_id_for
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    answer_wait_result,
)
from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptReplacement,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentReceiptHash,
)
from atelier2.contracts.catalog_v3 import CatalogLineage
from atelier2.contracts.effects import (
    ConfirmationSource,
    EffectIntentState,
    LogicalEffectKey,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventCancellationBinding,
    RunEventKind,
    WaitAnswerState,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId, ProjectRootRevision
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_cancellations import RunCancelCommandId
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.host import main
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.integration.test_runner_terminal_evidence_store import _bound
from tests.integration.test_v3_wait_run import (
    ANSWER,
    RUN,
    WAIT_IN_THE_MIDDLE,
    WAIT_NODE,
    recording_provider,
    start_and_launch,
    wait_for_state,
    wait_runtime_over,
)
from tests.scenarios.agents import agent_attempt_execution

ARCHIVED_RUN_ID = "live/erster-lauf-nach-der-nacht"
ARCHIVED_NODE_ID = "cook"
ARCHIVED_OUTPUT = b"lasagne, aufgetragen"

_V27_ACCESS_STORE_DDL = """
CREATE TABLE node_receipt_access_v3 (
	node_execution_id TEXT NOT NULL, position INTEGER NOT NULL, access_receipt_hash TEXT NOT NULL,
	PRIMARY KEY (node_execution_id, position), CHECK (position >= 0),
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), CHECK (length(access_receipt_hash) = 64 AND access_receipt_hash NOT GLOB '*[^0-9a-f]*'),
	FOREIGN KEY(node_execution_id) REFERENCES node_receipts_v3 (node_execution_id)
);
CREATE TRIGGER node_receipt_access_v3_no_update BEFORE UPDATE ON node_receipt_access_v3 BEGIN SELECT RAISE(ABORT, 'v3 node receipt access is immutable'); END; CREATE TRIGGER node_receipt_access_v3_no_delete BEFORE DELETE ON node_receipt_access_v3 BEGIN SELECT RAISE(ABORT, 'v3 node receipt access is immutable'); END;
"""


def _restore_v27_access_store(connection: sqlite3.Connection) -> None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='node_receipt_access_v3'"
    ).fetchone()
    if exists is None:
        connection.executescript(_V27_ACCESS_STORE_DDL)


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
ARCHIVED_RECEIPT_NODE_EXECUTION_ID = "99" * 32
ARCHIVED_AGENT_CONFIGURATION_HASH = "66" * 32
ARCHIVED_AGENT_MODEL = "archived-model"
"""The published configuration an old store already carried.

The capability hop rebuilds the table this row lives in, so the fixture's own
published binding is what proves the rows came over: a hop that rebuilt an empty
table would prove the shape and nothing about what stood in it.
"""


def _logical_dump(database_path: Path) -> tuple[str, ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(connection.iterdump())


_PREDECESSOR_RUN_EVENTS_INDEX_DDL = (
    (
        "CREATE UNIQUE INDEX run_events_attempt_kind_unique ON run_events "
        "(agent_attempt_id, event_kind) WHERE agent_attempt_id IS NOT NULL"
    ),
    (
        "CREATE UNIQUE INDEX run_events_legacy_execution_kind_unique ON run_events "
        "(node_execution_id, event_kind) WHERE agent_attempt_id IS NULL"
    ),
    (
        "CREATE UNIQUE INDEX run_events_legacy_kind_unique ON run_events "
        "(run_id, revision_hash, node_id, event_kind) WHERE agent_attempt_id IS NULL"
    ),
)
"""The three event keys the store behind this fixture's table text published.

The V36 hop re-scoped the once-per-node key to the round, so a fixture that
builds a store from before that hop states the key as it stood, exactly as it
states the predecessor table text next to it.
"""


def _restore_predecessor_run_events(connection: Connection) -> None:
    triggers = ("run_events_no_update", "run_events_no_delete")
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    for index in sorted(run_events.indexes, key=lambda index: index.name or ""):
        connection.execute(sa.text(f"DROP INDEX {index.name}"))
    connection.execute(sa.text("DROP TABLE run_events"))
    connection.execute(sa.text(_PREDECESSOR_RUN_EVENTS_DDL))
    for index_statement in _PREDECESSOR_RUN_EVENTS_INDEX_DDL:
        connection.execute(sa.text(index_statement))
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


_PREDECESSOR_AGENT_CONFIGURATION_REVISIONS_DDL = """
CREATE TABLE agent_configuration_revisions (
revision_hash TEXT NOT NULL, 
model TEXT NOT NULL, 
auth_profile_revision_hash TEXT NOT NULL, 
executor_revision TEXT NOT NULL, 
revision_format_version INTEGER NOT NULL, 
requested_capability TEXT NOT NULL, 
PRIMARY KEY (revision_hash), 
UNIQUE (revision_hash, auth_profile_revision_hash, model, executor_revision), 
UNIQUE (revision_hash, auth_profile_revision_hash, model, executor_revision, revision_format_version, requested_capability), 
CHECK (length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'), 
CHECK (length(model) BETWEEN 1 AND 1024), 
CHECK (length(auth_profile_revision_hash) = 64 AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'), 
CHECK (length(executor_revision) BETWEEN 1 AND 1024), 
CHECK (revision_format_version IN (1, 2)), 
CHECK (requested_capability IN ('headless', 'interactive')), 
CHECK (revision_format_version = 2 OR requested_capability = 'headless'), 
FOREIGN KEY(auth_profile_revision_hash) REFERENCES auth_profile_revisions (revision_hash)
)
"""


def _restore_predecessor_agent_configuration_revisions(
    connection: Connection,
) -> None:
    triggers = (
        "agent_configuration_revisions_no_update",
        "agent_configuration_revisions_no_delete",
    )
    connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    connection.execute(sa.text("DROP TABLE agent_configuration_revisions"))
    connection.execute(sa.text(_PREDECESSOR_AGENT_CONFIGURATION_REVISIONS_DDL))
    for trigger in triggers:
        connection.execute(sa.text(_PRODUCT_TRIGGERS[trigger]))
    connection.execute(sa.text("PRAGMA foreign_keys=ON"))


def _restore_predecessor_agent_attempts(connection: Connection) -> None:
    triggers = ("agent_attempts_state_transition", "agent_attempts_no_delete")
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    connection.execute(sa.text("DROP TABLE agent_attempts"))
    connection.execute(sa.text(_PREDECESSOR_AGENT_ATTEMPTS_DDL))
    connection.execute(sa.text(_PREDECESSOR_AGENT_ATTEMPTS_TRIGGER_DDL))
    connection.execute(sa.text(_PRODUCT_TRIGGERS["agent_attempts_no_delete"]))


_PREDECESSOR_NODE_EXECUTION_REQUESTS_DDL = """
CREATE TABLE node_execution_requests_v3 (
    request_hash TEXT NOT NULL,
    node_execution_id TEXT NOT NULL,
    run_configuration_revision_hash TEXT NOT NULL,
    context_package_hash TEXT NOT NULL,
    preimage BLOB NOT NULL,
    PRIMARY KEY (request_hash),
    UNIQUE (node_execution_id, request_hash),
    CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(context_package_hash) = 64 AND context_package_hash NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY(context_package_hash) REFERENCES context_packages_v3 (package_hash),
    FOREIGN KEY(run_configuration_revision_hash) REFERENCES run_configuration_revisions (revision_hash)
)
"""


def _restore_predecessor_node_execution_requests(connection: Connection) -> None:
    triggers = (
        "node_execution_requests_v3_no_update",
        "node_execution_requests_v3_no_delete",
    )
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    connection.execute(sa.text("DROP TABLE node_execution_requests_v3"))
    connection.execute(sa.text(_PREDECESSOR_NODE_EXECUTION_REQUESTS_DDL))
    for trigger in triggers:
        connection.execute(sa.text(_PRODUCT_TRIGGERS[trigger]))


_PREDECESSOR_AGENT_RECEIPTS_DDL = """
CREATE TABLE agent_receipts_v2 (
    node_execution_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    run_id TEXT NOT NULL,
    workflow_revision_hash TEXT NOT NULL,
    node_id TEXT NOT NULL,
    role TEXT NOT NULL,
    binding_set_hash TEXT NOT NULL,
    agent_configuration_revision_hash TEXT NOT NULL,
    auth_profile_revision_hash TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    auth_mode TEXT NOT NULL,
    model TEXT NOT NULL,
    executor_revision TEXT NOT NULL,
    executor_operational_identity TEXT NOT NULL,
    output_bytes BLOB NOT NULL,
    output_hash TEXT NOT NULL,
    receipt_hash TEXT NOT NULL,
    PRIMARY KEY (node_execution_id),
    UNIQUE (run_id, workflow_revision_hash, node_id),
    FOREIGN KEY(run_id, workflow_revision_hash, binding_set_hash, role, agent_configuration_revision_hash) REFERENCES run_agent_bindings (run_id, revision_hash, binding_set_hash, role, agent_configuration_revision_hash),
    FOREIGN KEY(agent_configuration_revision_hash, auth_profile_revision_hash, model, executor_revision) REFERENCES agent_configuration_revisions (revision_hash, auth_profile_revision_hash, model, executor_revision),
    FOREIGN KEY(auth_profile_revision_hash, profile_id, revision_number, provider_id, auth_mode) REFERENCES auth_profile_revisions (revision_hash, profile_id, revision_number, provider_id, auth_mode),
    CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(run_id) > 0),
    CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(node_id) BETWEEN 1 AND 1024),
    CHECK (length(role) BETWEEN 1 AND 1024),
    CHECK (length(binding_set_hash) = 64 AND binding_set_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(agent_configuration_revision_hash) = 64 AND agent_configuration_revision_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(auth_profile_revision_hash) = 64 AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(profile_id) BETWEEN 1 AND 1024),
    CHECK (revision_number BETWEEN 1 AND 9223372036854775807),
    CHECK (length(provider_id) BETWEEN 1 AND 64),
    CHECK (provider_id GLOB '[a-z]*'),
    CHECK (provider_id NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (auth_mode IN ('subscription', 'api_key')),
    CHECK (length(model) BETWEEN 1 AND 1024),
    CHECK (length(executor_revision) BETWEEN 1 AND 1024),
    CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024),
    CHECK (typeof(output_bytes) = 'blob' AND length(output_bytes) <= 49152),
    CHECK (length(output_hash) = 64 AND output_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'),
    UNIQUE (receipt_hash)
)
"""


def _restore_predecessor_agent_receipts(connection: Connection) -> None:
    triggers = ("agent_receipts_v2_no_update", "agent_receipts_v2_no_delete")
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    connection.execute(sa.text("DROP TABLE agent_receipts_v2"))
    connection.execute(sa.text(_PREDECESSOR_AGENT_RECEIPTS_DDL))
    for trigger in triggers:
        connection.execute(sa.text(_PRODUCT_TRIGGERS[trigger]))


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
    removed, and every table a later hop reshapes restored to the shape it had
    at V13, is the published V13 shape. That is the same method as the #240 Z2
    testimony
    (predecessor schema from before the V14 head), expressed through today's
    owner so the fixture cannot drift from the create path the hop will reopen.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with sqlite3.connect(database_path) as predecessor:
        _restore_v27_access_store(predecessor)
        _drop_queue_items_table(predecessor)
        _drop_webhook_delivery_cursor_table(predecessor)
        _drop_project_source_connection_table(predecessor)
        _revert_wait_answers_execution_key(predecessor)
    published = PublishedRevision(RevisionKind.WORKFLOW, b"name: lasagne\n")
    lineage = CatalogLineage(published.kind, published.revision_hash)
    configuration = "44" * 32
    package = "33" * 32
    request = "22" * 32
    execution = "11" * 32
    receipt = "ef" * 32
    auth_profile_revision_hash = "55" * 32
    agent_configuration_revision_hash = ARCHIVED_AGENT_CONFIGURATION_HASH
    binding_set_hash = "77" * 32
    agent_receipt_hash = "dd" * 32
    with engine.connect() as connection:
        for table in (
            artifacts.name,
            run_inputs_v3.name,
            tool_redemptions.name,
            host_occupancy_bindings.name,
            host_occupancy_revisions.name,
            host_project_root_revisions.name,
        ):
            connection.execute(sa.text(f"DROP TRIGGER {table}_no_update"))
            connection.execute(sa.text(f"DROP TRIGGER {table}_no_delete"))
            connection.execute(sa.text(f"DROP TABLE {table}"))
        for trigger in (
            "run_instants_start_no_update",
            "run_instants_end_once",
            "run_instants_no_delete",
            "attempt_instants_start_no_update",
            "attempt_instants_end_once",
            "attempt_instants_no_delete",
            "event_instants_no_update",
            "event_instants_no_delete",
        ):
            connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
        for table in (run_instants.name, attempt_instants.name, event_instants.name):
            connection.execute(sa.text(f"DROP TABLE {table}"))
        _restore_predecessor_run_events(connection)
        _restore_predecessor_agent_receipts(connection)
        _restore_predecessor_agent_attempts(connection)
        _restore_predecessor_runs(connection)
        _restore_predecessor_node_execution_requests(connection)
        _restore_predecessor_agent_configuration_revisions(connection)
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
                agent_binding_set_hash=binding_set_hash,
                current_node_id="cook",
                state="STARTED",
                state_version=1,
                last_event_sequence=1,
                terminal_hash=None,
                run_configuration_revision_hash=configuration,
            )
        )
        connection.execute(
            auth_profile_revisions.insert().values(
                revision_hash=auth_profile_revision_hash,
                profile_id="profile/archived",
                revision_number=1,
                provider_id="anthropic",
                auth_mode="api_key",
            )
        )
        connection.execute(
            agent_configuration_revisions.insert().values(
                revision_hash=agent_configuration_revision_hash,
                model=ARCHIVED_AGENT_MODEL,
                auth_profile_revision_hash=auth_profile_revision_hash,
                executor_revision="archived-executor",
                revision_format_version=1,
                requested_capability="headless",
            )
        )
        connection.execute(
            run_agent_bindings.insert().values(
                run_id=ARCHIVED_RUN_ID,
                revision_hash=published.revision_hash.value,
                binding_set_hash=binding_set_hash,
                role="chef",
                agent_configuration_revision_hash=agent_configuration_revision_hash,
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
        connection.execute(
            sa.text(
                "INSERT INTO agent_receipts_v2 (node_execution_id, request_hash, "
                "run_id, workflow_revision_hash, node_id, role, binding_set_hash, "
                "agent_configuration_revision_hash, auth_profile_revision_hash, "
                "profile_id, revision_number, provider_id, auth_mode, model, "
                "executor_revision, executor_operational_identity, output_bytes, "
                "output_hash, receipt_hash) VALUES (:node_execution_id, "
                ":request_hash, :run_id, :workflow_revision_hash, :node_id, "
                ":role, :binding_set_hash, :agent_configuration_revision_hash, "
                ":auth_profile_revision_hash, :profile_id, :revision_number, "
                ":provider_id, :auth_mode, :model, :executor_revision, "
                ":executor_operational_identity, :output_bytes, :output_hash, "
                ":receipt_hash)"
            ),
            {
                "node_execution_id": ARCHIVED_RECEIPT_NODE_EXECUTION_ID,
                "request_hash": request,
                "run_id": ARCHIVED_RUN_ID,
                "workflow_revision_hash": published.revision_hash.value,
                "node_id": ARCHIVED_NODE_ID,
                "role": "chef",
                "binding_set_hash": binding_set_hash,
                "agent_configuration_revision_hash": agent_configuration_revision_hash,
                "auth_profile_revision_hash": auth_profile_revision_hash,
                "profile_id": "profile/archived",
                "revision_number": 1,
                "provider_id": "anthropic",
                "auth_mode": "api_key",
                "model": "archived-model",
                "executor_revision": "archived-executor",
                "executor_operational_identity": "operational/archived",
                "output_bytes": ARCHIVED_OUTPUT,
                "output_hash": "aa" * 32,
                "receipt_hash": agent_receipt_hash,
            },
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
            connection.scalar(
                sa.select(runs.c.current_round_ordinal).where(
                    runs.c.run_id == ARCHIVED_RUN_ID
                )
            )
            == FIRST_ROUND_ORDINAL
        )
        carried_receipt = (
            connection.execute(
                sa.select(agent_receipts_v2).where(
                    agent_receipts_v2.c.run_id == ARCHIVED_RUN_ID,
                    agent_receipts_v2.c.node_id == ARCHIVED_NODE_ID,
                )
            )
            .mappings()
            .one()
        )
        assert (
            carried_receipt["node_execution_id"] == ARCHIVED_RECEIPT_NODE_EXECUTION_ID
        )
        assert carried_receipt["round_ordinal"] == FIRST_ROUND_ORDINAL
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
        assert attempt["runner_manifest_id"] is None
        assert attempt["runner_generation_id"] is None
        assert attempt["runner_invocation_id"] is None
        assert attempt["runner_terminal_evidence_hash"] is None
        assert attempt["runner_evidence_acceptance_phase"] == "NONE"
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(run_inputs_v3))
            == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tool_redemptions))
            == 0
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(artifacts)) == 0
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(host_project_root_revisions)
            )
            == 0
        )
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
        configuration = (
            connection.execute(sa.select(agent_configuration_revisions))
            .mappings()
            .one()
        )
        assert configuration["revision_hash"] == ARCHIVED_AGENT_CONFIGURATION_HASH
        assert configuration["model"] == ARCHIVED_AGENT_MODEL
        assert configuration["requested_capability"] == (
            AgentExecutionCapability.HEADLESS.value
        )
    # The widened vocabulary really arrived: the migrated store now publishes a
    # configuration the predecessor's CHECK would have refused.
    with engine.begin() as connection:
        connection.execute(
            agent_configuration_revisions.insert().values(
                revision_hash="cd" * 32,
                model=ARCHIVED_AGENT_MODEL,
                auth_profile_revision_hash=str(
                    configuration["auth_profile_revision_hash"]
                ),
                executor_revision="claude-subscription-tools/v1",
                revision_format_version=(
                    AgentConfigurationRevisionFormatVersion.V2.value
                ),
                requested_capability=(
                    AgentExecutionCapability.HEADLESS_WITH_TOOLS.value
                ),
            )
        )
    engine.dispose()


def _revert_project_verification_failed_attempts(
    connection: sqlite3.Connection,
) -> None:
    """Restore the three-code CHECK the PROJECT_VERIFICATION_FAILED hop left."""

    _rebuild_product_table(
        connection,
        agent_attempts,
        "agent_attempts_after_project_verification_failed",
        _AGENT_ATTEMPTS_TRIGGERS,
        SCHEMA_VERSION,
        V23_SCHEMA_HANDOFF.version,
        trigger_source=_V23_AGENT_ATTEMPT_TRIGGERS,
    )


def _revert_agent_refused_attempts(connection: sqlite3.Connection) -> None:
    """Restore the two-code CHECK the AGENT_REFUSED hop left behind."""

    _rebuild_product_table(
        connection,
        agent_attempts,
        "agent_attempts_after_agent_refused",
        _AGENT_ATTEMPTS_TRIGGERS,
        SCHEMA_VERSION,
        V22_SCHEMA_HANDOFF.version,
        trigger_source=_V17_AGENT_ATTEMPT_TRIGGERS,
    )


def _drop_host_project_root_channel(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER host_project_root_revisions_no_update")
    connection.execute("DROP TRIGGER host_project_root_revisions_no_delete")
    connection.execute(f"DROP TABLE {host_project_root_revisions.name}")


def _drop_occupancy_channel(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER host_occupancy_bindings_no_update")
    connection.execute("DROP TRIGGER host_occupancy_bindings_no_delete")
    connection.execute("DROP TRIGGER host_occupancy_revisions_no_update")
    connection.execute("DROP TRIGGER host_occupancy_revisions_no_delete")
    connection.execute(f"DROP TABLE {host_occupancy_bindings.name}")
    connection.execute(f"DROP TABLE {host_occupancy_revisions.name}")


def _drop_queue_items_table(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER queue_items_identity_no_update")
    connection.execute("DROP TRIGGER queue_items_no_delete")
    connection.execute(f"DROP TABLE {queue_items.name}")


def _drop_webhook_delivery_cursor_table(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER webhook_delivery_cursor_identity_no_update")
    connection.execute("DROP TRIGGER webhook_delivery_cursor_no_delete")
    connection.execute(f"DROP TABLE {webhook_delivery_cursor.name}")


def _drop_project_source_connection_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "DROP TRIGGER host_project_source_connection_revisions_no_update"
    )
    connection.execute(
        "DROP TRIGGER host_project_source_connection_revisions_no_delete"
    )
    connection.execute(f"DROP TABLE {host_project_source_connection_revisions.name}")


_PREDECESSOR_WAIT_ANSWERS_DDL = """
CREATE TABLE wait_answers (
	run_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	node_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	answer_bytes BLOB NOT NULL, 
	answer_hash TEXT NOT NULL, 
	answer_workflow_id TEXT NOT NULL, 
	state TEXT NOT NULL, 
	state_version INTEGER NOT NULL, 
	PRIMARY KEY (run_id, node_id), 
	FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash), 
	CHECK (length(node_id) > 0), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(answer_hash) = 64 AND answer_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(answer_workflow_id) > 0), 
	CHECK (state IN ('PENDING', 'APPLIED')), 
	CHECK (state_version IN (0, 1)), 
	CHECK ((state = 'PENDING' AND state_version = 0) OR (state = 'APPLIED' AND state_version = 1)), 
	UNIQUE (node_execution_id), 
	UNIQUE (answer_workflow_id)
)
"""

_PREDECESSOR_WAIT_ANSWERS_PAYLOAD_TRIGGER_DDL = """
CREATE TRIGGER wait_answers_payload_no_update
BEFORE UPDATE OF run_id, revision_hash, node_id, node_execution_id,
                 answer_bytes, answer_hash, answer_workflow_id
ON wait_answers BEGIN
  SELECT RAISE(ABORT, 'wait answer bindings are immutable');
END
"""


_PARKED_CURRENT_WAIT_ANSWERS = "wait_answers_of_the_current_schema"


def _revert_wait_answers_execution_key(connection: sqlite3.Connection) -> None:
    """Restore the run-and-node key and roundless payload trigger the #671 hop moved.

    `wait_answers` has carried one shape since it was introduced, so every
    "exact vNN store" fixture up to V33 shares this one predecessor -- the #671
    hop is the first to touch it at all.

    Rows are carried back the way the hop carries them forward, dropping the
    round the predecessor has no column for. An empty table copies nothing, so
    the fixtures that only want the shape pay nothing for it, and a store that
    was driven to a real pause keeps the answer that pause accepted.
    """

    for trigger in _WAIT_ANSWERS_TRIGGERS:
        connection.execute(f"DROP TRIGGER {trigger}")
    connection.execute(
        f"ALTER TABLE {wait_answers.name} RENAME TO {_PARKED_CURRENT_WAIT_ANSWERS}"
    )
    connection.execute(_PREDECESSOR_WAIT_ANSWERS_DDL)
    carried = ", ".join(
        str(record[1])
        for record in connection.execute(f"PRAGMA table_info({wait_answers.name})")
    )
    connection.execute(
        f"INSERT INTO {wait_answers.name} ({carried}) "
        f"SELECT {carried} FROM {_PARKED_CURRENT_WAIT_ANSWERS}"
    )
    connection.execute(f"DROP TABLE {_PARKED_CURRENT_WAIT_ANSWERS}")
    connection.execute(_PREDECESSOR_WAIT_ANSWERS_PAYLOAD_TRIGGER_DDL)
    connection.execute(_PRODUCT_TRIGGERS["wait_answers_state_transition"])
    connection.execute(_PRODUCT_TRIGGERS["wait_answers_no_delete"])


_PARKED_CURRENT_RUN_EVENTS = "run_events_after_the_cancelled_wait"


def _revert_wait_cancelled_event_kind(connection: sqlite3.Connection) -> None:
    """Restore the event-kind vocabulary the #668 hop widened.

    `run_events` has carried one shape since the V20 round column, so every
    "exact vNN store" fixture up to V34 shares this one predecessor -- the #668
    hop is simply the first since then to touch the table again. Stored events
    are carried back the way the hop carries them forward; no predecessor row
    can hold the kind the hop adds, so nothing is left behind.
    """

    _rebuild_product_table(
        connection,
        run_events,
        _PARKED_CURRENT_RUN_EVENTS,
        _RUN_EVENTS_TRIGGERS,
        SCHEMA_VERSION,
        V34_SCHEMA_HANDOFF.version,
    )


def _revert_cancelled_run_state(connection: sqlite3.Connection) -> None:
    """Restore the pre-CANCELLED `runs` CHECK the #439 P1 hop widened.

    `runs`' shape has been unchanged since the V20 round column, so every
    "exact vNN store" fixture between V21 and V29 shares the one V20 shape --
    the #439 P1 hop is simply the first since then to touch it again.
    """

    _rebuild_product_table(
        connection,
        runs,
        "runs_after_cancelled_state",
        ("runs_binding_no_update",),
        SCHEMA_VERSION,
        _VERSION_TWENTY,
    )


def _revert_runner_evidence_attempts(connection: sqlite3.Connection) -> None:
    """Restore the exact V26 attempt table and its pre-Runner trigger."""

    _rebuild_product_table(
        connection,
        agent_attempts,
        "agent_attempts_after_runner_evidence",
        _AGENT_ATTEMPTS_TRIGGERS,
        SCHEMA_VERSION,
        V26_SCHEMA_HANDOFF.version,
        trigger_source=_V24_AGENT_ATTEMPT_TRIGGERS,
    )


def _revert_agent_attempts_trigger_to_v27(connection: sqlite3.Connection) -> None:
    """Restore the pre-#584 attempt trigger V27 through V31 all shared.

    The current schema (#584, V32) is the first to change
    `agent_attempts_state_transition` since V27 gave it its runner-aware form,
    so a fixture that reverts only the version -- not the attempt table -- must
    also swap this trigger back, or its shape no longer matches the published
    fingerprint for the version it claims.
    """

    connection.execute("DROP TRIGGER agent_attempts_state_transition")
    connection.execute(_V27_AGENT_ATTEMPT_STATE_TRANSITION)


def _create_exact_v21_store(database_path: Path) -> None:
    """A current store with instants and AGENT_REFUSED removed: the published V21 shape."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_agent_refused_attempts(connection)
        _drop_occupancy_channel(connection)
        _drop_host_project_root_channel(connection)
        for trigger in (
            "run_instants_start_no_update",
            "run_instants_end_once",
            "run_instants_no_delete",
            "attempt_instants_start_no_update",
            "attempt_instants_end_once",
            "attempt_instants_no_delete",
            "event_instants_no_update",
            "event_instants_no_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        for table in (run_instants.name, attempt_instants.name, event_instants.name):
            connection.execute(f"DROP TABLE {table}")
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V21_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V21_SCHEMA_HANDOFF.version)


def _create_exact_v22_store(database_path: Path) -> None:
    """A current store with AGENT_REFUSED removed: the published V22 shape."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_agent_refused_attempts(connection)
        _drop_occupancy_channel(connection)
        _drop_host_project_root_channel(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V22_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V22_SCHEMA_HANDOFF.version)


def _create_exact_v23_store(database_path: Path) -> None:
    """A current store with PROJECT_VERIFICATION_FAILED removed: V23."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_project_verification_failed_attempts(connection)
        _drop_occupancy_channel(connection)
        _drop_host_project_root_channel(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V23_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V23_SCHEMA_HANDOFF.version)


def _create_exact_v24_store(database_path: Path) -> None:
    """A current store without the host configuration channel: V24."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_runner_evidence_attempts(connection)
        _drop_occupancy_channel(connection)
        _drop_host_project_root_channel(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V24_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V24_SCHEMA_HANDOFF.version)


def _create_exact_v25_store(database_path: Path) -> None:
    """A current store without occupancy revisions: V25."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_runner_evidence_attempts(connection)
        _drop_occupancy_channel(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V25_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V25_SCHEMA_HANDOFF.version)


def _create_exact_v26_store(database_path: Path) -> None:
    """The published occupancy store before Runner evidence existed."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_runner_evidence_attempts(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V26_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V26_SCHEMA_HANDOFF.version)


def _insert_v27_receipt_witness(
    connection: sqlite3.Connection, *, access: bool
) -> None:
    configuration, package, request, execution = (
        "44" * 32,
        "33" * 32,
        "22" * 32,
        "11" * 32,
    )
    connection.execute(
        "INSERT INTO run_configuration_revisions VALUES (?, ?)",
        (configuration, b"frozen configuration"),
    )
    connection.execute(
        "INSERT INTO context_packages_v3 VALUES (?, ?)",
        (package, b"frozen manifest"),
    )
    connection.execute(
        "INSERT INTO node_execution_requests_v3 VALUES (?, ?, ?, ?, ?)",
        (request, execution, configuration, package, b"frozen request"),
    )
    connection.execute(
        "INSERT INTO node_receipts_v3 VALUES (?, ?, ?, ?, ?, ?)",
        (execution, "succeeded", "completed", request, package, "9f" * 32),
    )
    if access:
        connection.execute(
            "INSERT INTO node_receipt_access_v3 VALUES (?, ?, ?)",
            (execution, 0, "aa" * 32),
        )


def _create_exact_v27_store(database_path: Path, *, access: bool = False) -> None:
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_agent_attempts_trigger_to_v27(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V27_SCHEMA_HANDOFF.version,),
        )
        _insert_v27_receipt_witness(connection, access=access)
        connection.commit()
        _require_product_shape(connection, V27_SCHEMA_HANDOFF.version)


def _create_exact_v28_store(database_path: Path) -> None:
    """A current store without the queue admission table: the published V28 shape."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_agent_attempts_trigger_to_v27(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V28_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V28_SCHEMA_HANDOFF.version)


def _create_exact_v29_store(database_path: Path) -> None:
    """A current store with the pre-CANCELLED runs CHECK: the published V29 shape.

    Unlike V28's fixture, `queue_items` stays: it is the table V29 itself
    added, and this store already carries every hop up to and including it.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_agent_attempts_trigger_to_v27(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V29_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V29_SCHEMA_HANDOFF.version)


def _v27_living_rows(database_path: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(
            (table, *row)
            for table in (
                "run_configuration_revisions",
                "context_packages_v3",
                "node_execution_requests_v3",
                "node_receipts_v3",
            )
            for row in connection.execute(f"SELECT * FROM {table}")
        )


@pytest.mark.proves("empty-v27-access-store-migrates-with-living-rows-intact")
def test_populated_v27_with_empty_access_store_migrates_and_reopens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v27_store(database_path)
    before = _v27_living_rows(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "27" in shown.out and "28" in shown.out and "29" in shown.out
    assert _v27_living_rows(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (SCHEMA_VERSION,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE "
                "'node_receipt_access_v3%'"
            ).fetchall()
            == []
        )
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()


@pytest.mark.proves("nonempty-v27-access-store-is-refused-unaltered")
def test_nonempty_v27_access_store_is_refused_without_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v27_store(database_path, access=True)
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "node_receipt_access_v3" in shown.err and "will not alter" in shown.err
    assert _logical_dump(database_path) == before


def test_nonempty_access_store_rolls_back_the_whole_v26_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v26_store(database_path)
    with sqlite3.connect(database_path) as connection:
        _insert_v27_receipt_witness(connection, access=True)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    assert "node_receipt_access_v3" in capsys.readouterr().err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (26,)


def test_an_exact_v21_store_migrates_to_v22(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v21_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 21"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "21" in shown.out and "22" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        for table in (run_instants, attempt_instants, event_instants):
            assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
    engine.dispose()


def test_an_exact_v22_store_migrates_to_v23(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v22_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 22"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "22" in shown.out and "23" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()


def test_an_exact_v23_store_migrates_to_v24(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v23_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 23"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "23" in shown.out and "24" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()


def test_an_exact_v24_store_migrates_to_v25(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v24_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 24"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "24" in shown.out and "25" in shown.out
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
                sa.select(sa.func.count()).select_from(host_project_root_revisions)
            )
            == 0
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(host_occupancy_revisions)
            )
            == 0
        )
    engine.dispose()


def test_an_exact_v28_store_migrates_through_v29_to_v30(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v28_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 28"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "28" in shown.out and "29" in shown.out and "30" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(queue_items)) == 0
        )
    engine.dispose()


def test_an_exact_v29_store_migrates_to_v30(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v29_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 29"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "29" in shown.out and "30" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        revision_hash = "cc" * 32
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision_hash, document=b"post-v30-migration"
            )
        )
        connection.execute(
            runs.insert().values(
                run_id="post-v30-run",
                bootstrap_workflow_id="post-v30-workflow",
                revision_hash=revision_hash,
                workflow_format_version=1,
                agent_binding_set_hash=None,
                current_node_id="final",
                current_round_ordinal=FIRST_ROUND_ORDINAL,
                state="CANCELLED",
                state_version=0,
                last_event_sequence=0,
                terminal_hash="0" * 64,
            )
        )
        connection.commit()
    engine.dispose()


def test_an_exact_v25_store_migrates_through_v27_and_v28_and_v29_to_v30(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v25_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 25"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert all(step in shown.out for step in ("25", "26", "27", "28", "29", "30"))
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
                sa.select(sa.func.count()).select_from(host_occupancy_revisions)
            )
            == 0
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(host_occupancy_bindings)
            )
            == 0
        )
    engine.dispose()


def test_an_exact_v26_store_migrates_through_v27_and_v28_and_v29_to_v30(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v26_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 26"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert all(step in shown.out for step in ("26", "27", "28", "29", "30"))
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        assert tuple(agent_attempts.c.keys())[-5:] == (
            "runner_manifest_id",
            "runner_generation_id",
            "runner_invocation_id",
            "runner_terminal_evidence_hash",
            "runner_evidence_acceptance_phase",
        )
    engine.dispose()


def test_v26_attempt_bytes_cross_v27_and_v28_unchanged_with_none_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    assert (V27_SCHEMA_HANDOFF.version, agent_attempts.name) in PUBLISHED_TABLE_SHAPES
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    request = attempt_request(runtime, "migration/v26-populated")
    DbosAgentAttemptStore(runtime.engine).prepare(agent_attempt_execution(request))
    runtime.close()

    with sqlite3.connect(database_path) as connection:
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_runner_evidence_attempts(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V26_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V26_SCHEMA_HANDOFF.version)
        predecessor_columns = tuple(
            str(record[1])
            for record in connection.execute("PRAGMA table_info(agent_attempts)")
        )
        predecessor_row = connection.execute("SELECT * FROM agent_attempts").fetchone()
    assert predecessor_row is not None

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        projected = ", ".join(predecessor_columns)
        assert (
            connection.execute(f"SELECT {projected} FROM agent_attempts").fetchone()
            == predecessor_row
        )
        runner_fields = connection.execute(
            "SELECT runner_manifest_id, runner_generation_id, runner_invocation_id, "
            "runner_terminal_evidence_hash, runner_evidence_acceptance_phase "
            "FROM agent_attempts"
        ).fetchone()
    assert runner_fields == (None, None, None, None, "NONE")


@pytest.mark.parametrize(
    "collision_sql",
    (
        "CREATE TABLE agent_attempts_before_runner_evidence(wrong TEXT)",
        "CREATE VIEW agent_attempts_before_runner_evidence AS SELECT 1 AS wrong",
    ),
)
def test_a_refused_v27_hop_rolls_back_the_attempt_rebuild(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    collision_sql: str,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v26_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "agent_attempts_before_runner_evidence" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (26,)


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE host_occupancy_bindings(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW host_occupancy_bindings AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_occupancy_hop_rolls_back_the_first_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The second occupancy object already exists, so the first create undoes.

    V25→V26 creates `host_occupancy_revisions` then `host_occupancy_bindings`
    then CAS 25→26 in one transaction. A name already holding the second
    object refuses the hop after the first table exists in that transaction.
    Rollback must leave no occupancy table, no occupancy trigger, and version 25.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v25_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "host_occupancy_bindings" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (25,)
        names = {
            record[0]
            for record in connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'host_occupancy%'"
            )
        }
        assert names == {"host_occupancy_bindings"}


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
    """A middle hop refuses, so the three that already ran are undone with it.

    The failure-code hop rebuilds `agent_attempts` under a parking name, so any
    object already holding that name is a collision the hop refuses by name --
    after the three earlier steps completed inside the same transaction, and
    before the hops that follow it can run at all.
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


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE runs_before_the_round_column(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW runs_before_the_round_column AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_round_column_hop_rolls_back_every_earlier_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The round hop refuses, so every step that already ran is undone with it.

    The round hop rebuilds `runs` under a parking name, so any object already
    holding that name is a collision the hop refuses by name -- after every
    earlier step completed inside the same transaction.
    """
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "runs_before_the_round_column" in shown.err
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
            "CREATE TABLE "
            "agent_configuration_revisions_before_workspace_tools(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW agent_configuration_revisions_before_workspace_tools "
            "AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_capability_hop_rolls_back_every_earlier_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The last hop refuses, so every step that already ran is undone with it.

    It rebuilds `agent_configuration_revisions` under a parking name, so any
    object already holding that name is a collision it refuses by name -- after
    every earlier step completed inside the same transaction.
    """
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "agent_configuration_revisions_before_workspace_tools" in shown.err
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


def test_a_foreign_trigger_name_collision_is_refused_without_altering_the_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE foreign_objects(value TEXT);
            CREATE TRIGGER run_inputs_v3_no_update
            BEFORE UPDATE ON foreign_objects BEGIN
              SELECT RAISE(ABORT, 'foreign object is immutable');
            END;
            """
        )
        connection.commit()
    before_bytes = database_path.read_bytes()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "run_inputs_v3_no_update" in shown.err
    assert "will not alter" in shown.err
    assert database_path.read_bytes() == before_bytes
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)


def _create_exact_v31_store(database_path: Path) -> None:
    """A current store with the pre-#584 attempt trigger: the published V31 shape.

    V31 differs from the current schema only by the never-launched runner-cancel
    branch #584 added to `agent_attempts_state_transition`; the fixture is a
    fresh store with that trigger reverted to its V31 grammar. The pinned V31
    fingerprint refuses it the moment a character drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        connection.execute("DROP TRIGGER agent_attempts_state_transition")
        connection.execute(_V27_AGENT_ATTEMPT_STATE_TRANSITION)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V31_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V31_SCHEMA_HANDOFF.version)


def test_an_exact_v31_store_migrates_to_v32_by_a_trigger_swap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v31_store(database_path)
    with sqlite3.connect(database_path) as connection:
        table_before = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_attempts'"
        ).fetchone()
        trigger_before = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='trigger' AND name='agent_attempts_state_transition'"
        ).fetchone()
    assert trigger_before is not None and "NEVER_LAUNCHED" not in trigger_before[0]

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 31"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "31" in shown.out and "32" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()

    with sqlite3.connect(database_path) as connection:
        table_after = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_attempts'"
        ).fetchone()
        trigger_after = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='trigger' AND name='agent_attempts_state_transition'"
        ).fetchone()
    # The hop moved no table shape -- only the trigger grammar changed.
    assert table_after == table_before
    assert trigger_after is not None and "NEVER_LAUNCHED" in trigger_after[0]


def test_a_populated_v31_runner_attempt_survives_the_v32_trigger_swap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    runtime, store, execution, _binding = _bound(
        tmp_path, "migration/v31-runner-attempt"
    )
    durable = store.load(execution.attempt_id)
    assert durable.runner_manifest_id is not None
    assert durable.runner_invocation_id is None
    runtime.close()

    with sqlite3.connect(database_path) as connection:
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        connection.execute("DROP TRIGGER agent_attempts_state_transition")
        connection.execute(_V27_AGENT_ATTEMPT_STATE_TRANSITION)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V31_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V31_SCHEMA_HANDOFF.version)
        predecessor_row = connection.execute("SELECT * FROM agent_attempts").fetchone()
    assert predecessor_row is not None

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute("SELECT * FROM agent_attempts").fetchone()
            == predecessor_row
        )
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (SCHEMA_VERSION,)


def _create_exact_v32_store(database_path: Path) -> None:
    """A current store without the connection table: the published V32 shape.

    V32 differs from the current schema only by the project-source connection
    table #567 added, so the fixture is a fresh store with that table and its
    immutability trigger pair removed. The pinned V32 fingerprint refuses it
    the moment a character drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V32_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V32_SCHEMA_HANDOFF.version)


def test_an_exact_v32_store_migrates_to_v33_by_adding_the_connection_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v32_store(database_path)
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='host_project_source_connection_revisions'"
            ).fetchone()
            is None
        )

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 32"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "32" in shown.out and "33" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()

    with sqlite3.connect(database_path) as connection:
        trigger_names = {
            str(record[0])
            for record in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='host_project_source_connection_revisions'"
            )
        }
    assert trigger_names == {
        "host_project_source_connection_revisions_no_update",
        "host_project_source_connection_revisions_no_delete",
    }


def test_populated_v32_host_configuration_rows_survive_the_v33_table_add(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v32_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired):
        initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        root = ProjectRootRevision(ProjectId("studio"), 1, tmp_path)
        connection.execute(
            "INSERT INTO host_project_root_revisions VALUES (?, ?, ?, ?)",
            (
                root.revision_hash.value,
                root.project_id.value,
                root.revision_number,
                str(root.root_path),
            ),
        )
        connection.commit()
        predecessor_row = connection.execute(
            "SELECT * FROM host_project_root_revisions"
        ).fetchone()
    assert predecessor_row is not None

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute("SELECT * FROM host_project_root_revisions").fetchone()
            == predecessor_row
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM host_project_source_connection_revisions"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (SCHEMA_VERSION,)


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE host_project_source_connection_revisions(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW host_project_source_connection_revisions AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_connection_table_hop_rolls_back_the_trigger_swap_before_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The last step refuses, so the v31→v32 swap that already ran is undone.

    V32→V33 creates the connection table; a name already holding that object
    refuses the hop by name, after the trigger-swap step completed inside the
    same transaction. Rollback must leave version 31 and the store logically
    untouched.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v31_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "host_project_source_connection_revisions" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (31,)


def _create_exact_v33_store(database_path: Path) -> None:
    """A current store with the pre-#671 answer table: the published V33 shape.

    V33 differs from the current schema only in `wait_answers`: keyed by run and
    node, without the round, and with a payload trigger whose column list does
    not name one. The fixture is a fresh store with that table and its payload
    trigger restored to their V33 text, and the pinned V33 fingerprint refuses
    it the moment a character drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V33_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V33_SCHEMA_HANDOFF.version)


_ANSWER_NODE_ID = "approve"


def _v33_wait_answer_values(
    run_id: RunId, revision_hash: WorkflowRevisionHash, answer_bytes: bytes
) -> tuple[str | bytes, ...]:
    """One predecessor answer row, derived from the identities production derives."""
    execution_id = NodeExecutionId.for_node(run_id, revision_hash, _ANSWER_NODE_ID)
    return (
        run_id.value,
        revision_hash.value,
        _ANSWER_NODE_ID,
        execution_id.value,
        answer_bytes,
        Sha256Hash.of(answer_bytes).value,
        answer_workflow_id_for(execution_id),
    )


def _populate_v33_wait_answers(database_path: Path) -> None:
    """One resting run holding a PENDING answer, one finished run holding an APPLIED.

    Both states have to cross the hop, because they are the two halves of the
    one thing this table exists for: an answer already written and not yet
    applied, and one whose transition already happened.
    """

    revision = WorkflowRevision(b"name: freigabe\n")
    resting_run = RunId("live/wartet-noch")
    answered_run = RunId("live/beantwortet")
    terminal_hash = Sha256Hash.of(b"the run this answer finished")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO workflow_revisions (revision_hash, document) VALUES (?, ?)",
            (revision.revision_hash.value, revision.document),
        )
        connection.executemany(
            "INSERT INTO runs (run_id, bootstrap_workflow_id, revision_hash, "
            "workflow_format_version, current_node_id, current_round_ordinal, "
            "state, state_version, last_event_sequence, terminal_hash) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, 1, 1, ?)",
            [
                (
                    resting_run.value,
                    f"bootstrap-{resting_run.value}",
                    revision.revision_hash.value,
                    _ANSWER_NODE_ID,
                    FIRST_ROUND_ORDINAL,
                    RunState.WAITING_INPUT.value,
                    None,
                ),
                (
                    answered_run.value,
                    f"bootstrap-{answered_run.value}",
                    revision.revision_hash.value,
                    _ANSWER_NODE_ID,
                    FIRST_ROUND_ORDINAL,
                    RunState.COMPLETED.value,
                    terminal_hash.value,
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO wait_answers (run_id, revision_hash, node_id, "
            "node_execution_id, answer_bytes, answer_hash, answer_workflow_id, "
            "state, state_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                _v33_wait_answer_values(
                    resting_run, revision.revision_hash, b'"noch nicht"'
                )
                + (WaitAnswerState.PENDING.value, 0),
                _v33_wait_answer_values(
                    answered_run, revision.revision_hash, b'"freigegeben"'
                )
                + (WaitAnswerState.APPLIED.value, 1),
            ],
        )
        connection.commit()


def test_an_exact_v33_store_migrates_to_v34_by_rekeying_the_answer_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v33_store(database_path)

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 33"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "33" in shown.out and "34" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()

    with sqlite3.connect(database_path) as connection:
        key_columns = tuple(
            str(record[1])
            for record in connection.execute("PRAGMA table_info(wait_answers)")
            if int(record[5]) > 0
        )
        parents = tuple(
            (str(record[2]), str(record[3]), str(record[4]))
            for record in connection.execute("PRAGMA foreign_key_list(wait_answers)")
        )
    assert key_columns == ("node_execution_id",)
    assert set(parents) == {
        ("runs", "run_id", "run_id"),
        ("runs", "revision_hash", "revision_hash"),
    }


def test_pending_and_applied_v33_answers_survive_the_v34_rekey_as_round_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v33_store(database_path)
    _populate_v33_wait_answers(database_path)
    with sqlite3.connect(database_path) as connection:
        predecessor_rows = connection.execute(
            "SELECT run_id, revision_hash, node_id, node_execution_id, answer_bytes, "
            "answer_hash, answer_workflow_id, state, state_version FROM wait_answers "
            "ORDER BY run_id"
        ).fetchall()
    assert len(predecessor_rows) == 2

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        carried = connection.execute(
            "SELECT run_id, revision_hash, node_id, node_execution_id, answer_bytes, "
            "answer_hash, answer_workflow_id, state, state_version FROM wait_answers "
            "ORDER BY run_id"
        ).fetchall()
        rounds = connection.execute(
            "SELECT state, round_ordinal FROM wait_answers ORDER BY run_id"
        ).fetchall()
    assert carried == predecessor_rows
    assert rounds == [
        (WaitAnswerState.APPLIED.value, FIRST_ROUND_ORDINAL),
        (WaitAnswerState.PENDING.value, FIRST_ROUND_ORDINAL),
    ]


def test_the_three_answer_triggers_are_live_again_after_the_v34_rekey(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rebuild drops every trigger, so each one is proved by what it refuses."""

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v33_store(database_path)
    _populate_v33_wait_answers(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="bindings are immutable"):
            connection.execute("UPDATE wait_answers SET round_ordinal = 2")
        with pytest.raises(sqlite3.IntegrityError, match="invalid wait answer"):
            connection.execute(
                "UPDATE wait_answers SET state = 'PENDING', state_version = 0 "
                "WHERE state = 'APPLIED'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="answers are immutable"):
            connection.execute("DELETE FROM wait_answers")


def test_a_refused_answer_rekey_leaves_the_v33_store_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A name already holding the parking object refuses before the first statement."""

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v33_store(database_path)
    _populate_v33_wait_answers(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"CREATE TABLE {_PREDECESSOR_WAIT_ANSWERS} (wrong TEXT)")
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert _PREDECESSOR_WAIT_ANSWERS in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (33,)


def _enqueue_a_three_argument_answer_workflow(
    database_path: Path,
    application_version: str,
    revision_hash: WorkflowRevisionHash,
    execution_id: NodeExecutionId,
) -> None:
    """Record the answer invocation a store written before the round existed holds.

    A predecessor enqueued run, revision and node and nothing else. Recording it
    under the identity the answer will be minted with is what makes it the same
    workflow the submission would otherwise have enqueued: the submission's own
    enqueue then finds the id taken and adds nothing, so what stands in the queue
    across the hop is the three-argument shape and only that.
    """

    engine = create_canonical_engine(database_path)
    client = DBOSClient(system_database_engine=engine, use_listen_notify=False)
    try:
        options: EnqueueOptions = {
            "workflow_name": ANSWER_WORKFLOW_NAME,
            "queue_name": QUEUE_NAME,
            "workflow_id": answer_workflow_id_for(execution_id),
            "app_version": application_version,
        }
        client.enqueue(options, RUN.value, revision_hash.value, WAIT_NODE)
    finally:
        client.destroy()
        engine.dispose()


def _recorded_invocation(
    serialized: str,
) -> tuple[tuple[object, ...], dict[str, object]]:
    """Every argument DBOS really recorded, read the way DBOS reads them.

    Both halves of the call, because the positional ones alone do not say what
    the recovered workflow is handed: three positional arguments and
    `round_ordinal` as a keyword would satisfy an assertion about the tuple and
    would need no compatibility default at all. The queue row is the artifact
    under test, so it is decoded whole rather than trusted.
    """
    recorded = DefaultSerializer().deserialize(serialized)
    return tuple(recorded["args"]), dict(recorded["kwargs"])


def _downgrade_a_driven_store_to_v33(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V33_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V33_SCHEMA_HANDOFF.version)


def test_a_v33_answer_enqueued_without_a_round_still_applies_after_the_v34_hop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The answer a predecessor accepted is applied by the runtime that comes after.

    This is the sentence the hop has to earn. An operator answered on the old
    schema; the process holding the run died before the answer workflow ran; the
    store was migrated offline; a new process came up. Nothing about that
    sequence is arranged after the fact -- the answer workflow is recorded with
    the three arguments a predecessor really wrote, the first runtime is closed
    while the answer is still PENDING and nothing is left to consume it, and the
    store is put back into its exact published V33 shape, fingerprint and all,
    with that PENDING row inside it.

    What is then asserted is that the answer is not stranded: it becomes APPLIED
    in the first round, writes exactly one WAIT_ANSWERED event, and carries the
    line on to the heir its author declared -- run by a runtime that never saw a
    byte of it in memory.
    """

    database_path = tmp_path / "atelier.sqlite"
    recording = recording_provider()
    paused = wait_runtime_over(tmp_path, recording)
    paused.initialize_storage()
    try:
        workflow = start_and_launch(paused, WAIT_IN_THE_MIDDLE)
        wait_for_state(paused, RunState.WAITING_INPUT)
        application_version = paused.settings.application_version
    finally:
        paused.close()

    execution_id = NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE)
    _enqueue_a_three_argument_answer_workflow(
        database_path, application_version, workflow.revision_hash, execution_id
    )
    engine = create_canonical_engine(database_path)
    try:
        accepted = answer_wait_result(
            RUN,
            workflow.revision_hash,
            WAIT_NODE,
            ANSWER,
            DbosWaitAnswerer(engine, application_version),
        )
    finally:
        engine.dispose()
    assert isinstance(accepted, AnswerAcceptedPending), accepted

    _downgrade_a_driven_store_to_v33(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT state FROM wait_answers WHERE node_execution_id = ?",
            (execution_id.value,),
        ).fetchone() == (WaitAnswerState.PENDING.value,)
        recorded_inputs = connection.execute(
            "SELECT inputs FROM workflow_status WHERE workflow_uuid = ?",
            (answer_workflow_id_for(execution_id),),
        ).fetchone()
    assert recorded_inputs is not None
    assert _recorded_invocation(str(recorded_inputs[0])) == (
        (RUN.value, workflow.revision_hash.value, WAIT_NODE),
        {},
    )

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    recovered = wait_runtime_over(tmp_path, recording)
    try:
        recovered.launch()
        wait_for_state(recovered, RunState.COMPLETED)
        with recovered.engine.connect() as connection:
            stored = connection.execute(sa.select(wait_answers)).mappings().one()
            answered = connection.execute(
                sa.select(run_events.c.node_id, run_events.c.round_ordinal).where(
                    run_events.c.event_kind == RunEventKind.WAIT_ANSWERED.value
                )
            ).all()
            heirs = (
                connection.execute(
                    sa.select(run_events.c.node_id).where(
                        run_events.c.event_kind == RunEventKind.AGENT_COMPLETED.value
                    )
                )
                .scalars()
                .all()
            )
    finally:
        recovered.close()

    assert str(stored["state"]) == WaitAnswerState.APPLIED.value
    assert int(stored["round_ordinal"]) == FIRST_ROUND_ORDINAL
    assert bytes(stored["answer_bytes"]) == ANSWER
    assert answered == [(WAIT_NODE, FIRST_ROUND_ORDINAL)]
    assert list(heirs) == ["implement", "review"]


def _create_exact_v34_store(database_path: Path) -> None:
    """A current store with the pre-#668 event vocabulary: the published V34 shape.

    V34 differs from the current schema only in `run_events`, whose kind CHECK
    does not yet name `WAIT_CANCELLED`. The fixture is a fresh store with that
    table rebuilt into its V34 text, and the pinned V34 fingerprint refuses it
    the moment a character drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _revert_wait_cancelled_event_kind(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V34_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V34_SCHEMA_HANDOFF.version)


_PAUSED_RUN = RunId("live/haelt-am-tor")
_PAUSED_NODE = "vorbereiten"


_ACTION_NODE = "wirken"
_FIRST_ATTEMPT = AgentAttemptId("aa" * 32)
_REPLACEMENT_ATTEMPT = AgentAttemptId("bb" * 32)
_CANCEL_COMMAND = "cancel/erste-fassung"
_EFFECT_KEY = LogicalEffectKey("wirken/einmal")
_EFFECT_RESULT = b'{"outcome":"CONFIRMED"}'


def _paused_run_event_log(revision_hash: WorkflowRevisionHash) -> tuple[RunEvent, ...]:
    """The event log one paused run really holds, derived by the contract.

    One event of every family the table has optional columns for -- an attempt
    cancelled and replaced, a completion carrying its agent receipt, an action
    bound to its effect receipt, and the pause the run rests at -- because a hop
    is only proved to carry a column by a row that had something in it. The
    hashes are the ones production would frame; nothing here recomputes them a
    second way.
    """

    node_execution = NodeExecutionId.for_node(_PAUSED_RUN, revision_hash, _PAUSED_NODE)
    return (
        RunEvent(
            _PAUSED_RUN,
            revision_hash,
            1,
            _PAUSED_NODE,
            node_execution,
            RunEventKind.AGENT_CANCEL_REQUESTED,
            b'"abgebrochen"',
            attempt_binding=RunEventCancellationBinding(
                _FIRST_ATTEMPT,
                AGENT_ATTEMPT_ORDINAL,
                AgentAttemptReplacement.ONE,
                _CANCEL_COMMAND,
            ),
        ),
        RunEvent(
            _PAUSED_RUN,
            revision_hash,
            2,
            _PAUSED_NODE,
            node_execution,
            RunEventKind.AGENT_CANCELLED,
            b'"aufgeraeumt"',
            attempt_binding=RunEventCancellationBinding(
                _FIRST_ATTEMPT,
                AGENT_ATTEMPT_ORDINAL,
                AgentAttemptReplacement.ONE,
                _CANCEL_COMMAND,
                AgentAttemptCancellationDisposition.REAPED_AFTER_TERM,
                _REPLACEMENT_ATTEMPT,
            ),
        ),
        RunEvent(
            _PAUSED_RUN,
            revision_hash,
            3,
            _PAUSED_NODE,
            node_execution,
            RunEventKind.AGENT_COMPLETED,
            b'"fertig"',
            attempt_binding=RunEventAgentAttemptBinding(
                _REPLACEMENT_ATTEMPT, REPLACEMENT_AGENT_ATTEMPT_ORDINAL
            ),
            agent_receipt_hash=AgentReceiptHash.of(b'"fertig"'),
        ),
        RunEvent(
            _PAUSED_RUN,
            revision_hash,
            4,
            _ACTION_NODE,
            NodeExecutionId.for_node(_PAUSED_RUN, revision_hash, _ACTION_NODE),
            RunEventKind.ACTION_COMPLETED,
            _EFFECT_RESULT,
            receipt_logical_key=_EFFECT_KEY,
            receipt_result_hash=Sha256Hash.of(_EFFECT_RESULT),
        ),
        RunEvent(
            _PAUSED_RUN,
            revision_hash,
            5,
            _ANSWER_NODE_ID,
            NodeExecutionId.for_node(_PAUSED_RUN, revision_hash, _ANSWER_NODE_ID),
            RunEventKind.WAITING_INPUT,
            b"",
        ),
    )


_EVENT_COLUMNS = tuple(column.name for column in run_events.columns)
"""Every column the event table has, in its own order.

Read from the table rather than listed here, so a column a later hop adds is
carried by this fixture and compared by it without anybody remembering to.
"""

_INSERT_EVENT_STATEMENT = (
    f"INSERT INTO run_events ({', '.join(_EVENT_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _EVENT_COLUMNS)})"
)


def _event_row(event: RunEvent) -> tuple[object, ...]:
    binding = event.attempt_binding
    cancellation = binding if isinstance(binding, RunEventCancellationBinding) else None
    written: Mapping[str, object] = {
        "run_id": event.run_id.value,
        "revision_hash": event.revision_hash.value,
        "event_sequence": event.event_sequence,
        "node_id": event.node_id,
        "node_execution_id": event.node_execution_id.value,
        "round_ordinal": event.round_ordinal,
        "event_kind": event.event_kind.value,
        "payload": event.payload,
        "payload_hash": event.payload_hash.value,
        "receipt_logical_key": (
            None
            if event.receipt_logical_key is None
            else event.receipt_logical_key.value
        ),
        "receipt_result_hash": (
            None
            if event.receipt_result_hash is None
            else event.receipt_result_hash.value
        ),
        "event_hash": event.event_hash.value,
        "agent_attempt_id": None if binding is None else binding.attempt_id.value,
        "attempt_ordinal": None if binding is None else binding.attempt_ordinal,
        "cancellation_command_id": (
            None if cancellation is None else cancellation.command_id
        ),
        "replacement": (
            None if cancellation is None else cancellation.replacement.value
        ),
        "cancellation_disposition": (
            None
            if cancellation is None or cancellation.disposition is None
            else cancellation.disposition.value
        ),
        "replacement_attempt_id": (
            None
            if cancellation is None or cancellation.replacement_attempt_id is None
            else cancellation.replacement_attempt_id.value
        ),
        "agent_receipt_hash": (
            None if event.agent_receipt_hash is None else event.agent_receipt_hash.value
        ),
    }
    return tuple(written[name] for name in _EVENT_COLUMNS)


def _populate_paused_run_events(database_path: Path) -> WorkflowRevisionHash:
    """One run resting at its pause, with the events that carried it there."""

    revision = WorkflowRevision(b"name: torwaechter\n")
    events = _paused_run_event_log(revision.revision_hash)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO workflow_revisions (revision_hash, document) VALUES (?, ?)",
            (revision.revision_hash.value, revision.document),
        )
        connection.execute(
            "INSERT INTO runs (run_id, bootstrap_workflow_id, revision_hash, "
            "workflow_format_version, current_node_id, current_round_ordinal, "
            "state, state_version, last_event_sequence, terminal_hash) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, NULL)",
            (
                _PAUSED_RUN.value,
                f"bootstrap-{_PAUSED_RUN.value}",
                revision.revision_hash.value,
                _ANSWER_NODE_ID,
                FIRST_ROUND_ORDINAL,
                RunState.WAITING_INPUT.value,
                len(events),
                len(events),
            ),
        )
        _seed_effect_receipt(connection, revision.revision_hash)
        connection.executemany(
            _INSERT_EVENT_STATEMENT, [_event_row(event) for event in events]
        )
        connection.commit()
    return revision.revision_hash


def _seed_effect_receipt(
    connection: sqlite3.Connection, revision_hash: WorkflowRevisionHash
) -> None:
    """The receipt an ACTION_COMPLETED event points at, so its binding resolves.

    A migration checks foreign keys before it commits, so an event carrying a
    receipt key nothing answers would refuse the whole hop rather than prove it.
    """

    request_hash = Sha256Hash.of(b"wirken/anfrage").value
    shared = (
        _EFFECT_KEY.value,
        _PAUSED_RUN.value,
        b"wirken/anfrage",
        request_hash,
        revision_hash.value,
        "loopback-v1",
        "loopback-test",
        "operational/loopback",
    )
    connection.execute(
        "INSERT INTO effect_intents (logical_key, run_id, canonical_request, "
        "request_hash, workflow_revision_hash, adapter_revision, "
        "destination_identity, adapter_operational_identity, state, state_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (*shared, EffectIntentState.CONFIRMED.value),
    )
    connection.execute(
        "INSERT INTO effect_receipts (logical_key, run_id, canonical_request, "
        "request_hash, workflow_revision_hash, adapter_revision, "
        "destination_identity, adapter_operational_identity, effect_id, result, "
        "result_hash, confirmation_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            *shared,
            "effect/einmal",
            _EFFECT_RESULT,
            Sha256Hash.of(_EFFECT_RESULT).value,
            ConfirmationSource.ADAPTER_EXECUTION.value,
        ),
    )


def _stored_event_rows(database_path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            f"SELECT {', '.join(_EVENT_COLUMNS)} FROM run_events "
            "ORDER BY run_id, event_sequence"
        ).fetchall()


def test_an_exact_v34_store_migrates_to_v35_by_widening_the_event_vocabulary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v34_store(database_path)

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 34"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "34" in shown.out and "35" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()


def test_every_v34_event_crosses_the_v35_rebuild_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hop widens what may be written; it rewrites nothing already written."""

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v34_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    predecessor_rows = _stored_event_rows(database_path)
    assert predecessor_rows == [
        _event_row(event) for event in _paused_run_event_log(revision_hash)
    ]

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    assert _stored_event_rows(database_path) == predecessor_rows


def test_a_v35_store_admits_the_wait_cancellation_its_predecessor_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one sentence the hop exists for, asked of both sides of it.

    Written straight at the table rather than through the store, because what is
    under test here is the CHECK the hop moved -- the store path that mints this
    event is proved where the run is actually driven.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v34_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    cancellation = RunEvent(
        _PAUSED_RUN,
        revision_hash,
        len(_paused_run_event_log(revision_hash)) + 1,
        _ANSWER_NODE_ID,
        NodeExecutionId.for_node(_PAUSED_RUN, revision_hash, _ANSWER_NODE_ID),
        RunEventKind.WAIT_CANCELLED,
        RunCancelCommandId.for_key("operator-key").value.encode("utf-8"),
    )

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        connection.execute(_INSERT_EVENT_STATEMENT, _event_row(cancellation))

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        connection.execute(_INSERT_EVENT_STATEMENT, _event_row(cancellation))
        connection.commit()
    assert _stored_event_rows(database_path)[-1] == _event_row(cancellation)


def test_the_event_log_is_append_only_again_after_the_v35_rebuild(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rebuild drops every trigger and index, so each is proved by what it refuses.

    An event log that could be updated, deleted, or made to hold two entries of
    one kind for one execution would be no evidence at all, and a terminal hash
    folded over it would be a hash over something that can still change.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v34_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()
    pause_again = _pause_at_sequence(revision_hash, _UNTAKEN_SEQUENCE)

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
            connection.execute("UPDATE run_events SET node_id = 'anders'")
        with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
            connection.execute("DELETE FROM run_events")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(_INSERT_EVENT_STATEMENT, _event_row(pause_again))


def test_a_refused_event_vocabulary_hop_leaves_the_v34_store_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A name already holding the parking object refuses before the first statement."""

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v34_store(database_path)
    _populate_paused_run_events(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f"CREATE TABLE {_PREDECESSOR_WAIT_UNCANCELLABLE_RUN_EVENTS} (wrong TEXT)"
        )
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert _PREDECESSOR_WAIT_UNCANCELLABLE_RUN_EVENTS in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (34,)


_PARKED_CURRENT_RUN_EVENTS_V36 = "run_events_after_the_round_scoped_key"


def _revert_the_round_scoped_event_key(connection: sqlite3.Connection) -> None:
    """Restore the once-per-node event key the #658 hop re-scoped to the round.

    Every schema up to V35 keyed an attempt-free event by its node and run at
    once, so a store that claims one of those versions has to hold that key
    again -- rebuilt from the shape and the index set V35 published, which is
    also what proves those two records are what a V35 store really was.
    """

    _rebuild_product_table(
        connection,
        run_events,
        _PARKED_CURRENT_RUN_EVENTS_V36,
        _RUN_EVENTS_TRIGGERS,
        SCHEMA_VERSION,
        V35_SCHEMA_HANDOFF.version,
    )


def _create_exact_v35_store(database_path: Path) -> None:
    """A current store keyed the pre-#658 way: the published V35 shape.

    V35 differs from the current schema in the scope of one index -- no column,
    no CHECK, no trigger -- and the pinned V35 fingerprint refuses the fixture
    the moment anything else about it drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _revert_the_round_scoped_event_key(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V35_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V35_SCHEMA_HANDOFF.version)


_UNTAKEN_SEQUENCE = 9
"""An event sequence past every one the seeded log took.

A duplicate has to be written at a free primary key, or the key it collides on
would be the run's own (run, sequence) rather than the one under test.
"""


def _pause_at_sequence(
    revision_hash: WorkflowRevisionHash,
    event_sequence: int,
    round_ordinal: int = FIRST_ROUND_ORDINAL,
) -> RunEvent:
    """The pause the wait node writes in one round, at one place in the log."""

    return RunEvent(
        _PAUSED_RUN,
        revision_hash,
        event_sequence,
        _ANSWER_NODE_ID,
        NodeExecutionId.for_node(
            _PAUSED_RUN, revision_hash, _ANSWER_NODE_ID, round_ordinal
        ),
        RunEventKind.WAITING_INPUT,
        b"",
        round_ordinal=round_ordinal,
    )


def test_an_exact_v35_store_migrates_to_v36_by_rescoping_the_event_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v35_store(database_path)

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 35"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "35" in shown.out and "36" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()


def test_every_v35_event_column_crosses_the_v36_hop_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hop moves a key; it reads and writes no row at all.

    Every column of every row is compared, over a log carrying one event of each
    family the table keeps optional columns for -- a hop that lost a receipt
    binding, a cancellation disposition or a replacement attempt would otherwise
    pass on rows that never had one.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v35_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    predecessor_rows = _stored_event_rows(database_path)
    assert predecessor_rows == [
        _event_row(event) for event in _paused_run_event_log(revision_hash)
    ]
    assert all(
        any(row[_EVENT_COLUMNS.index(column)] is not None for row in predecessor_rows)
        for column in (
            "receipt_logical_key",
            "receipt_result_hash",
            "agent_attempt_id",
            "attempt_ordinal",
            "cancellation_command_id",
            "replacement",
            "cancellation_disposition",
            "replacement_attempt_id",
            "agent_receipt_hash",
        )
    )

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    assert _stored_event_rows(database_path) == predecessor_rows


def test_a_v36_store_admits_the_second_pause_its_predecessor_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one sentence the hop exists for, asked of both sides of it.

    Written straight at the table rather than through the store, because what is
    under test here is the key the hop re-scoped -- the run that actually turns a
    loop through two pauses is driven in `tests/integration/test_v3_wait_in_loop`.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v35_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    second_pause = _pause_at_sequence(
        revision_hash, _UNTAKEN_SEQUENCE, FIRST_ROUND_ORDINAL + 1
    )

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"),
    ):
        connection.execute(_INSERT_EVENT_STATEMENT, _event_row(second_pause))

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        connection.execute(_INSERT_EVENT_STATEMENT, _event_row(second_pause))
        connection.commit()
    assert _stored_event_rows(database_path)[-1] == _event_row(second_pause)


def test_one_round_still_holds_one_pause_after_the_v36_hop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the hop widens is which round may repeat a kind, never whether one may.

    The successor key is asked in the coordinates a reader asks in -- run,
    revision, node, round -- so a second pause of the round already stored is
    refused even when it names another execution, which is the state
    `_existing_event` would otherwise read back as two rows for one round.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v35_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()
    pause_again = _pause_at_sequence(revision_hash, _UNTAKEN_SEQUENCE)
    foreign_execution = _event_row(pause_again)
    foreign_execution = (
        foreign_execution[: _EVENT_COLUMNS.index("node_execution_id")]
        + ("f" * 64,)
        + foreign_execution[_EVENT_COLUMNS.index("node_execution_id") + 1 :]
    )

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
            connection.execute("UPDATE run_events SET node_id = 'anders'")
        with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
            connection.execute("DELETE FROM run_events")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(_INSERT_EVENT_STATEMENT, _event_row(pause_again))
        with pytest.raises(sqlite3.IntegrityError, match="run_events.round_ordinal"):
            connection.execute(_INSERT_EVENT_STATEMENT, foreign_execution)


def test_a_refused_key_rescope_takes_its_own_first_statement_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A foreign object under the successor's name refuses the hop, and nothing is half done.

    The hop drops one index before it creates the other, so this is the case
    that proves the two statements and the version stand or fall together: the
    predecessor's key is back afterwards and the store still reads V35.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v35_store(database_path)
    _populate_paused_run_events(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"CREATE TABLE {_ROUND_SCOPED_EVENT_INDEX} (wrong TEXT)")
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert _ROUND_SCOPED_EVENT_INDEX in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (35,)
