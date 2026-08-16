from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class ProductSchemaHandoff:
    version: int
    fingerprint_sha256: str


SCHEMA_VERSION = 10
_VERSION_NINE = 9
# Operator ruling 5307892458: no store compatibility until a named maturity.
# V9 remains the published predecessor; runtime never migrates it.
_OFFLINE_CUTOVER_VERSIONS = frozenset(range(1, SCHEMA_VERSION))
# V9 product tables equal V8. V10 adds the thin catalog/receipt subset.
_PRODUCT_SCHEMA_FINGERPRINT_SHA256 = {
    7: "0bf32217a1254ee64d84c4ed629244600d542211ac655e4405a0df51f857081b",
    8: "6ba76214cb567ffcdab46e5a3ae00fc10824b962f16a8036ce90590be0b79b38",
    9: "6ba76214cb567ffcdab46e5a3ae00fc10824b962f16a8036ce90590be0b79b38",
    10: "4a7bbd9bf07880868aa2f7ddae3e7262eb270f711d4fdc420f902457817bfff7",
}
V9_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_NINE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_NINE],
)
PRODUCT_SCHEMA_HANDOFF = ProductSchemaHandoff(
    SCHEMA_VERSION,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[SCHEMA_VERSION],
)

metadata = sa.MetaData()

atelier_schema_versions = sa.Table(
    "atelier_schema_versions",
    metadata,
    sa.Column("version", sa.Integer, primary_key=True),
)
workflow_revisions = sa.Table(
    "workflow_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("document", sa.LargeBinary, nullable=False),
    sa.CheckConstraint(
        "length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
runs = sa.Table(
    "runs",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("bootstrap_workflow_id", sa.Text, unique=True, nullable=False),
    sa.Column(
        "revision_hash",
        sa.Text,
        sa.ForeignKey("workflow_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("workflow_format_version", sa.Integer, nullable=False),
    sa.Column("agent_binding_set_hash", sa.Text, nullable=True),
    sa.Column("current_node_id", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_version", sa.Integer, nullable=False),
    sa.Column("last_event_sequence", sa.Integer, nullable=False),
    sa.Column("terminal_hash", sa.Text, nullable=True),
    sa.UniqueConstraint("run_id", "revision_hash"),
    sa.UniqueConstraint("run_id", "revision_hash", "agent_binding_set_hash"),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint("length(current_node_id) > 0"),
    sa.CheckConstraint("workflow_format_version IN (1, 2, 3)"),
    sa.CheckConstraint(
        "(workflow_format_version = 1 AND agent_binding_set_hash IS NULL) OR "
        "(workflow_format_version = 2 AND agent_binding_set_hash IS NOT NULL "
        "AND length(agent_binding_set_hash) = 64 "
        "AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*') OR "
        "(workflow_format_version = 3 AND (agent_binding_set_hash IS NULL OR "
        "(length(agent_binding_set_hash) = 64 "
        "AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*')))"
    ),
    sa.CheckConstraint(
        "state IN ('STARTED', 'WAITING_RECONCILIATION', 'WAITING_INPUT', 'COMPLETED')"
    ),
    sa.CheckConstraint("state_version >= 0"),
    sa.CheckConstraint("last_event_sequence >= 0"),
    sa.CheckConstraint(
        "(state = 'COMPLETED' AND terminal_hash IS NOT NULL "
        "AND length(terminal_hash) = 64 AND terminal_hash NOT GLOB '*[^0-9a-f]*') "
        "OR (state <> 'COMPLETED' AND terminal_hash IS NULL)"
    ),
)
auth_profile_revisions = sa.Table(
    "auth_profile_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("profile_id", sa.Text, nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("provider_id", sa.Text, nullable=False),
    sa.Column("auth_mode", sa.Text, nullable=False),
    sa.UniqueConstraint("profile_id", "revision_number"),
    sa.UniqueConstraint(
        "revision_hash",
        "profile_id",
        "revision_number",
        "provider_id",
        "auth_mode",
    ),
    sa.CheckConstraint(
        "length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(profile_id) BETWEEN 1 AND 1024"),
    sa.CheckConstraint("revision_number BETWEEN 1 AND 9223372036854775807"),
    sa.CheckConstraint("length(provider_id) BETWEEN 1 AND 64"),
    sa.CheckConstraint("provider_id GLOB '[a-z]*'"),
    sa.CheckConstraint("provider_id NOT GLOB '*[^a-z0-9._-]*'"),
    sa.CheckConstraint("auth_mode IN ('subscription', 'api_key')"),
)
agent_configuration_revisions = sa.Table(
    "agent_configuration_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column(
        "auth_profile_revision_hash",
        sa.Text,
        sa.ForeignKey("auth_profile_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("executor_revision", sa.Text, nullable=False),
    sa.Column("revision_format_version", sa.Integer, nullable=False),
    sa.Column("requested_capability", sa.Text, nullable=False),
    sa.UniqueConstraint(
        "revision_hash",
        "auth_profile_revision_hash",
        "model",
        "executor_revision",
    ),
    sa.UniqueConstraint(
        "revision_hash",
        "auth_profile_revision_hash",
        "model",
        "executor_revision",
        "revision_format_version",
        "requested_capability",
    ),
    sa.CheckConstraint(
        "length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(model) BETWEEN 1 AND 1024"),
    sa.CheckConstraint(
        "length(auth_profile_revision_hash) = 64 "
        "AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(executor_revision) BETWEEN 1 AND 1024"),
    sa.CheckConstraint("revision_format_version IN (1, 2)"),
    sa.CheckConstraint("requested_capability IN ('headless', 'interactive')"),
    sa.CheckConstraint(
        "revision_format_version = 2 OR requested_capability = 'headless'"
    ),
)

run_agent_bindings = sa.Table(
    "run_agent_bindings",
    metadata,
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.Column("binding_set_hash", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("agent_configuration_revision_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("run_id", "role"),
    sa.ForeignKeyConstraint(
        ("run_id", "revision_hash", "binding_set_hash"),
        ("runs.run_id", "runs.revision_hash", "runs.agent_binding_set_hash"),
    ),
    sa.ForeignKeyConstraint(
        ("agent_configuration_revision_hash",),
        ("agent_configuration_revisions.revision_hash",),
    ),
    sa.UniqueConstraint(
        "run_id",
        "revision_hash",
        "binding_set_hash",
        "role",
        "agent_configuration_revision_hash",
    ),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint(
        "length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(binding_set_hash) = 64 AND binding_set_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(role) BETWEEN 1 AND 1024"),
    sa.CheckConstraint(
        "length(agent_configuration_revision_hash) = 64 "
        "AND agent_configuration_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
effect_intents = sa.Table(
    "effect_intents",
    metadata,
    sa.Column("logical_key", sa.Text, primary_key=True),
    sa.Column("run_id", sa.Text, sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("canonical_request", sa.LargeBinary, nullable=False),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column(
        "workflow_revision_hash",
        sa.Text,
        sa.ForeignKey("workflow_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("adapter_revision", sa.Text, nullable=False),
    sa.Column("destination_identity", sa.Text, nullable=False),
    sa.Column("adapter_operational_identity", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_version", sa.Integer, nullable=False),
    sa.Column(
        "reconciliation_owner_command_id",
        sa.Text,
        sa.ForeignKey("reconcile_commands.command_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.UniqueConstraint("logical_key", "run_id", "workflow_revision_hash"),
    sa.ForeignKeyConstraint(
        ("run_id", "workflow_revision_hash"),
        ("runs.run_id", "runs.revision_hash"),
    ),
    sa.CheckConstraint("length(logical_key) > 0"),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint(
        "length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(workflow_revision_hash) = 64 "
        "AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(adapter_revision) > 0"),
    sa.CheckConstraint("length(destination_identity) > 0"),
    sa.CheckConstraint("length(adapter_operational_identity) > 0"),
    sa.CheckConstraint(
        "state IN ('PREPARED', 'WAITING_RECONCILIATION', 'RECONCILING', 'CONFIRMED')"
    ),
    sa.CheckConstraint("state_version >= 0"),
    sa.CheckConstraint(
        "(state = 'RECONCILING' "
        "AND reconciliation_owner_command_id IS NOT NULL "
        "AND length(reconciliation_owner_command_id) > 0) "
        "OR (state <> 'RECONCILING' "
        "AND reconciliation_owner_command_id IS NULL)"
    ),
)
reconcile_commands = sa.Table(
    "reconcile_commands",
    metadata,
    sa.Column("command_id", sa.Text, primary_key=True),
    sa.Column(
        "logical_key",
        sa.Text,
        sa.ForeignKey("effect_intents.logical_key"),
        nullable=False,
    ),
    sa.Column("expected_intent_version", sa.Integer, nullable=False),
    sa.Column("determination", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("evidence", sa.Text, nullable=False),
    sa.Column("found_effect_id", sa.Text, nullable=True),
    sa.Column("found_result", sa.LargeBinary, nullable=True),
    sa.Column("found_result_hash", sa.Text, nullable=True),
    sa.Column("state", sa.Text, nullable=False),
    sa.CheckConstraint("length(command_id) > 0"),
    sa.CheckConstraint("length(logical_key) > 0"),
    sa.CheckConstraint("expected_intent_version >= 0"),
    sa.CheckConstraint("determination IN ('FOUND', 'AUTHORITATIVE_NOT_FOUND')"),
    sa.CheckConstraint("length(actor) > 0"),
    sa.CheckConstraint("length(evidence) > 0"),
    sa.CheckConstraint(
        "(determination = 'FOUND' "
        "AND found_effect_id IS NOT NULL AND length(found_effect_id) > 0 "
        "AND found_result IS NOT NULL "
        "AND found_result_hash IS NOT NULL AND length(found_result_hash) = 64 "
        "AND found_result_hash NOT GLOB '*[^0-9a-f]*') "
        "OR (determination = 'AUTHORITATIVE_NOT_FOUND' "
        "AND found_effect_id IS NULL "
        "AND found_result IS NULL "
        "AND found_result_hash IS NULL)"
    ),
    sa.CheckConstraint("state IN ('PENDING', 'APPLIED', 'REJECTED_CONFLICT')"),
)
effect_receipts = sa.Table(
    "effect_receipts",
    metadata,
    sa.Column(
        "logical_key",
        sa.Text,
        sa.ForeignKey("effect_intents.logical_key"),
        primary_key=True,
    ),
    sa.Column("run_id", sa.Text, sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("canonical_request", sa.LargeBinary, nullable=False),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column(
        "workflow_revision_hash",
        sa.Text,
        sa.ForeignKey("workflow_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("adapter_revision", sa.Text, nullable=False),
    sa.Column("destination_identity", sa.Text, nullable=False),
    sa.Column("adapter_operational_identity", sa.Text, nullable=False),
    sa.Column("effect_id", sa.Text, nullable=False),
    sa.Column("result", sa.LargeBinary, nullable=False),
    sa.Column("result_hash", sa.Text, nullable=False),
    sa.Column("confirmation_source", sa.Text, nullable=False),
    sa.Column(
        "reconcile_command_id",
        sa.Text,
        sa.ForeignKey("reconcile_commands.command_id"),
        nullable=True,
    ),
    sa.UniqueConstraint(
        "logical_key", "run_id", "workflow_revision_hash", "result_hash"
    ),
    sa.ForeignKeyConstraint(
        ("logical_key", "run_id", "workflow_revision_hash"),
        (
            "effect_intents.logical_key",
            "effect_intents.run_id",
            "effect_intents.workflow_revision_hash",
        ),
    ),
    sa.CheckConstraint("length(logical_key) > 0"),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint(
        "length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(workflow_revision_hash) = 64 "
        "AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(adapter_revision) > 0"),
    sa.CheckConstraint("length(destination_identity) > 0"),
    sa.CheckConstraint("length(adapter_operational_identity) > 0"),
    sa.CheckConstraint("length(effect_id) > 0"),
    sa.CheckConstraint(
        "length(result_hash) = 64 AND result_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "confirmation_source IN "
        "('ADAPTER_READBACK', 'ADAPTER_EXECUTION', "
        "'OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION')"
    ),
    sa.CheckConstraint(
        "(confirmation_source IN ('ADAPTER_READBACK', 'ADAPTER_EXECUTION') "
        "AND reconcile_command_id IS NULL) "
        "OR (confirmation_source IN "
        "('OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION') "
        "AND reconcile_command_id IS NOT NULL "
        "AND length(reconcile_command_id) > 0)"
    ),
)
agent_receipts = sa.Table(
    "agent_receipts",
    metadata,
    sa.Column("node_execution_id", sa.Text, primary_key=True),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("workflow_revision_hash", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("executor_adapter_revision", sa.Text, nullable=False),
    sa.Column("executor_operational_identity", sa.Text, nullable=False),
    sa.Column("output_bytes", sa.LargeBinary, nullable=False),
    sa.Column("output_hash", sa.Text, nullable=False),
    sa.Column("receipt_hash", sa.Text, nullable=False, unique=True),
    sa.UniqueConstraint("run_id", "workflow_revision_hash", "node_id"),
    sa.ForeignKeyConstraint(
        ("run_id", "workflow_revision_hash"),
        ("runs.run_id", "runs.revision_hash"),
    ),
    sa.CheckConstraint(
        "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint(
        "length(workflow_revision_hash) = 64 "
        "AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(node_id) > 0"),
    sa.CheckConstraint("length(executor_adapter_revision) > 0"),
    sa.CheckConstraint("length(executor_operational_identity) > 0"),
    sa.CheckConstraint(
        "length(output_hash) = 64 AND output_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
agent_receipts_v2 = sa.Table(
    "agent_receipts_v2",
    metadata,
    sa.Column("node_execution_id", sa.Text, primary_key=True),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("workflow_revision_hash", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("binding_set_hash", sa.Text, nullable=False),
    sa.Column("agent_configuration_revision_hash", sa.Text, nullable=False),
    sa.Column("auth_profile_revision_hash", sa.Text, nullable=False),
    sa.Column("profile_id", sa.Text, nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("provider_id", sa.Text, nullable=False),
    sa.Column("auth_mode", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("executor_revision", sa.Text, nullable=False),
    sa.Column("executor_operational_identity", sa.Text, nullable=False),
    sa.Column("output_bytes", sa.LargeBinary, nullable=False),
    sa.Column("output_hash", sa.Text, nullable=False),
    sa.Column("receipt_hash", sa.Text, nullable=False, unique=True),
    sa.UniqueConstraint("run_id", "workflow_revision_hash", "node_id"),
    sa.ForeignKeyConstraint(
        (
            "run_id",
            "workflow_revision_hash",
            "binding_set_hash",
            "role",
            "agent_configuration_revision_hash",
        ),
        (
            "run_agent_bindings.run_id",
            "run_agent_bindings.revision_hash",
            "run_agent_bindings.binding_set_hash",
            "run_agent_bindings.role",
            "run_agent_bindings.agent_configuration_revision_hash",
        ),
    ),
    sa.ForeignKeyConstraint(
        (
            "agent_configuration_revision_hash",
            "auth_profile_revision_hash",
            "model",
            "executor_revision",
        ),
        (
            "agent_configuration_revisions.revision_hash",
            "agent_configuration_revisions.auth_profile_revision_hash",
            "agent_configuration_revisions.model",
            "agent_configuration_revisions.executor_revision",
        ),
    ),
    sa.ForeignKeyConstraint(
        (
            "auth_profile_revision_hash",
            "profile_id",
            "revision_number",
            "provider_id",
            "auth_mode",
        ),
        (
            "auth_profile_revisions.revision_hash",
            "auth_profile_revisions.profile_id",
            "auth_profile_revisions.revision_number",
            "auth_profile_revisions.provider_id",
            "auth_profile_revisions.auth_mode",
        ),
    ),
    sa.CheckConstraint(
        "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint(
        "length(workflow_revision_hash) = 64 "
        "AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(node_id) BETWEEN 1 AND 1024"),
    sa.CheckConstraint("length(role) BETWEEN 1 AND 1024"),
    sa.CheckConstraint(
        "length(binding_set_hash) = 64 AND binding_set_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(agent_configuration_revision_hash) = 64 "
        "AND agent_configuration_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(auth_profile_revision_hash) = 64 "
        "AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(profile_id) BETWEEN 1 AND 1024"),
    sa.CheckConstraint("revision_number BETWEEN 1 AND 9223372036854775807"),
    sa.CheckConstraint("length(provider_id) BETWEEN 1 AND 64"),
    sa.CheckConstraint("provider_id GLOB '[a-z]*'"),
    sa.CheckConstraint("provider_id NOT GLOB '*[^a-z0-9._-]*'"),
    sa.CheckConstraint("auth_mode IN ('subscription', 'api_key')"),
    sa.CheckConstraint("length(model) BETWEEN 1 AND 1024"),
    sa.CheckConstraint("length(executor_revision) BETWEEN 1 AND 1024"),
    sa.CheckConstraint("length(executor_operational_identity) BETWEEN 1 AND 1024"),
    sa.CheckConstraint(
        "typeof(output_bytes) = 'blob' AND length(output_bytes) <= 49152"
    ),
    sa.CheckConstraint(
        "length(output_hash) = 64 AND output_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
agent_attempts = sa.Table(
    "agent_attempts",
    metadata,
    sa.Column("attempt_id", sa.Text, primary_key=True),
    sa.Column("node_execution_id", sa.Text, nullable=False),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("executor_operational_identity", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("workflow_revision_hash", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("attempt_ordinal", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_version", sa.Integer, nullable=False),
    sa.Column("process_phase", sa.Text, nullable=False),
    sa.Column("process_owner_id", sa.Text, nullable=True),
    sa.Column("watchdog_generation_id", sa.Text, nullable=True),
    sa.Column("cancellation_command_id", sa.Text, nullable=True),
    sa.Column("cancellation_expected_state_version", sa.Integer, nullable=True),
    sa.Column("replacement", sa.Text, nullable=True),
    sa.Column("redrive_state", sa.Text, nullable=True),
    sa.Column("cancellation_disposition", sa.Text, nullable=True),
    sa.Column("cancellation_workflow_id", sa.Text, unique=True, nullable=True),
    sa.Column("failure_code", sa.Text, nullable=True),
    sa.Column(
        "receipt_hash",
        sa.Text,
        sa.ForeignKey("agent_receipts_v2.receipt_hash", ondelete="RESTRICT"),
        unique=True,
        nullable=True,
    ),
    sa.UniqueConstraint("node_execution_id", "attempt_ordinal"),
    sa.ForeignKeyConstraint(
        ("run_id", "workflow_revision_hash"),
        ("runs.run_id", "runs.revision_hash"),
    ),
    sa.CheckConstraint("length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint(
        "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(executor_operational_identity) BETWEEN 1 AND 1024"),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint(
        "length(workflow_revision_hash) = 64 "
        "AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(node_id) BETWEEN 1 AND 1024"),
    sa.CheckConstraint("attempt_ordinal IN (1, 2)"),
    sa.CheckConstraint(
        "process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', "
        "'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')"
    ),
    sa.CheckConstraint(
        "(process_phase = 'NONE' AND process_owner_id IS NULL "
        "AND watchdog_generation_id IS NULL) OR "
        "(process_phase = 'CLEANUP_ATTESTED' "
        "AND cancellation_disposition = 'NEVER_LAUNCHED' "
        "AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR "
        "(process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 "
        "AND length(watchdog_generation_id) BETWEEN 1 AND 1024)"
    ),
    sa.CheckConstraint(
        "(cancellation_command_id IS NULL "
        "AND cancellation_expected_state_version IS NULL "
        "AND replacement IS NULL AND redrive_state IS NULL "
        "AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) "
        "OR (length(cancellation_command_id) BETWEEN 1 AND 1024 "
        "AND cancellation_expected_state_version >= 0 "
        "AND replacement IN ('NONE', 'ONE') "
        "AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') "
        "AND length(cancellation_workflow_id) > 0 "
        "AND ((redrive_state = 'CLEANUP_ATTESTED' "
        "AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', "
        "'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', "
        "'OWNER_LOST_AFTER_PARENT_DEATH')) OR "
        "(redrive_state <> 'CLEANUP_ATTESTED' "
        "AND cancellation_disposition IS NULL)))"
    ),
    sa.CheckConstraint(
        "(state = 'PREPARED' AND state_version = 0 "
        "AND process_phase = 'NONE' AND cancellation_command_id IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state = 'PREPARED' AND state_version = 1 "
        "AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state = 'LAUNCH_ARMED' AND state_version = 1 "
        "AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') "
        "AND cancellation_command_id IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state = 'LAUNCH_ARMED' AND state_version >= 2 "
        "AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') "
        "AND cancellation_command_id IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state = 'CANCEL_REQUESTED' AND state_version >= 1 "
        "AND cancellation_command_id IS NOT NULL "
        "AND cancellation_disposition IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 "
        "AND process_phase = 'CLEANUP_ATTESTED' "
        "AND cancellation_command_id IS NOT NULL "
        "AND cancellation_disposition IS NOT NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state = 'SUCCEEDED' AND state_version >= 2 "
        "AND cancellation_command_id IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR "
        "(state = 'FAILED' AND state_version >= 2 "
        "AND cancellation_command_id IS NULL "
        "AND failure_code = 'PROCESS_EXITED_UNSUCCESSFULLY' "
        "AND receipt_hash IS NULL)"
    ),
)
run_events = sa.Table(
    "run_events",
    metadata,
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.Column("event_sequence", sa.Integer, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("node_execution_id", sa.Text, nullable=False),
    sa.Column("event_kind", sa.Text, nullable=False),
    sa.Column("payload", sa.LargeBinary, nullable=False),
    sa.Column("payload_hash", sa.Text, nullable=False),
    sa.Column("receipt_logical_key", sa.Text, nullable=True),
    sa.Column("receipt_result_hash", sa.Text, nullable=True),
    sa.Column("event_hash", sa.Text, nullable=False),
    sa.Column("agent_attempt_id", sa.Text, nullable=True),
    sa.Column("attempt_ordinal", sa.Integer, nullable=True),
    sa.Column("cancellation_command_id", sa.Text, nullable=True),
    sa.Column("replacement", sa.Text, nullable=True),
    sa.Column("cancellation_disposition", sa.Text, nullable=True),
    sa.Column("replacement_attempt_id", sa.Text, nullable=True),
    sa.PrimaryKeyConstraint("run_id", "event_sequence"),
    sa.ForeignKeyConstraint(
        ("run_id", "revision_hash"), ("runs.run_id", "runs.revision_hash")
    ),
    sa.ForeignKeyConstraint(
        (
            "receipt_logical_key",
            "run_id",
            "revision_hash",
            "receipt_result_hash",
        ),
        (
            "effect_receipts.logical_key",
            "effect_receipts.run_id",
            "effect_receipts.workflow_revision_hash",
            "effect_receipts.result_hash",
        ),
    ),
    sa.CheckConstraint("event_sequence > 0"),
    sa.CheckConstraint("length(node_id) > 0"),
    sa.CheckConstraint(
        "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED', "
        "'AGENT_CANCEL_REQUESTED', 'AGENT_CANCELLED', 'AGENT_INTERRUPTED', "
        "'ACTION_RECONCILIATION_REQUIRED', "
        "'ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED', 'WAITING_INPUT', "
        "'WAIT_ANSWERED', 'SUBWORKFLOW_COMPLETED')"
    ),
    sa.CheckConstraint(
        "length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint(
        "(event_kind IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') "
        "AND receipt_logical_key IS NOT NULL "
        "AND length(receipt_logical_key) > 0 "
        "AND receipt_result_hash IS NOT NULL "
        "AND length(receipt_result_hash) = 64 "
        "AND receipt_result_hash NOT GLOB '*[^0-9a-f]*' "
        "AND receipt_result_hash = payload_hash) "
        "OR (event_kind NOT IN "
        "('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') "
        "AND receipt_logical_key IS NULL AND receipt_result_hash IS NULL)"
    ),
    sa.CheckConstraint(
        "(agent_attempt_id IS NULL AND attempt_ordinal IS NULL "
        "AND cancellation_command_id IS NULL AND replacement IS NULL "
        "AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) "
        "OR (length(agent_attempt_id) = 64 "
        "AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' "
        "AND attempt_ordinal IN (1, 2) "
        "AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') "
        "AND cancellation_command_id IS NULL AND replacement IS NULL "
        "AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) "
        "OR (event_kind = 'AGENT_CANCEL_REQUESTED' "
        "AND length(cancellation_command_id) BETWEEN 1 AND 1024 "
        "AND replacement IN ('NONE', 'ONE') "
        "AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) "
        "OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') "
        "AND length(cancellation_command_id) BETWEEN 1 AND 1024 "
        "AND replacement IN ('NONE', 'ONE') "
        "AND cancellation_disposition IS NOT NULL)))"
    ),
)
sa.Index(
    "run_events_legacy_kind_unique",
    run_events.c.run_id,
    run_events.c.revision_hash,
    run_events.c.node_id,
    run_events.c.event_kind,
    unique=True,
    sqlite_where=run_events.c.agent_attempt_id.is_(None),
)
sa.Index(
    "run_events_legacy_execution_kind_unique",
    run_events.c.node_execution_id,
    run_events.c.event_kind,
    unique=True,
    sqlite_where=run_events.c.agent_attempt_id.is_(None),
)
sa.Index(
    "run_events_attempt_kind_unique",
    run_events.c.agent_attempt_id,
    run_events.c.event_kind,
    unique=True,
    sqlite_where=run_events.c.agent_attempt_id.is_not(None),
)
wait_answers = sa.Table(
    "wait_answers",
    metadata,
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("node_execution_id", sa.Text, nullable=False, unique=True),
    sa.Column("answer_bytes", sa.LargeBinary, nullable=False),
    sa.Column("answer_hash", sa.Text, nullable=False),
    sa.Column("answer_workflow_id", sa.Text, nullable=False, unique=True),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_version", sa.Integer, nullable=False),
    sa.PrimaryKeyConstraint("run_id", "node_id"),
    sa.ForeignKeyConstraint(
        ("run_id", "revision_hash"), ("runs.run_id", "runs.revision_hash")
    ),
    sa.CheckConstraint("length(node_id) > 0"),
    sa.CheckConstraint(
        "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(answer_hash) = 64 AND answer_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(answer_workflow_id) > 0"),
    sa.CheckConstraint("state IN ('PENDING', 'APPLIED')"),
    sa.CheckConstraint("state_version IN (0, 1)"),
    sa.CheckConstraint(
        "(state = 'PENDING' AND state_version = 0) "
        "OR (state = 'APPLIED' AND state_version = 1)"
    ),
)
_PUBLISHED_REVISION_KIND_SQL = (
    "kind IN ('workflow', 'schema', 'deterministic_operation', "
    "'adapter_operation', 'context_source', 'read_operation', 'profile', "
    "'skill', 'tool', 'policy', 'budget_policy', 'retry_policy', "
    "'cancellation_policy', 'scorecard_policy', 'selection_policy', "
    "'admission_policy', 'agent_definition')"
)
published_revisions = sa.Table(
    "published_revisions",
    metadata,
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.Column("document", sa.LargeBinary, nullable=False),
    sa.PrimaryKeyConstraint("kind", "revision_hash"),
    sa.CheckConstraint("length(kind) BETWEEN 1 AND 64"),
    sa.CheckConstraint(_PUBLISHED_REVISION_KIND_SQL),
    sa.CheckConstraint(
        "length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
catalog_lineages = sa.Table(
    "catalog_lineages",
    metadata,
    sa.Column("lineage_id", sa.Text, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("founding_revision_hash", sa.Text, nullable=False),
    sa.UniqueConstraint("kind", "founding_revision_hash"),
    sa.ForeignKeyConstraint(
        ("kind", "founding_revision_hash"),
        ("published_revisions.kind", "published_revisions.revision_hash"),
    ),
    sa.CheckConstraint("length(lineage_id) = 64 AND lineage_id NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint("length(kind) BETWEEN 1 AND 64"),
    sa.CheckConstraint(_PUBLISHED_REVISION_KIND_SQL),
    sa.CheckConstraint(
        "length(founding_revision_hash) = 64 "
        "AND founding_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
catalog_lineage_members = sa.Table(
    "catalog_lineage_members",
    metadata,
    sa.Column(
        "lineage_id",
        sa.Text,
        sa.ForeignKey("catalog_lineages.lineage_id"),
        nullable=False,
    ),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("lineage_id", "revision_number"),
    sa.UniqueConstraint("lineage_id", "revision_hash"),
    sa.CheckConstraint("revision_number >= 1"),
    sa.CheckConstraint(
        "length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
node_receipts_v3 = sa.Table(
    "node_receipts_v3",
    metadata,
    sa.Column("node_execution_id", sa.Text, primary_key=True),
    sa.Column("disposition", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("context_package_hash", sa.Text, nullable=False),
    sa.Column("receipt_hash", sa.Text, unique=True, nullable=False),
    sa.CheckConstraint(
        "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "disposition IN ('succeeded', 'failed', 'cancelled', 'blocked')"
    ),
    sa.CheckConstraint("length(reason) > 0"),
    sa.CheckConstraint(
        "length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(context_package_hash) = 64 "
        "AND context_package_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)

PRODUCT_TABLE_NAMES = frozenset(metadata.tables)

_PRODUCT_TRIGGERS = {
    "workflow_revisions_no_update": """
        CREATE TRIGGER workflow_revisions_no_update
        BEFORE UPDATE ON workflow_revisions BEGIN
          SELECT RAISE(ABORT, 'workflow revisions are immutable');
        END
    """,
    "workflow_revisions_no_delete": """
        CREATE TRIGGER workflow_revisions_no_delete
        BEFORE DELETE ON workflow_revisions BEGIN
          SELECT RAISE(ABORT, 'workflow revisions are immutable');
        END
    """,
    "runs_binding_no_update": """
        CREATE TRIGGER runs_binding_no_update
        BEFORE UPDATE OF run_id, bootstrap_workflow_id, revision_hash,
                         workflow_format_version, agent_binding_set_hash
        ON runs BEGIN
          SELECT RAISE(ABORT, 'run bindings are immutable');
        END
    """,
    "auth_profile_revisions_no_update": """
        CREATE TRIGGER auth_profile_revisions_no_update
        BEFORE UPDATE ON auth_profile_revisions BEGIN
          SELECT RAISE(ABORT, 'auth profile revisions are immutable');
        END
    """,
    "auth_profile_revisions_no_delete": """
        CREATE TRIGGER auth_profile_revisions_no_delete
        BEFORE DELETE ON auth_profile_revisions BEGIN
          SELECT RAISE(ABORT, 'auth profile revisions are immutable');
        END
    """,
    "agent_configuration_revisions_no_update": """
        CREATE TRIGGER agent_configuration_revisions_no_update
        BEFORE UPDATE ON agent_configuration_revisions BEGIN
          SELECT RAISE(ABORT, 'agent configuration revisions are immutable');
        END
    """,
    "agent_configuration_revisions_no_delete": """
        CREATE TRIGGER agent_configuration_revisions_no_delete
        BEFORE DELETE ON agent_configuration_revisions BEGIN
          SELECT RAISE(ABORT, 'agent configuration revisions are immutable');
        END
    """,
    "run_agent_bindings_no_update": """
        CREATE TRIGGER run_agent_bindings_no_update
        BEFORE UPDATE ON run_agent_bindings BEGIN
          SELECT RAISE(ABORT, 'run agent bindings are immutable');
        END
    """,
    "run_agent_bindings_no_delete": """
        CREATE TRIGGER run_agent_bindings_no_delete
        BEFORE DELETE ON run_agent_bindings BEGIN
          SELECT RAISE(ABORT, 'run agent bindings are immutable');
        END
    """,
    "effect_intents_binding_no_update": """
        CREATE TRIGGER effect_intents_binding_no_update
        BEFORE UPDATE OF logical_key, run_id, canonical_request, request_hash,
                         workflow_revision_hash, adapter_revision, destination_identity,
                         adapter_operational_identity
        ON effect_intents BEGIN
          SELECT RAISE(ABORT, 'effect intent bindings are immutable');
        END
    """,
    "effect_intents_no_delete": """
        CREATE TRIGGER effect_intents_no_delete
        BEFORE DELETE ON effect_intents BEGIN
          SELECT RAISE(ABORT, 'effect intents are immutable');
        END
    """,
    "effect_receipts_no_update": """
        CREATE TRIGGER effect_receipts_no_update
        BEFORE UPDATE ON effect_receipts BEGIN
          SELECT RAISE(ABORT, 'effect receipts are immutable');
        END
    """,
    "effect_receipts_no_delete": """
        CREATE TRIGGER effect_receipts_no_delete
        BEFORE DELETE ON effect_receipts BEGIN
          SELECT RAISE(ABORT, 'effect receipts are immutable');
        END
    """,
    "agent_receipts_no_update": """
        CREATE TRIGGER agent_receipts_no_update
        BEFORE UPDATE ON agent_receipts BEGIN
          SELECT RAISE(ABORT, 'agent receipts are immutable');
        END
    """,
    "agent_receipts_no_delete": """
        CREATE TRIGGER agent_receipts_no_delete
        BEFORE DELETE ON agent_receipts BEGIN
          SELECT RAISE(ABORT, 'agent receipts are immutable');
        END
    """,
    "agent_receipts_v2_no_update": """
        CREATE TRIGGER agent_receipts_v2_no_update
        BEFORE UPDATE ON agent_receipts_v2 BEGIN
          SELECT RAISE(ABORT, 'v2 agent receipts are immutable');
        END
    """,
    "agent_receipts_v2_no_delete": """
        CREATE TRIGGER agent_receipts_v2_no_delete
        BEFORE DELETE ON agent_receipts_v2 BEGIN
          SELECT RAISE(ABORT, 'v2 agent receipts are immutable');
        END
    """,
    "reconcile_commands_payload_no_update": """
        CREATE TRIGGER reconcile_commands_payload_no_update
        BEFORE UPDATE OF command_id, logical_key, expected_intent_version,
                         determination, actor, evidence, found_effect_id,
                         found_result, found_result_hash
        ON reconcile_commands BEGIN
          SELECT RAISE(ABORT, 'reconcile command payloads are immutable');
        END
    """,
    "reconcile_commands_no_delete": """
        CREATE TRIGGER reconcile_commands_no_delete
        BEFORE DELETE ON reconcile_commands BEGIN
          SELECT RAISE(ABORT, 'reconcile commands are immutable');
        END
    """,
    "run_events_no_update": """
        CREATE TRIGGER run_events_no_update
        BEFORE UPDATE ON run_events BEGIN
          SELECT RAISE(ABORT, 'run events are immutable');
        END
    """,
    "run_events_no_delete": """
        CREATE TRIGGER run_events_no_delete
        BEFORE DELETE ON run_events BEGIN
          SELECT RAISE(ABORT, 'run events are immutable');
        END
    """,
    "agent_attempts_state_transition": """
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
    """,
    "agent_attempts_no_delete": """
        CREATE TRIGGER agent_attempts_no_delete
        BEFORE DELETE ON agent_attempts BEGIN
          SELECT RAISE(ABORT, 'agent attempts are immutable');
        END
    """,
    "wait_answers_payload_no_update": """
        CREATE TRIGGER wait_answers_payload_no_update
        BEFORE UPDATE OF run_id, revision_hash, node_id, node_execution_id,
                         answer_bytes, answer_hash, answer_workflow_id
        ON wait_answers BEGIN
          SELECT RAISE(ABORT, 'wait answer bindings are immutable');
        END
    """,
    "wait_answers_state_transition": """
        CREATE TRIGGER wait_answers_state_transition
        BEFORE UPDATE OF state, state_version ON wait_answers
        WHEN NOT (OLD.state = 'PENDING' AND OLD.state_version = 0
                  AND NEW.state = 'APPLIED' AND NEW.state_version = 1)
        BEGIN
          SELECT RAISE(ABORT, 'invalid wait answer transition');
        END
    """,
    "wait_answers_no_delete": """
        CREATE TRIGGER wait_answers_no_delete
        BEFORE DELETE ON wait_answers BEGIN
          SELECT RAISE(ABORT, 'wait answers are immutable');
        END
    """,
    "published_revisions_no_update": """
        CREATE TRIGGER published_revisions_no_update
        BEFORE UPDATE ON published_revisions BEGIN
          SELECT RAISE(ABORT, 'published revisions are immutable');
        END
    """,
    "published_revisions_no_delete": """
        CREATE TRIGGER published_revisions_no_delete
        BEFORE DELETE ON published_revisions BEGIN
          SELECT RAISE(ABORT, 'published revisions are immutable');
        END
    """,
    "catalog_lineages_no_update": """
        CREATE TRIGGER catalog_lineages_no_update
        BEFORE UPDATE ON catalog_lineages BEGIN
          SELECT RAISE(ABORT, 'catalog lineages are immutable');
        END
    """,
    "catalog_lineages_no_delete": """
        CREATE TRIGGER catalog_lineages_no_delete
        BEFORE DELETE ON catalog_lineages BEGIN
          SELECT RAISE(ABORT, 'catalog lineages are immutable');
        END
    """,
    "catalog_lineage_members_must_be_published": """
        CREATE TRIGGER catalog_lineage_members_must_be_published
        BEFORE INSERT ON catalog_lineage_members
        WHEN NOT EXISTS (
          SELECT 1
          FROM catalog_lineages AS lineage
          JOIN published_revisions AS revision
            ON revision.kind = lineage.kind
           AND revision.revision_hash = NEW.revision_hash
          WHERE lineage.lineage_id = NEW.lineage_id
        )
        BEGIN
          SELECT RAISE(
            ABORT,
            'catalog lineage members must name a published revision of the lineage kind'
          );
        END
    """,
    "catalog_lineage_members_no_update": """
        CREATE TRIGGER catalog_lineage_members_no_update
        BEFORE UPDATE ON catalog_lineage_members BEGIN
          SELECT RAISE(ABORT, 'catalog lineage members are immutable');
        END
    """,
    "catalog_lineage_members_no_delete": """
        CREATE TRIGGER catalog_lineage_members_no_delete
        BEFORE DELETE ON catalog_lineage_members BEGIN
          SELECT RAISE(ABORT, 'catalog lineage members are immutable');
        END
    """,
    "node_receipts_v3_no_update": """
        CREATE TRIGGER node_receipts_v3_no_update
        BEFORE UPDATE ON node_receipts_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node receipts are immutable');
        END
    """,
    "node_receipts_v3_no_delete": """
        CREATE TRIGGER node_receipts_v3_no_delete
        BEFORE DELETE ON node_receipts_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node receipts are immutable');
        END
    """,
}


class UnsupportedSchemaVersion(RuntimeError):
    def __init__(self, actual: object) -> None:
        super().__init__(
            f"Atelier schema version {actual!r} is unsupported; expected {SCHEMA_VERSION}"
        )


class MigrationRequired(UnsupportedSchemaVersion):
    def __init__(self, actual: int = 2) -> None:
        RuntimeError.__init__(
            self,
            f"Atelier schema version {actual} requires an explicit offline migration; "
            "runtime startup will not alter it",
        )


def _require_supported_versions(versions: Sequence[int]) -> int:
    normalized = tuple(versions)
    if len(normalized) == 1 and normalized[0] in _OFFLINE_CUTOVER_VERSIONS:
        raise MigrationRequired(normalized[0])
    if len(normalized) != 1 or normalized[0] != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(normalized)
    return normalized[0]


@dataclass(frozen=True)
class _TableSchemaFingerprint:
    name: str
    create_sql: str
    columns: tuple[tuple[object, ...], ...]
    indexes: tuple[tuple[object, ...], ...]
    foreign_keys: tuple[tuple[object, ...], ...]


@dataclass(frozen=True)
class _ProductSchemaFingerprint:
    tables: tuple[_TableSchemaFingerprint, ...]
    triggers: tuple[tuple[str, str, str], ...]


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalized_sql(value: object) -> str:
    if value is None:
        return ""
    source = str(value)
    normalized: list[str] = []
    pending_space = False
    closing_quote: str | None = None
    index = 0
    while index < len(source):
        character = source[index]
        if closing_quote is not None:
            normalized.append(character)
            if character == closing_quote:
                if index + 1 < len(source) and source[index + 1] == closing_quote:
                    normalized.append(source[index + 1])
                    index += 2
                    continue
                closing_quote = None
            index += 1
            continue
        if character.isspace():
            pending_space = bool(normalized)
            index += 1
            continue
        if pending_space:
            normalized.append(" ")
            pending_space = False
        normalized.append(character)
        if character in {"'", '"', "`"}:
            closing_quote = character
        elif character == "[":
            closing_quote = "]"
        index += 1
    return "".join(normalized)


def _table_fingerprint(
    connection: sqlite3.Connection, table_name: str
) -> _TableSchemaFingerprint:
    create_record = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    if create_record is None or create_record[0] is None:
        raise UnsupportedSchemaVersion(
            f"malformed v{SCHEMA_VERSION} product table {table_name}"
        )
    quoted_table = _quoted_identifier(table_name)
    columns = tuple(
        tuple(record)
        for record in connection.execute(f"PRAGMA table_xinfo({quoted_table})")
    )
    indexes: list[tuple[object, ...]] = []
    for record in connection.execute(f"PRAGMA index_list({quoted_table})"):
        index_name = str(record[1])
        quoted_index = _quoted_identifier(index_name)
        index_sql_record = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        index_columns = tuple(
            tuple(column)
            for column in connection.execute(f"PRAGMA index_xinfo({quoted_index})")
        )
        indexes.append(
            (
                index_name,
                int(record[2]),
                str(record[3]),
                int(record[4]),
                _normalized_sql(
                    None if index_sql_record is None else index_sql_record[0]
                ),
                index_columns,
            )
        )
    foreign_keys = tuple(
        tuple(record)
        for record in connection.execute(f"PRAGMA foreign_key_list({quoted_table})")
    )
    return _TableSchemaFingerprint(
        table_name,
        _normalized_sql(create_record[0]),
        columns,
        tuple(sorted(indexes, key=lambda value: str(value[0]))),
        foreign_keys,
    )


def _product_schema_fingerprint(
    connection: sqlite3.Connection,
) -> _ProductSchemaFingerprint:
    tables = tuple(
        _table_fingerprint(connection, table_name)
        for table_name in sorted(PRODUCT_TABLE_NAMES)
    )
    placeholders = ",".join("?" for _ in PRODUCT_TABLE_NAMES)
    triggers = tuple(
        (str(record[0]), str(record[1]), _normalized_sql(record[2]))
        for record in connection.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master "
            f"WHERE type='trigger' AND tbl_name IN ({placeholders}) ORDER BY name",
            tuple(sorted(PRODUCT_TABLE_NAMES)),
        )
    )
    return _ProductSchemaFingerprint(tables, triggers)


def _sqlite_connection(connection: sa.Connection) -> sqlite3.Connection:
    raw_connection = connection.connection.driver_connection
    if not isinstance(raw_connection, sqlite3.Connection):
        raise UnsupportedSchemaVersion(f"Atelier v{SCHEMA_VERSION} requires SQLite")
    return raw_connection


def _product_schema_fingerprint_sha256(
    fingerprint: _ProductSchemaFingerprint,
) -> str:
    encoded = json.dumps(
        asdict(fingerprint),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _require_product_shape(connection: sqlite3.Connection, version: int) -> None:
    try:
        observed = _product_schema_fingerprint(connection)
    except UnsupportedSchemaVersion as error:
        raise UnsupportedSchemaVersion(
            f"malformed v{version} product schema fingerprint"
        ) from error
    if (
        _product_schema_fingerprint_sha256(observed)
        != (_PRODUCT_SCHEMA_FINGERPRINT_SHA256[version])
    ):
        raise UnsupportedSchemaVersion(
            f"malformed v{version} product schema fingerprint"
        )


def _preflight_existing_schema(engine: Engine) -> int | None:
    raw_database_path = engine.url.database
    if engine.url.get_backend_name() != "sqlite" or raw_database_path is None:
        return None
    if raw_database_path in {"", ":memory:"}:
        return None
    database_path = Path(raw_database_path).resolve()
    if not database_path.is_file() or database_path.stat().st_size == 0:
        return None
    try:
        with sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro", uri=True
        ) as connection:
            table_names = {
                str(record[0])
                for record in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not table_names:
                return None
            if atelier_schema_versions.name not in table_names:
                raise UnsupportedSchemaVersion(
                    f"missing version owner beside tables {tuple(sorted(table_names))!r}"
                )
            versions: list[int] = []
            for record in connection.execute(
                "SELECT version FROM atelier_schema_versions"
            ):
                version = record[0]
                if not isinstance(version, int):
                    raise TypeError("schema version must be stored as an integer")
                versions.append(version)
            version = _require_supported_versions(versions)
            _require_product_shape(connection, version)
            return version
    except UnsupportedSchemaVersion:
        raise
    except (sqlite3.DatabaseError, TypeError, ValueError) as error:
        raise UnsupportedSchemaVersion("unreadable schema version owner") from error


def _create_triggers(connection: sa.Connection, statements: Iterable[str]) -> None:
    for statement in statements:
        connection.execute(sa.text(statement))


def _schema_version_from_connection(connection: sa.Connection) -> int | None:
    inspector = sa.inspect(connection)
    if not inspector.has_table(atelier_schema_versions.name):
        return None
    versions = connection.execute(
        sa.select(atelier_schema_versions.c.version)
    ).scalars()
    normalized: list[int] = []
    for version in versions:
        if not isinstance(version, int):
            raise UnsupportedSchemaVersion("schema version must be an integer")
        normalized.append(version)
    return _require_supported_versions(normalized)


def initialize_schema(engine: Engine) -> None:
    if engine.url.get_backend_name() != "sqlite":
        raise UnsupportedSchemaVersion(f"Atelier v{SCHEMA_VERSION} requires SQLite")
    _preflight_existing_schema(engine)
    with engine.connect() as connection:
        _schema_version_from_connection(connection)
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            inspector = sa.inspect(connection)
            if not inspector.has_table(atelier_schema_versions.name):
                existing_tables = set(inspector.get_table_names())
                if existing_tables:
                    raise UnsupportedSchemaVersion(
                        "missing version owner beside tables "
                        f"{tuple(sorted(existing_tables))!r}"
                    )
                metadata.create_all(connection)
                connection.execute(
                    atelier_schema_versions.insert().values(version=SCHEMA_VERSION)
                )
                _create_triggers(connection, _PRODUCT_TRIGGERS.values())

            locked_version = _schema_version_from_connection(connection)
            if locked_version != SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(locked_version)
            _require_product_shape(_sqlite_connection(connection), SCHEMA_VERSION)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
