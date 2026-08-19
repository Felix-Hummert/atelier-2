from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateIndex, CreateTable

from atelier2.adapters.dbos.published_schema_shapes import PUBLISHED_TABLE_SHAPES
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_PROVIDER_ID_CHARACTERS,
    MAXIMUM_SIGNED_INT64,
)
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.contracts.catalog_v3 import MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL
from atelier2.contracts.workflow_formats import WorkflowFormatVersion


def _rfc3339_utc(column: str) -> str:
    """RFC 3339 UTC at second precision."""

    return f"(length({column}) = 20 AND {column} LIKE '____-__-__T__:__:__Z')"


def _rfc3339_utc_or_null(column: str) -> str:
    """A recording instant is absent, or RFC 3339 UTC at second precision."""

    return f"({column} IS NULL OR {_rfc3339_utc(column)})"


@dataclass(frozen=True)
class ProductSchemaHandoff:
    version: int
    fingerprint_sha256: str


# Movable hop: this head admits AGENT_REFUSED. Predecessor is today's
# published version (#355 landed as 22). Change only this constant to restack.
_HOP_PREDECESSOR_VERSION = 22
SCHEMA_VERSION = _HOP_PREDECESSOR_VERSION + 1
_VERSION_NINE = 9
_VERSION_TEN = 10
_VERSION_ELEVEN = 11
_VERSION_TWELVE = 12
_VERSION_THIRTEEN = 13
_VERSION_FOURTEEN = 14
_VERSION_FIFTEEN = 15
_VERSION_SIXTEEN = 16
_VERSION_SEVENTEEN = 17
_VERSION_EIGHTEEN = 18
_VERSION_NINETEEN = 19
_VERSION_TWENTY = 20
_VERSION_TWENTY_ONE = 21
_VERSION_TWENTY_TWO = 22
# Operator ruling 5307892458: no store compatibility until a named maturity.
# Every published prototype schema remains a predecessor; runtime never migrates it.
_OFFLINE_CUTOVER_VERSIONS = frozenset(range(1, SCHEMA_VERSION))
# V9 product tables equal V8. V10 adds the thin catalog/receipt foundation. V11
# closes the artifact/output/access store shape that Cut B writes atomically.
# V12 adds append-only catalog alias and retirement histories. V13 gives the
# context-package manifest, the node-execution-request preimage and the run
# configuration snapshot durable, immutable homes, and records the run
# configuration revision a supervised V3 run was started under. V14 gives the
# order a run was started with a durable, immutable home, so one published
# revision serves every order instead of one revision per distinct input. V15
# adds the immutable evidence of one redeemed tool grant: which command the
# attempt ran, how it ended, and what it wrote. V16 gives an agent completion a
# home for the receipt hash its event preimage now binds, so a recomputed
# terminal hash proves under which binding the attempt ran. V17 admits
# OUTPUT_SCHEMA_REFUSED as a second attempt failure code, so a schema-refused
# output ends its attempt under its own name instead of borrowing the process
# exit's or killing the driver. V18 admits FAILED as a run state, so a line
# whose open node paths have terminally failed ends under the node's own
# reason instead of standing STARTED with nothing to continue it. V19 gives
# content-addressed material a durable, immutable home, so an order larger than
# the inline bound travels as the address of bytes published once instead of not
# travelling at all. V20 gives the round a declared loop is turning a durable
# home on the run and on every event and agent receipt it writes, keys a node
# execution request by the execution rather than by the request it repeats, and
# drops the receipt key that said one agent receipt per node per run -- a
# sentence that stopped being true when a node could run twice. V21 admits
# headless_with_tools as a requested capability, so a configuration may durably
# ask for an executor whose invocation carries the provider's own tools. V22
# records when a run, attempt, or event was written, as RFC 3339 UTC.
# Predecessor rows stay NULL — no invented time. V23 admits AGENT_REFUSED as a
# third attempt failure code. The hop number is movable:
# `_HOP_PREDECESSOR_VERSION` is the one constant to restack.
_PRODUCT_SCHEMA_FINGERPRINT_SHA256 = {
    7: "0bf32217a1254ee64d84c4ed629244600d542211ac655e4405a0df51f857081b",
    8: "6ba76214cb567ffcdab46e5a3ae00fc10824b962f16a8036ce90590be0b79b38",
    9: "6ba76214cb567ffcdab46e5a3ae00fc10824b962f16a8036ce90590be0b79b38",
    10: "4a7bbd9bf07880868aa2f7ddae3e7262eb270f711d4fdc420f902457817bfff7",
    11: "18dead2ab36c15bf61fa1b1bb5fed3b5a1075dc773d83d8b57c00c05c84178ef",
    12: "feef25b171e305bb9a3a9637cc4d0fb1c8dec4a4a7a9813e060ccf12598a5cc7",
    13: "5782fdc1331c52f3f04097f6a2a6d416ab528d6ee8a6546a7d6435ae9d11c175",
    14: "6cf56491322e716fce9be2310584ed2b92533961b8fda341bfcc317182432f0a",
    15: "375e81d1c8967053951d1be0cab19cee274e35272f364feae15ec3413eb3c9b9",
    16: "97605fb330cb6382d52a554d644015f631cccea3759c04c27de3ca5f1fea9c3a",
    17: "2f3a11d0b4d67e375259ca732c7243c95d19fa763e03785b0bd4a83c1b1359d2",
    18: "c60275544c9984adccff79e3a4f5ab6eeab5ea1683306adf1d2faa7dbb51e29d",
    19: "a861d9087da05c112f88ae8ec573f57338b5ef1d04f36553922c505127b34298",
    20: "09752981999444ee4129cfe29b7322b79d2ff378f91d1af5050342eff78b8637",
    21: "6c4705f2960d1669a596ae8f3c857dd0ac15c4c94b71b4bb5998d1bac672cefe",
    22: "72aa8f76942197b704f07c156adbb1e46c3b069ce16a53c6d95a067827966387",
    23: "6d8a3af85ecc40781c6eea454e33ae625de1cf6d8726ca5c502cdcc33eb2c124",
}
V9_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_NINE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_NINE],
)
V10_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TEN],
)
V11_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_ELEVEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_ELEVEN],
)
V12_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWELVE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWELVE],
)
V13_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTEEN],
)
V14_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FOURTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FOURTEEN],
)
V15_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FIFTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FIFTEEN],
)
V16_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_SIXTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_SIXTEEN],
)
V17_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_SEVENTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_SEVENTEEN],
)
V18_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_EIGHTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_EIGHTEEN],
)
V19_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_NINETEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_NINETEEN],
)
V20_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY],
)
V21_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY_ONE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY_ONE],
)
V22_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY_TWO,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY_TWO],
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
    sa.Column("current_round_ordinal", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_version", sa.Integer, nullable=False),
    sa.Column("last_event_sequence", sa.Integer, nullable=False),
    sa.Column("terminal_hash", sa.Text, nullable=True),
    sa.Column(
        "run_configuration_revision_hash",
        sa.Text,
        sa.ForeignKey("run_configuration_revisions.revision_hash"),
        nullable=True,
    ),
    sa.UniqueConstraint("run_id", "revision_hash"),
    sa.UniqueConstraint("run_id", "revision_hash", "agent_binding_set_hash"),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint("length(current_node_id) > 0"),
    sa.CheckConstraint(f"current_round_ordinal >= {FIRST_ROUND_ORDINAL}"),
    sa.CheckConstraint(
        "workflow_format_version IN ("
        + ", ".join(str(int(member)) for member in WorkflowFormatVersion)
        + ")"
    ),
    sa.CheckConstraint(
        f"(workflow_format_version = {int(WorkflowFormatVersion.V1)} AND "
        "agent_binding_set_hash IS NULL) OR "
        f"(workflow_format_version = {int(WorkflowFormatVersion.V2)} AND "
        "agent_binding_set_hash IS NOT NULL "
        "AND length(agent_binding_set_hash) = 64 "
        "AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*') OR "
        f"(workflow_format_version = {int(WorkflowFormatVersion.V3)} AND "
        "(agent_binding_set_hash IS NULL OR "
        "(length(agent_binding_set_hash) = 64 "
        "AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*')))"
    ),
    sa.CheckConstraint(
        "state IN ('STARTED', 'WAITING_RECONCILIATION', 'WAITING_INPUT', "
        "'COMPLETED', 'FAILED')"
    ),
    sa.CheckConstraint("state_version >= 0"),
    sa.CheckConstraint("last_event_sequence >= 0"),
    sa.CheckConstraint(
        "(state IN ('COMPLETED', 'FAILED') AND terminal_hash IS NOT NULL "
        "AND length(terminal_hash) = 64 AND terminal_hash NOT GLOB '*[^0-9a-f]*') "
        "OR (state NOT IN ('COMPLETED', 'FAILED') AND terminal_hash IS NULL)"
    ),
    sa.CheckConstraint(
        f"(workflow_format_version = {int(WorkflowFormatVersion.V3)} "
        "AND run_configuration_revision_hash IS NOT NULL "
        "AND length(run_configuration_revision_hash) = 64 "
        "AND run_configuration_revision_hash NOT GLOB '*[^0-9a-f]*') "
        f"OR (workflow_format_version <> {int(WorkflowFormatVersion.V3)} "
        "AND run_configuration_revision_hash IS NULL)"
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
    sa.CheckConstraint(
        f"length(profile_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(f"revision_number BETWEEN 1 AND {MAXIMUM_SIGNED_INT64}"),
    sa.CheckConstraint(
        f"length(provider_id) BETWEEN 1 AND {MAXIMUM_PROVIDER_ID_CHARACTERS}"
    ),
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
    sa.CheckConstraint(f"length(model) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"),
    sa.CheckConstraint(
        "length(auth_profile_revision_hash) = 64 "
        "AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        f"length(executor_revision) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint("revision_format_version IN (1, 2)"),
    sa.CheckConstraint(
        "requested_capability IN ('headless', 'headless_with_tools', 'interactive')"
    ),
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
    sa.CheckConstraint(f"length(role) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"),
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
    sa.Column("round_ordinal", sa.Integer, nullable=False),
    # One receipt per node *execution* -- the primary key above says it, and it
    # says it exactly. A second key over (run, revision, node) said the same
    # thing while a node ran once per run, and said something false the moment a
    # declared loop ran it again.
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
    sa.CheckConstraint(
        f"length(node_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(f"length(role) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"),
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
    sa.CheckConstraint(
        f"length(profile_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(f"revision_number BETWEEN 1 AND {MAXIMUM_SIGNED_INT64}"),
    sa.CheckConstraint(
        f"length(provider_id) BETWEEN 1 AND {MAXIMUM_PROVIDER_ID_CHARACTERS}"
    ),
    sa.CheckConstraint("provider_id GLOB '[a-z]*'"),
    sa.CheckConstraint("provider_id NOT GLOB '*[^a-z0-9._-]*'"),
    sa.CheckConstraint("auth_mode IN ('subscription', 'api_key')"),
    sa.CheckConstraint(f"length(model) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"),
    sa.CheckConstraint(
        f"length(executor_revision) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(
        f"length(executor_operational_identity) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(
        "typeof(output_bytes) = 'blob' AND length(output_bytes) <= 49152"
    ),
    sa.CheckConstraint(
        "length(output_hash) = 64 AND output_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(f"round_ordinal >= {FIRST_ROUND_ORDINAL}"),
)
tool_redemptions = sa.Table(
    "tool_redemptions",
    metadata,
    sa.Column(
        "node_execution_id",
        sa.Text,
        sa.ForeignKey("agent_receipts_v2.node_execution_id"),
        primary_key=True,
    ),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("workflow_revision_hash", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column(
        "attempt_id",
        sa.Text,
        sa.ForeignKey("agent_attempts.attempt_id"),
        nullable=False,
    ),
    sa.Column("tool_revision_hash", sa.Text, nullable=False),
    sa.Column("capability", sa.Text, nullable=False),
    sa.Column("command", sa.Text, nullable=False),
    sa.Column("exit_code", sa.Integer, nullable=False),
    sa.Column("standard_output_hash", sa.Text, nullable=False),
    sa.Column("receipt_hash", sa.Text, nullable=False, unique=True),
    sa.ForeignKeyConstraint(
        ("run_id", "workflow_revision_hash"),
        ("runs.run_id", "runs.revision_hash"),
    ),
    sa.CheckConstraint(
        "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint(
        "length(workflow_revision_hash) = 64 "
        "AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        f"length(node_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint("length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint(
        "length(tool_revision_hash) = 64 AND tool_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("capability IN ('run-project-verification')"),
    # The exact argv, as the adapter writes one immutable value. Its length is
    # the record's own bound, not a second one spelled here: what a store may
    # hold and what a receipt may carry would be two numbers for one limit.
    sa.CheckConstraint("length(command) > 0"),
    sa.CheckConstraint(
        f"exit_code BETWEEN {-MAXIMUM_SIGNED_INT64 - 1} AND {MAXIMUM_SIGNED_INT64}"
    ),
    sa.CheckConstraint(
        "length(standard_output_hash) = 64 "
        "AND standard_output_hash NOT GLOB '*[^0-9a-f]*'"
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
    sa.CheckConstraint(
        f"length(executor_operational_identity) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint(
        "length(workflow_revision_hash) = 64 "
        "AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        f"length(node_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
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
        f"(process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS} "
        f"AND length(watchdog_generation_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS})"
    ),
    sa.CheckConstraint(
        "(cancellation_command_id IS NULL "
        "AND cancellation_expected_state_version IS NULL "
        "AND replacement IS NULL AND redrive_state IS NULL "
        "AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) "
        f"OR (length(cancellation_command_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS} "
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
        "AND failure_code IN "
        "('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED', "
        "'AGENT_REFUSED') "
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
    sa.Column("round_ordinal", sa.Integer, nullable=False),
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
    sa.Column("agent_receipt_hash", sa.Text, nullable=True),
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
    sa.CheckConstraint(f"round_ordinal >= {FIRST_ROUND_ORDINAL}"),
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
        f"AND length(cancellation_command_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS} "
        "AND replacement IN ('NONE', 'ONE') "
        "AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) "
        "OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') "
        f"AND length(cancellation_command_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS} "
        "AND replacement IN ('NONE', 'ONE') "
        "AND cancellation_disposition IS NOT NULL)))"
    ),
    # The mirror of the contract's admission rule (contracts/executions.py):
    # only a completion has an agent receipt, and it stays nullable because a
    # run written before v3 of the event hash carries none.
    sa.CheckConstraint(
        "(event_kind = 'AGENT_COMPLETED' AND (agent_receipt_hash IS NULL "
        "OR (length(agent_receipt_hash) = 64 "
        "AND agent_receipt_hash NOT GLOB '*[^0-9a-f]*'))) "
        "OR (event_kind <> 'AGENT_COMPLETED' AND agent_receipt_hash IS NULL)"
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
run_instants = sa.Table(
    "run_instants",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("started_at", sa.Text, nullable=False),
    sa.Column("ended_at", sa.Text, nullable=True),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint(_rfc3339_utc("started_at")),
    sa.CheckConstraint(_rfc3339_utc_or_null("ended_at")),
)
attempt_instants = sa.Table(
    "attempt_instants",
    metadata,
    sa.Column("attempt_id", sa.Text, primary_key=True),
    sa.Column("started_at", sa.Text, nullable=False),
    sa.Column("ended_at", sa.Text, nullable=True),
    sa.CheckConstraint("length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint(_rfc3339_utc("started_at")),
    sa.CheckConstraint(_rfc3339_utc_or_null("ended_at")),
)
event_instants = sa.Table(
    "event_instants",
    metadata,
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("event_sequence", sa.Integer, nullable=False),
    sa.Column("recorded_at", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("run_id", "event_sequence"),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint("event_sequence > 0"),
    sa.CheckConstraint(_rfc3339_utc("recorded_at")),
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
catalog_lineage_aliases = sa.Table(
    "catalog_lineage_aliases",
    metadata,
    sa.Column(
        "lineage_id",
        sa.Text,
        sa.ForeignKey("catalog_lineages.lineage_id"),
        nullable=False,
    ),
    sa.Column("activation_number", sa.Integer, nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("activated_at", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("lineage_id", "activation_number"),
    sa.CheckConstraint("activation_number >= 1"),
    sa.CheckConstraint(
        f"length(name) BETWEEN 1 AND {MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS}"
    ),
    sa.CheckConstraint("name GLOB '[a-z]*' AND name NOT GLOB '*[^a-z0-9._-]*'"),
    sa.CheckConstraint("length(name) <> 64 OR name GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint("length(actor) > 0"),
    sa.CheckConstraint("length(activated_at) > 0"),
)
catalog_lineage_retirements = sa.Table(
    "catalog_lineage_retirements",
    metadata,
    sa.Column(
        "lineage_id",
        sa.Text,
        sa.ForeignKey("catalog_lineages.lineage_id"),
        nullable=False,
    ),
    sa.Column("activation_number", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("activated_at", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("lineage_id", "activation_number"),
    sa.CheckConstraint("activation_number >= 1"),
    sa.CheckConstraint("state IN ('retired')"),
    sa.CheckConstraint("length(actor) > 0"),
    sa.CheckConstraint("length(activated_at) > 0"),
)
node_artifacts_v3 = sa.Table(
    "node_artifacts_v3",
    metadata,
    sa.Column(
        "run_id",
        sa.Text,
        sa.ForeignKey("runs.run_id"),
        nullable=False,
    ),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("node_execution_id", sa.Text, nullable=False),
    sa.Column("output_name", sa.Text, nullable=False),
    sa.Column("schema_revision_hash", sa.Text, nullable=False),
    sa.Column("value", sa.LargeBinary, nullable=False),
    sa.Column("value_hash", sa.Text, nullable=False),
    sa.Column("artifact_hash", sa.Text, unique=True, nullable=False),
    sa.PrimaryKeyConstraint("run_id", "node_id", "node_execution_id", "output_name"),
    sa.UniqueConstraint(
        "node_execution_id",
        "output_name",
        "schema_revision_hash",
        "value_hash",
    ),
    sa.CheckConstraint("length(node_id) > 0"),
    sa.CheckConstraint("length(output_name) > 0"),
    sa.CheckConstraint(
        "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(schema_revision_hash) = 64 "
        "AND schema_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(value_hash) = 64 AND value_hash NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint(
        "length(artifact_hash) = 64 AND artifact_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
node_receipts_v3 = sa.Table(
    "node_receipts_v3",
    metadata,
    sa.Column("node_execution_id", sa.Text, primary_key=True),
    sa.Column("disposition", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column(
        "context_package_hash",
        sa.Text,
        sa.ForeignKey("context_packages_v3.package_hash"),
        nullable=False,
    ),
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
    # The pair is the binding. Each hash alone can name a record that exists
    # while the two together describe a node execution nobody ran -- this
    # execution's receipt pointing at another execution's request -- so the key
    # is composite and a single-column one would not see it.
    sa.ForeignKeyConstraint(
        ("node_execution_id", "request_hash"),
        (
            "node_execution_requests_v3.node_execution_id",
            "node_execution_requests_v3.request_hash",
        ),
    ),
)
artifacts = sa.Table(
    "artifacts",
    metadata,
    sa.Column("artifact_hash", sa.Text, primary_key=True),
    sa.Column("content", sa.LargeBinary, nullable=False),
    sa.CheckConstraint(
        "length(artifact_hash) = 64 AND artifact_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        f"length(content) BETWEEN 1 AND {MAXIMUM_ARTIFACT_BYTES}",
    ),
)
run_inputs_v3 = sa.Table(
    "run_inputs_v3",
    metadata,
    sa.Column("run_id", sa.Text, sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("schema_revision_hash", sa.Text, nullable=False),
    sa.Column("value", sa.LargeBinary, nullable=False),
    sa.Column("value_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("run_id", "name"),
    sa.CheckConstraint("length(name) > 0"),
    sa.CheckConstraint(
        "length(schema_revision_hash) = 64 "
        "AND schema_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(value_hash) = 64 AND value_hash NOT GLOB '*[^0-9a-f]*'"),
)
run_configuration_revisions = sa.Table(
    "run_configuration_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("preimage", sa.LargeBinary, nullable=False),
    sa.CheckConstraint(
        "length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
node_execution_requests_v3 = sa.Table(
    "node_execution_requests_v3",
    metadata,
    # The execution is the key, not the request. Two rounds of one looped node
    # are asked the same thing until a result differs between them, so their
    # request preimages are identical and their hashes are one value -- while
    # the executions are two, and each owes its own receipt. Keying by the hash
    # made the second round's row vanish into the first and left its receipt
    # with nothing to bind.
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("node_execution_id", sa.Text, primary_key=True),
    sa.Column(
        "run_configuration_revision_hash",
        sa.Text,
        sa.ForeignKey("run_configuration_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("context_package_hash", sa.Text, nullable=False),
    sa.Column("preimage", sa.LargeBinary, nullable=False),
    sa.UniqueConstraint("node_execution_id", "request_hash"),
    sa.CheckConstraint(
        "length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(context_package_hash) = 64 "
        "AND context_package_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.ForeignKeyConstraint(
        ("context_package_hash",), ("context_packages_v3.package_hash",)
    ),
)
context_packages_v3 = sa.Table(
    "context_packages_v3",
    metadata,
    sa.Column("package_hash", sa.Text, primary_key=True),
    sa.Column("manifest", sa.LargeBinary, nullable=False),
    sa.CheckConstraint(
        "length(package_hash) = 64 AND package_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
node_receipt_outputs_v3 = sa.Table(
    "node_receipt_outputs_v3",
    metadata,
    sa.Column(
        "node_execution_id",
        sa.Text,
        sa.ForeignKey("node_receipts_v3.node_execution_id"),
        nullable=False,
    ),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("output_name", sa.Text, nullable=False),
    sa.Column("schema_revision_hash", sa.Text, nullable=False),
    sa.Column("value_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("node_execution_id", "position"),
    sa.UniqueConstraint("node_execution_id", "output_name"),
    sa.ForeignKeyConstraint(
        (
            "node_execution_id",
            "output_name",
            "schema_revision_hash",
            "value_hash",
        ),
        (
            "node_artifacts_v3.node_execution_id",
            "node_artifacts_v3.output_name",
            "node_artifacts_v3.schema_revision_hash",
            "node_artifacts_v3.value_hash",
        ),
    ),
    sa.CheckConstraint("position >= 0"),
    sa.CheckConstraint("length(output_name) > 0"),
    sa.CheckConstraint(
        "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(schema_revision_hash) = 64 "
        "AND schema_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(value_hash) = 64 AND value_hash NOT GLOB '*[^0-9a-f]*'"),
)
node_receipt_access_v3 = sa.Table(
    "node_receipt_access_v3",
    metadata,
    sa.Column(
        "node_execution_id",
        sa.Text,
        sa.ForeignKey("node_receipts_v3.node_execution_id"),
        nullable=False,
    ),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("access_receipt_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("node_execution_id", "position"),
    sa.CheckConstraint("position >= 0"),
    sa.CheckConstraint(
        "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(access_receipt_hash) = 64 "
        "AND access_receipt_hash NOT GLOB '*[^0-9a-f]*'"
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
                         workflow_format_version, agent_binding_set_hash,
                         run_configuration_revision_hash
        ON runs BEGIN
          SELECT RAISE(ABORT, 'run bindings are immutable');
        END
    """,
    "artifacts_no_update": """
        CREATE TRIGGER artifacts_no_update
        BEFORE UPDATE ON artifacts BEGIN
          SELECT RAISE(ABORT, 'artifacts are immutable');
        END
    """,
    "artifacts_no_delete": """
        CREATE TRIGGER artifacts_no_delete
        BEFORE DELETE ON artifacts BEGIN
          SELECT RAISE(ABORT, 'artifacts are immutable');
        END
    """,
    "run_inputs_v3_no_update": """
        CREATE TRIGGER run_inputs_v3_no_update
        BEFORE UPDATE ON run_inputs_v3 BEGIN
          SELECT RAISE(ABORT, 'run inputs are immutable');
        END
    """,
    "run_inputs_v3_no_delete": """
        CREATE TRIGGER run_inputs_v3_no_delete
        BEFORE DELETE ON run_inputs_v3 BEGIN
          SELECT RAISE(ABORT, 'run inputs are immutable');
        END
    """,
    "run_configuration_revisions_no_update": """
        CREATE TRIGGER run_configuration_revisions_no_update
        BEFORE UPDATE ON run_configuration_revisions BEGIN
          SELECT RAISE(ABORT, 'run configuration revisions are immutable');
        END
    """,
    "run_configuration_revisions_no_delete": """
        CREATE TRIGGER run_configuration_revisions_no_delete
        BEFORE DELETE ON run_configuration_revisions BEGIN
          SELECT RAISE(ABORT, 'run configuration revisions are immutable');
        END
    """,
    "node_execution_requests_v3_no_update": """
        CREATE TRIGGER node_execution_requests_v3_no_update
        BEFORE UPDATE ON node_execution_requests_v3 BEGIN
          SELECT RAISE(ABORT, 'node execution requests are immutable');
        END
    """,
    "node_execution_requests_v3_no_delete": """
        CREATE TRIGGER node_execution_requests_v3_no_delete
        BEFORE DELETE ON node_execution_requests_v3 BEGIN
          SELECT RAISE(ABORT, 'node execution requests are immutable');
        END
    """,
    "context_packages_v3_no_update": """
        CREATE TRIGGER context_packages_v3_no_update
        BEFORE UPDATE ON context_packages_v3 BEGIN
          SELECT RAISE(ABORT, 'context packages are immutable');
        END
    """,
    "context_packages_v3_no_delete": """
        CREATE TRIGGER context_packages_v3_no_delete
        BEFORE DELETE ON context_packages_v3 BEGIN
          SELECT RAISE(ABORT, 'context packages are immutable');
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
    "tool_redemptions_no_update": """
        CREATE TRIGGER tool_redemptions_no_update
        BEFORE UPDATE ON tool_redemptions BEGIN
          SELECT RAISE(ABORT, 'tool redemptions are immutable');
        END
    """,
    "tool_redemptions_no_delete": """
        CREATE TRIGGER tool_redemptions_no_delete
        BEFORE DELETE ON tool_redemptions BEGIN
          SELECT RAISE(ABORT, 'tool redemptions are immutable');
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
             AND NEW.failure_code IN
               ('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED',
                'AGENT_REFUSED')
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
    "catalog_lineage_members_unique_per_kind": """
        CREATE TRIGGER catalog_lineage_members_unique_per_kind
        BEFORE INSERT ON catalog_lineage_members
        WHEN EXISTS (
          SELECT 1
          FROM catalog_lineage_members AS existing
          JOIN catalog_lineages AS existing_lineage
            ON existing_lineage.lineage_id = existing.lineage_id
          JOIN catalog_lineages AS new_lineage
            ON new_lineage.lineage_id = NEW.lineage_id
          WHERE existing.revision_hash = NEW.revision_hash
            AND existing_lineage.kind = new_lineage.kind
            AND existing.lineage_id <> NEW.lineage_id
        )
        BEGIN
          SELECT RAISE(
            ABORT,
            'catalog lineage members of one kind cannot share a revision'
          );
        END
    """,
    "catalog_lineage_aliases_name_unique_per_kind": """
        CREATE TRIGGER catalog_lineage_aliases_name_unique_per_kind
        BEFORE INSERT ON catalog_lineage_aliases
        WHEN EXISTS (
          SELECT 1
          FROM catalog_lineage_aliases AS existing
          JOIN catalog_lineages AS existing_lineage
            ON existing_lineage.lineage_id = existing.lineage_id
          JOIN catalog_lineages AS new_lineage
            ON new_lineage.lineage_id = NEW.lineage_id
          WHERE existing.name = NEW.name
            AND existing_lineage.kind = new_lineage.kind
            AND existing.lineage_id <> NEW.lineage_id
        )
        BEGIN
          SELECT RAISE(
            ABORT,
            'catalog lineage names are never reused across lineages of one kind'
          );
        END
    """,
    "catalog_lineage_aliases_no_update": """
        CREATE TRIGGER catalog_lineage_aliases_no_update
        BEFORE UPDATE ON catalog_lineage_aliases BEGIN
          SELECT RAISE(ABORT, 'catalog lineage aliases are immutable');
        END
    """,
    "catalog_lineage_aliases_no_delete": """
        CREATE TRIGGER catalog_lineage_aliases_no_delete
        BEFORE DELETE ON catalog_lineage_aliases BEGIN
          SELECT RAISE(ABORT, 'catalog lineage aliases are immutable');
        END
    """,
    "catalog_lineage_retirements_no_update": """
        CREATE TRIGGER catalog_lineage_retirements_no_update
        BEFORE UPDATE ON catalog_lineage_retirements BEGIN
          SELECT RAISE(ABORT, 'catalog lineage retirements are immutable');
        END
    """,
    "catalog_lineage_retirements_no_delete": """
        CREATE TRIGGER catalog_lineage_retirements_no_delete
        BEFORE DELETE ON catalog_lineage_retirements BEGIN
          SELECT RAISE(ABORT, 'catalog lineage retirements are immutable');
        END
    """,
    "node_artifacts_v3_no_update": """
        CREATE TRIGGER node_artifacts_v3_no_update
        BEFORE UPDATE ON node_artifacts_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node artifacts are immutable');
        END
    """,
    "node_artifacts_v3_no_delete": """
        CREATE TRIGGER node_artifacts_v3_no_delete
        BEFORE DELETE ON node_artifacts_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node artifacts are immutable');
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
    "node_receipt_outputs_v3_no_update": """
        CREATE TRIGGER node_receipt_outputs_v3_no_update
        BEFORE UPDATE ON node_receipt_outputs_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node receipt outputs are immutable');
        END
    """,
    "node_receipt_outputs_v3_no_delete": """
        CREATE TRIGGER node_receipt_outputs_v3_no_delete
        BEFORE DELETE ON node_receipt_outputs_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node receipt outputs are immutable');
        END
    """,
    "node_receipt_access_v3_no_update": """
        CREATE TRIGGER node_receipt_access_v3_no_update
        BEFORE UPDATE ON node_receipt_access_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node receipt access is immutable');
        END
    """,
    "node_receipt_access_v3_no_delete": """
        CREATE TRIGGER node_receipt_access_v3_no_delete
        BEFORE DELETE ON node_receipt_access_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node receipt access is immutable');
        END
    """,
    "run_instants_start_no_update": """
        CREATE TRIGGER run_instants_start_no_update
        BEFORE UPDATE OF run_id, started_at ON run_instants BEGIN
          SELECT RAISE(ABORT, 'run start instant is immutable');
        END
    """,
    "run_instants_end_once": """
        CREATE TRIGGER run_instants_end_once
        BEFORE UPDATE OF ended_at ON run_instants
        WHEN OLD.ended_at IS NOT NULL OR NEW.ended_at IS NULL BEGIN
          SELECT RAISE(ABORT, 'run end instant is written once');
        END
    """,
    "run_instants_no_delete": """
        CREATE TRIGGER run_instants_no_delete
        BEFORE DELETE ON run_instants BEGIN
          SELECT RAISE(ABORT, 'run instants are immutable');
        END
    """,
    "attempt_instants_start_no_update": """
        CREATE TRIGGER attempt_instants_start_no_update
        BEFORE UPDATE OF attempt_id, started_at ON attempt_instants BEGIN
          SELECT RAISE(ABORT, 'attempt start instant is immutable');
        END
    """,
    "attempt_instants_end_once": """
        CREATE TRIGGER attempt_instants_end_once
        BEFORE UPDATE OF ended_at ON attempt_instants
        WHEN OLD.ended_at IS NOT NULL OR NEW.ended_at IS NULL BEGIN
          SELECT RAISE(ABORT, 'attempt end instant is written once');
        END
    """,
    "attempt_instants_no_delete": """
        CREATE TRIGGER attempt_instants_no_delete
        BEFORE DELETE ON attempt_instants BEGIN
          SELECT RAISE(ABORT, 'attempt instants are immutable');
        END
    """,
    "event_instants_no_update": """
        CREATE TRIGGER event_instants_no_update
        BEFORE UPDATE ON event_instants BEGIN
          SELECT RAISE(ABORT, 'event instants are immutable');
        END
    """,
    "event_instants_no_delete": """
        CREATE TRIGGER event_instants_no_delete
        BEFORE DELETE ON event_instants BEGIN
          SELECT RAISE(ABORT, 'event instants are immutable');
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


class StoreMigrationRefused(RuntimeError):
    """The offline migrate command will not alter this store."""


class StoreInUse(StoreMigrationRefused):
    def __init__(self) -> None:
        super().__init__(
            "the database is in use; stop the process that holds it and retry"
        )


@dataclass(frozen=True)
class StoreMigrationReport:
    source_version: int
    target_version: int
    fingerprint_sha256: str
    already_current: bool
    steps: tuple[tuple[int, int, str], ...]


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
    connection: sqlite3.Connection,
    table_name: str,
    *,
    version: int = SCHEMA_VERSION,
) -> _TableSchemaFingerprint:
    create_record = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    if create_record is None or create_record[0] is None:
        raise UnsupportedSchemaVersion(
            f"malformed v{version} product table {table_name}"
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


def _table_names_for_version(version: int) -> frozenset[str]:
    later = {run_instants.name, attempt_instants.name, event_instants.name}
    if version in {SCHEMA_VERSION, _VERSION_TWENTY_TWO}:
        return PRODUCT_TABLE_NAMES
    if version in {_VERSION_TWENTY_ONE, _VERSION_TWENTY, _VERSION_NINETEEN}:
        return PRODUCT_TABLE_NAMES - later
    if version in {
        _VERSION_EIGHTEEN,
        _VERSION_SEVENTEEN,
        _VERSION_SIXTEEN,
        _VERSION_FIFTEEN,
    }:
        return PRODUCT_TABLE_NAMES - {artifacts.name} - later
    if version == _VERSION_FOURTEEN:
        return PRODUCT_TABLE_NAMES - {artifacts.name, tool_redemptions.name} - later
    if version == _VERSION_THIRTEEN:
        return (
            PRODUCT_TABLE_NAMES
            - {
                artifacts.name,
                run_inputs_v3.name,
                tool_redemptions.name,
            }
            - later
        )
    raise UnsupportedSchemaVersion(version)


def _product_schema_fingerprint(
    connection: sqlite3.Connection,
    table_names: frozenset[str] | None = None,
    *,
    version: int = SCHEMA_VERSION,
) -> _ProductSchemaFingerprint:
    names = PRODUCT_TABLE_NAMES if table_names is None else table_names
    tables = tuple(
        _table_fingerprint(connection, table_name, version=version)
        for table_name in sorted(names)
    )
    placeholders = ",".join("?" for _ in names)
    triggers = tuple(
        (str(record[0]), str(record[1]), _normalized_sql(record[2]))
        for record in connection.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master "
            f"WHERE type='trigger' AND tbl_name IN ({placeholders}) ORDER BY name",
            tuple(sorted(names)),
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
        observed = _product_schema_fingerprint(
            connection, _table_names_for_version(version), version=version
        )
    except UnsupportedSchemaVersion as error:
        raise UnsupportedSchemaVersion(
            f"malformed v{version} product schema fingerprint"
        ) from error
    expected = _PRODUCT_SCHEMA_FINGERPRINT_SHA256.get(version)
    if expected is None or _product_schema_fingerprint_sha256(observed) != expected:
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


def _read_declared_schema_version(connection: sqlite3.Connection) -> int:
    table_names = {
        str(record[0])
        for record in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if atelier_schema_versions.name not in table_names:
        raise StoreMigrationRefused(
            "missing version owner; this command will not alter it"
        )
    rows = connection.execute(
        f"SELECT version FROM {atelier_schema_versions.name}"
    ).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], int):
        raise StoreMigrationRefused(
            f"schema version {tuple(row[0] for row in rows)!r} is unreadable; "
            "this command will not alter it"
        )
    return int(rows[0][0])


def _is_sqlite_lock(error: BaseException) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


def _raise_declared_version(
    connection: sqlite3.Connection, source: int, target: int
) -> None:
    changed = connection.execute(
        f"UPDATE {atelier_schema_versions.name} SET version = ? WHERE version = ?",
        (target, source),
    ).rowcount
    if changed != 1:
        raise StoreMigrationRefused(
            f"schema version CAS {source} -> {target} changed nothing; "
            "this command will not alter it"
        )


def _added_table_step(
    table: sa.Table, triggers: tuple[str, ...], source: int, target: int
) -> Callable[[sqlite3.Connection], None]:
    """One additive hop: a table this version introduces, its triggers, the CAS.

    Two published steps add exactly one immutable table, so the hop is written
    once rather than copied per version; what differs between them is only the
    table, its triggers, and the two version numbers.
    """

    def apply(connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table.name,),
        ).fetchone()
        if existing is not None:
            raise StoreMigrationRefused(
                f"schema version {source} already has {table.name}; "
                "this command will not alter it"
            )
        connection.execute(
            str(CreateTable(table).compile(dialect=sqlite_dialect.dialect()))
        )
        for trigger in triggers:
            connection.execute(_PRODUCT_TRIGGERS[trigger])
        _raise_declared_version(connection, source, target)

    return apply


def _table_shape_at(version: int, table: sa.Table) -> str:
    """The `CREATE TABLE` text this table has at one published schema version.

    The current version is the declaration; every earlier one is a record, and
    `published_schema_shapes` says why it may not be derived.
    """
    if version == SCHEMA_VERSION:
        return str(CreateTable(table).compile(dialect=sqlite_dialect.dialect()))
    frozen_shape = PUBLISHED_TABLE_SHAPES.get((version, table.name))
    if frozen_shape is None:
        raise StoreMigrationRefused(
            f"no published shape of {table.name} at schema version {version} is "
            "recorded, so this hop cannot rebuild it"
        )
    return frozen_shape


def _column_names(connection: sqlite3.Connection, table_name: str) -> tuple[str, ...]:
    return tuple(
        str(record[1])
        for record in connection.execute(f"PRAGMA table_info({table_name})")
    )


def _columns_a_row_must_carry(
    connection: sqlite3.Connection, table_name: str
) -> frozenset[str]:
    """The columns of this table no stored row may leave empty.

    Read from the table SQLite actually holds rather than from the declaration:
    a rebuild materialises the shape of its own target version, and only the
    newest of those is the declaration.
    """
    return frozenset(
        str(record[1])
        for record in connection.execute(f"PRAGMA table_info({table_name})")
        if int(record[3]) == 1 and record[4] is None
    )


def _rebuild_product_table(
    connection: sqlite3.Connection,
    table: sa.Table,
    parked_name: str,
    triggers: tuple[str, ...],
    source_version: int,
    target_version: int,
    filled_columns: Mapping[str, str] = {},
    trigger_source: Mapping[str, str] | None = None,
) -> None:
    """Republish one table in its target shape and carry every stored row over.

    SQLite changes neither a key nor a constraint in place, so every shape hop is
    this same rebuild: park the predecessor, create the target shape, copy by
    column name, drop the predecessor. Which columns are carried is read from the
    two tables themselves rather than hand-kept per step, so a column a hop adds
    is simply not carried and a column it drops simply stops being.

    `filled_columns` names the value each carried row gets in a column the
    predecessor does not have. A column that needs one and has none is refused
    here by name -- the store would otherwise answer with an integrity error
    about a column the operator never heard of.

    `trigger_source` is the trigger text to install after the rebuild. Earlier
    hops that rebuild this table must reinstall the trigger of *their* target,
    not today's, or an intermediate fingerprint breaks.
    """

    trigger_sql = _PRODUCT_TRIGGERS if trigger_source is None else trigger_source

    # The fingerprint this store was checked against says nothing about objects
    # outside the product schema, so any object holding the parking name is
    # refused before the first statement rather than overwritten.
    if connection.execute(
        "SELECT name FROM sqlite_master WHERE name=?", (parked_name,)
    ).fetchone():
        raise StoreMigrationRefused(
            f"schema version {source_version} already has {parked_name}; "
            "this command will not alter it"
        )
    indexes = sorted(table.indexes, key=lambda index: index.name or "")
    for trigger in triggers:
        connection.execute(f"DROP TRIGGER {trigger}")
    for index in indexes:
        connection.execute(f"DROP INDEX {index.name}")
    # Children declare their foreign keys on this table by name, and a plain
    # rename would rewrite them to point at the predecessor this hop drops.
    connection.execute("PRAGMA legacy_alter_table=ON")
    try:
        connection.execute(f"ALTER TABLE {table.name} RENAME TO {parked_name}")
    finally:
        connection.execute("PRAGMA legacy_alter_table=OFF")
    connection.execute(_table_shape_at(target_version, table))
    parked_columns = set(_column_names(connection, parked_name))
    carried = [
        name for name in _column_names(connection, table.name) if name in parked_columns
    ]
    unfillable = (
        _columns_a_row_must_carry(connection, table.name)
        - parked_columns
        - set(filled_columns)
    )
    if unfillable:
        raise StoreMigrationRefused(
            f"schema version {target_version} adds {', '.join(sorted(unfillable))} to "
            f"{table.name} and no value is declared for the rows already stored"
        )
    written = ", ".join(carried + list(filled_columns))
    read = ", ".join(carried + list(filled_columns.values()))
    connection.execute(
        f"INSERT INTO {table.name} ({written}) SELECT {read} FROM {parked_name}"
    )
    connection.execute(f"DROP TABLE {parked_name}")
    for index in indexes:
        connection.execute(
            str(CreateIndex(index).compile(dialect=sqlite_dialect.dialect()))
        )
    for trigger in triggers:
        connection.execute(trigger_sql[trigger])


_RUN_EVENTS_TRIGGERS = ("run_events_no_update", "run_events_no_delete")
_PREDECESSOR_RUN_EVENTS = "run_events_before_the_receipt_column"


def _apply_v15_to_v16(connection: sqlite3.Connection) -> None:
    """Give an event the column v3 of its hash binds, and keep every stored row.

    Nothing already written is reinterpreted: an event from before this version
    carries NULL, which is what "this attempt recorded no receipt binding"
    means, and never an invented hash.
    """

    _rebuild_product_table(
        connection,
        run_events,
        _PREDECESSOR_RUN_EVENTS,
        _RUN_EVENTS_TRIGGERS,
        _VERSION_FIFTEEN,
        _VERSION_SIXTEEN,
    )
    _raise_declared_version(connection, _VERSION_FIFTEEN, _VERSION_SIXTEEN)


_AGENT_ATTEMPTS_TRIGGERS = (
    "agent_attempts_state_transition",
    "agent_attempts_no_delete",
)
_AGENT_RECEIPTS_V2_TRIGGERS = (
    "agent_receipts_v2_no_update",
    "agent_receipts_v2_no_delete",
)
_NODE_EXECUTION_REQUESTS_TRIGGERS = (
    "node_execution_requests_v3_no_update",
    "node_execution_requests_v3_no_delete",
)
_PREDECESSOR_AGENT_ATTEMPTS = "agent_attempts_before_the_refusal_code"
_V17_AGENT_ATTEMPT_TRIGGERS = {
    "agent_attempts_state_transition": _PRODUCT_TRIGGERS[
        "agent_attempts_state_transition"
    ].replace(
        "('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED',\n"
        "                'AGENT_REFUSED')",
        "('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED')",
    ),
    "agent_attempts_no_delete": _PRODUCT_TRIGGERS["agent_attempts_no_delete"],
}


def _apply_v16_to_v17(connection: sqlite3.Connection) -> None:
    """Admit the refusal's own failure code, and keep every stored row.

    Every stored FAILED attempt already carries `PROCESS_EXITED_UNSUCCESSFULLY`,
    which the widened constraint still admits, so nothing is reinterpreted.
    """

    _rebuild_product_table(
        connection,
        agent_attempts,
        _PREDECESSOR_AGENT_ATTEMPTS,
        _AGENT_ATTEMPTS_TRIGGERS,
        _VERSION_SIXTEEN,
        _VERSION_SEVENTEEN,
        trigger_source=_V17_AGENT_ATTEMPT_TRIGGERS,
    )
    _raise_declared_version(connection, _VERSION_SIXTEEN, _VERSION_SEVENTEEN)


_PREDECESSOR_RUNS = "runs_before_failed_state"


def _apply_v17_to_v18(connection: sqlite3.Connection) -> None:
    """Admit FAILED as a run ending, and keep every stored row.

    Every stored run is still STARTED, waiting, or COMPLETED, which the widened
    constraint still admits, and nothing is reinterpreted. Inventory that should
    have ended is a serve-start convergence, not this hop's job.
    """

    _rebuild_product_table(
        connection,
        runs,
        _PREDECESSOR_RUNS,
        ("runs_binding_no_update",),
        _VERSION_SEVENTEEN,
        _VERSION_EIGHTEEN,
    )
    _raise_declared_version(connection, _VERSION_SEVENTEEN, _VERSION_EIGHTEEN)


_PREDECESSOR_ROUNDLESS_RUNS = "runs_before_the_round_column"
_PREDECESSOR_ROUNDLESS_RUN_EVENTS = "run_events_before_the_round_column"
_PREDECESSOR_REQUEST_KEYED_REQUESTS = (
    "node_execution_requests_v3_before_the_execution_key"
)
_PREDECESSOR_ONCE_PER_RUN_AGENT_RECEIPTS = "agent_receipts_v2_before_the_round"


def _apply_v19_to_v20(connection: sqlite3.Connection) -> None:
    """Give the round a durable home, and read every stored row as round one.

    Every run, event and agent receipt this store already holds was written
    before a document could declare a loop, so each of them stands in the first
    round -- that is a fact about them, not a default filled in to make a column
    fit.

    Two keys go with it, because both said "once per run" about something that
    is now once per round. A node execution request keyed by the request hash
    made the second round of a node vanish into the first, and an agent receipt
    keyed by (run, revision, node) refused the second round outright; each is
    replaced by the node execution key that says the same thing exactly.
    """

    _rebuild_product_table(
        connection,
        runs,
        _PREDECESSOR_ROUNDLESS_RUNS,
        ("runs_binding_no_update",),
        _VERSION_NINETEEN,
        _VERSION_TWENTY,
        {runs.c.current_round_ordinal.name: str(FIRST_ROUND_ORDINAL)},
    )
    _rebuild_product_table(
        connection,
        run_events,
        _PREDECESSOR_ROUNDLESS_RUN_EVENTS,
        _RUN_EVENTS_TRIGGERS,
        _VERSION_NINETEEN,
        _VERSION_TWENTY,
        {run_events.c.round_ordinal.name: str(FIRST_ROUND_ORDINAL)},
    )
    _rebuild_product_table(
        connection,
        node_execution_requests_v3,
        _PREDECESSOR_REQUEST_KEYED_REQUESTS,
        _NODE_EXECUTION_REQUESTS_TRIGGERS,
        _VERSION_NINETEEN,
        _VERSION_TWENTY,
    )
    _rebuild_product_table(
        connection,
        agent_receipts_v2,
        _PREDECESSOR_ONCE_PER_RUN_AGENT_RECEIPTS,
        _AGENT_RECEIPTS_V2_TRIGGERS,
        _VERSION_NINETEEN,
        _VERSION_TWENTY,
        {agent_receipts_v2.c.round_ordinal.name: str(FIRST_ROUND_ORDINAL)},
    )
    _raise_declared_version(connection, _VERSION_NINETEEN, _VERSION_TWENTY)


_AGENT_CONFIGURATION_REVISIONS_TRIGGERS = (
    "agent_configuration_revisions_no_update",
    "agent_configuration_revisions_no_delete",
)
_PREDECESSOR_TOOL_FREE_CONFIGURATIONS = (
    "agent_configuration_revisions_before_workspace_tools"
)


def _apply_v20_to_v21(connection: sqlite3.Connection) -> None:
    """Admit headless_with_tools as a requested capability, and keep every row.

    SQLite cannot widen a table CHECK in place, so this is the same rebuild every
    shape hop is. Every stored configuration requests `headless` or
    `interactive`, which the widened constraint still admits, and no stored one
    can name the tool executor either, because no predecessor store could publish
    it -- so no row is reinterpreted and none needs a value filled in.
    """

    _rebuild_product_table(
        connection,
        agent_configuration_revisions,
        _PREDECESSOR_TOOL_FREE_CONFIGURATIONS,
        _AGENT_CONFIGURATION_REVISIONS_TRIGGERS,
        _VERSION_TWENTY,
        _VERSION_TWENTY_ONE,
    )
    _raise_declared_version(connection, _VERSION_TWENTY, _VERSION_TWENTY_ONE)


_INSTANT_TABLES = (run_instants, attempt_instants, event_instants)
_INSTANT_TRIGGERS = (
    "run_instants_start_no_update",
    "run_instants_end_once",
    "run_instants_no_delete",
    "attempt_instants_start_no_update",
    "attempt_instants_end_once",
    "attempt_instants_no_delete",
    "event_instants_no_update",
    "event_instants_no_delete",
)


def _apply_v21_to_v22(connection: sqlite3.Connection) -> None:
    """Give runs, attempts, and events a home for the instant they were written.

    Three additive tables, no reinterpretation of a predecessor row: a run that
    already existed has no instant, which is what "this store never recorded
    when" means, and never an invented clock.
    """

    for table in _INSTANT_TABLES:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table.name,),
        ).fetchone()
        if existing is not None:
            raise StoreMigrationRefused(
                f"schema version {_VERSION_TWENTY_ONE} already has {table.name}; "
                "this command will not alter it"
            )
        connection.execute(
            str(CreateTable(table).compile(dialect=sqlite_dialect.dialect()))
        )
    for trigger in _INSTANT_TRIGGERS:
        connection.execute(_PRODUCT_TRIGGERS[trigger])
    _raise_declared_version(connection, _VERSION_TWENTY_ONE, _VERSION_TWENTY_TWO)


_PREDECESSOR_ATTEMPTS_BEFORE_AGENT_REFUSED = "agent_attempts_before_agent_refused"


def _apply_v22_to_v23(connection: sqlite3.Connection) -> None:
    """Admit AGENT_REFUSED as a failure code, and keep every stored row.

    Every stored FAILED attempt already carries PROCESS_EXITED_UNSUCCESSFULLY
    or OUTPUT_SCHEMA_REFUSED, which the widened constraint still admits, so
    nothing is reinterpreted.
    """

    _rebuild_product_table(
        connection,
        agent_attempts,
        _PREDECESSOR_ATTEMPTS_BEFORE_AGENT_REFUSED,
        _AGENT_ATTEMPTS_TRIGGERS,
        _VERSION_TWENTY_TWO,
        SCHEMA_VERSION,
    )
    _raise_declared_version(connection, _VERSION_TWENTY_TWO, SCHEMA_VERSION)


@dataclass(frozen=True)
class _SchemaMigrationStep:
    source_version: int
    target_version: int
    apply: Callable[[sqlite3.Connection], None]


_SCHEMA_MIGRATION_STEPS: tuple[_SchemaMigrationStep, ...] = (
    _SchemaMigrationStep(
        _VERSION_THIRTEEN,
        _VERSION_FOURTEEN,
        _added_table_step(
            run_inputs_v3,
            ("run_inputs_v3_no_update", "run_inputs_v3_no_delete"),
            _VERSION_THIRTEEN,
            _VERSION_FOURTEEN,
        ),
    ),
    _SchemaMigrationStep(
        _VERSION_FOURTEEN,
        _VERSION_FIFTEEN,
        _added_table_step(
            tool_redemptions,
            ("tool_redemptions_no_update", "tool_redemptions_no_delete"),
            _VERSION_FOURTEEN,
            _VERSION_FIFTEEN,
        ),
    ),
    _SchemaMigrationStep(_VERSION_FIFTEEN, _VERSION_SIXTEEN, _apply_v15_to_v16),
    _SchemaMigrationStep(_VERSION_SIXTEEN, _VERSION_SEVENTEEN, _apply_v16_to_v17),
    _SchemaMigrationStep(_VERSION_SEVENTEEN, _VERSION_EIGHTEEN, _apply_v17_to_v18),
    _SchemaMigrationStep(
        _VERSION_EIGHTEEN,
        _VERSION_NINETEEN,
        _added_table_step(
            artifacts,
            ("artifacts_no_update", "artifacts_no_delete"),
            _VERSION_EIGHTEEN,
            _VERSION_NINETEEN,
        ),
    ),
    _SchemaMigrationStep(_VERSION_NINETEEN, _VERSION_TWENTY, _apply_v19_to_v20),
    _SchemaMigrationStep(_VERSION_TWENTY, _VERSION_TWENTY_ONE, _apply_v20_to_v21),
    _SchemaMigrationStep(_VERSION_TWENTY_ONE, _VERSION_TWENTY_TWO, _apply_v21_to_v22),
    _SchemaMigrationStep(_VERSION_TWENTY_TWO, SCHEMA_VERSION, _apply_v22_to_v23),
)
_SCHEMA_MIGRATION_BY_SOURCE = {
    step.source_version: step for step in _SCHEMA_MIGRATION_STEPS
}


def _fingerprint_for_version(connection: sqlite3.Connection, version: int) -> str:
    _require_product_shape(connection, version)
    return _product_schema_fingerprint_sha256(
        _product_schema_fingerprint(
            connection, _table_names_for_version(version), version=version
        )
    )


def _inspect_store_readonly(database_path: Path) -> tuple[int, str | None]:
    """Read the version, and the fingerprint when this command can honour it.

    A refuse path must not open the file for write: converting journal mode or
    taking a write lock would mutate a store we then claim we left alone.
    """

    try:
        with sqlite3.connect(
            f"{database_path.resolve().as_uri()}?mode=ro", uri=True
        ) as connection:
            version = _read_declared_schema_version(connection)
            if version == SCHEMA_VERSION or version in _SCHEMA_MIGRATION_BY_SOURCE:
                try:
                    return version, _fingerprint_for_version(connection, version)
                except UnsupportedSchemaVersion as error:
                    raise StoreMigrationRefused(str(error)) from error
            return version, None
    except StoreMigrationRefused:
        raise
    except sqlite3.DatabaseError as error:
        if _is_sqlite_lock(error):
            raise StoreInUse() from error
        raise StoreMigrationRefused(
            "the database is unreadable; this command will not alter it"
        ) from error


def migrate_store(database_path: Path) -> StoreMigrationReport:
    """Raise one existing store to SCHEMA_VERSION, or refuse it unaltered.

    The hop is a SQLite transaction on the named file: additive DDL and a
    version CAS, then the published fingerprint. A copy-then-swap would have
    to checkpoint WAL, copy the sidecar files, and still risk a torn rename;
    the object's native atomicity is the transaction. Each committed step is
    a complete published schema, never a half-written one.
    """

    if database_path.is_dir():
        raise StoreMigrationRefused(
            f"{database_path} is a directory, not a database file"
        )
    if not database_path.is_file() or database_path.stat().st_size == 0:
        raise StoreMigrationRefused(
            f"{database_path} is not a database file; "
            "this command does not create a store"
        )

    source_version, preview_fingerprint = _inspect_store_readonly(database_path)
    if source_version == SCHEMA_VERSION:
        if preview_fingerprint is None:
            raise StoreMigrationRefused(
                f"schema version {SCHEMA_VERSION} fingerprint could not be read; "
                "this command will not alter it"
            )
        return StoreMigrationReport(
            source_version,
            SCHEMA_VERSION,
            preview_fingerprint,
            True,
            (),
        )
    step = _SCHEMA_MIGRATION_BY_SOURCE.get(source_version)
    if step is None:
        if source_version in _OFFLINE_CUTOVER_VERSIONS:
            raisable = ", ".join(
                str(version) for version in sorted(_SCHEMA_MIGRATION_BY_SOURCE)
            )
            raise StoreMigrationRefused(
                f"schema version {source_version} has no migration step; "
                f"only version {raisable} can be raised to {SCHEMA_VERSION}. "
                "runtime startup still refuses it without mutation"
            )
        raise StoreMigrationRefused(
            f"schema version {source_version} is unknown; "
            "this command will not alter it"
        )

    connection = sqlite3.connect(str(database_path.resolve()), timeout=0)
    try:
        connection.execute("PRAGMA busy_timeout=0")
        # Deliberately OFF for the hop, per SQLite's own table-rebuild recipe:
        # with enforcement on, renaming a table out rewrites every child
        # declaration to follow the parked name, which the rebuild then drops.
        # Row-level integrity is not waived -- the explicit foreign_key_check
        # before the commit refuses the whole hop on any violation.
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if _is_sqlite_lock(error):
                raise StoreInUse() from error
            raise StoreMigrationRefused(
                "the database could not be locked; this command will not alter it"
            ) from error
        try:
            locked_version = _read_declared_schema_version(connection)
            if locked_version != source_version:
                raise StoreMigrationRefused(
                    f"schema version changed from {source_version} to "
                    f"{locked_version} before the hop; this command will not alter it"
                )
            try:
                _fingerprint_for_version(connection, locked_version)
            except UnsupportedSchemaVersion as error:
                raise StoreMigrationRefused(str(error)) from error
            completed: list[tuple[int, int, str]] = []
            current = locked_version
            while current != SCHEMA_VERSION:
                current_step = _SCHEMA_MIGRATION_BY_SOURCE.get(current)
                if current_step is None:
                    raise StoreMigrationRefused(
                        f"schema version {current} has no migration step; "
                        "this command will not alter it"
                    )
                current_step.apply(connection)
                try:
                    fingerprint = _fingerprint_for_version(
                        connection, current_step.target_version
                    )
                except UnsupportedSchemaVersion as error:
                    raise StoreMigrationRefused(str(error)) from error
                completed.append(
                    (
                        current_step.source_version,
                        current_step.target_version,
                        fingerprint,
                    )
                )
                current = current_step.target_version
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                tables = ", ".join(sorted({str(row[0]) for row in violations}))
                raise StoreMigrationRefused(
                    f"the migrated store violates foreign keys in {tables}; "
                    "this command will not alter it"
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass
        return StoreMigrationReport(
            source_version,
            SCHEMA_VERSION,
            completed[-1][2],
            False,
            tuple(completed),
        )
    finally:
        connection.close()
