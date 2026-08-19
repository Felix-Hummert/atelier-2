"""What a product table looked like at a schema version that is no longer current.

A migration step must materialise the shape of **its own** target, and the table
declarations in `schema.py` are only ever the shape of the current version. A
step that read a declaration instead would go on rebuilding a predecessor into
whatever the newest hop last changed -- correct until a second hop touches the
same table, and then a chain that dies in the middle with an error about a column
nobody was migrating. Every published shape a later hop moved away from is
therefore recorded here.

**This text is deliberately not derived, and deliberately not kept in step with
anything.** It is a record of what a published schema said, so the guards that
require a live bound to be written once and derived everywhere would be wrong
about it: an entry here must *not* follow the owner it once agreed with. That is
the whole reason it lives beside the schema rather than inside it. An entry is
appended when a hop leaves a shape behind, and is never edited afterwards.

Indexes and triggers are still taken from the declaration. They are exact while
no hop has changed one, and the published fingerprint the migration runner takes
after **every** step is what refuses the day that stops being true -- loudly,
before the next step, rather than at the end.

The predecessor shapes a *test* builds a store from are that test's own scenario
data and stay there: they are inputs to a fixture, and no production caller
reads them.
"""

from __future__ import annotations

from collections.abc import Mapping

PUBLISHED_TABLE_SHAPES: Mapping[tuple[int, str], str] = {
    (16, "run_events"): """
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
	agent_receipt_hash TEXT, 
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
	CHECK ((agent_attempt_id IS NULL AND attempt_ordinal IS NULL AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (length(agent_attempt_id) = 64 AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' AND attempt_ordinal IN (1, 2) AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind = 'AGENT_CANCEL_REQUESTED' AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NOT NULL)))), 
	CHECK ((event_kind = 'AGENT_COMPLETED' AND (agent_receipt_hash IS NULL OR (length(agent_receipt_hash) = 64 AND agent_receipt_hash NOT GLOB '*[^0-9a-f]*'))) OR (event_kind <> 'AGENT_COMPLETED' AND agent_receipt_hash IS NULL))
)

""",
    (17, "agent_attempts"): """
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
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND process_phase = 'CLEANUP_ATTESTED' AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED') AND receipt_hash IS NULL)), 
	UNIQUE (cancellation_workflow_id), 
	UNIQUE (receipt_hash), 
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)


""",
    (18, "runs"): """
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
	CHECK (state IN ('STARTED', 'WAITING_RECONCILIATION', 'WAITING_INPUT', 'COMPLETED', 'FAILED')), 
	CHECK (state_version >= 0), 
	CHECK (last_event_sequence >= 0), 
	CHECK ((state IN ('COMPLETED', 'FAILED') AND terminal_hash IS NOT NULL AND length(terminal_hash) = 64 AND terminal_hash NOT GLOB '*[^0-9a-f]*') OR (state NOT IN ('COMPLETED', 'FAILED') AND terminal_hash IS NULL)), 
	CHECK ((workflow_format_version = 3 AND run_configuration_revision_hash IS NOT NULL AND length(run_configuration_revision_hash) = 64 AND run_configuration_revision_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version <> 3 AND run_configuration_revision_hash IS NULL)), 
	UNIQUE (bootstrap_workflow_id), 
	FOREIGN KEY(revision_hash) REFERENCES workflow_revisions (revision_hash), 
	FOREIGN KEY(run_configuration_revision_hash) REFERENCES run_configuration_revisions (revision_hash)
)


""",
    (20, "runs"): """
CREATE TABLE runs (
	run_id TEXT NOT NULL, 
	bootstrap_workflow_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	workflow_format_version INTEGER NOT NULL, 
	agent_binding_set_hash TEXT, 
	current_node_id TEXT NOT NULL, 
	current_round_ordinal INTEGER NOT NULL, 
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
	CHECK (current_round_ordinal >= 1), 
	CHECK (workflow_format_version IN (1, 2, 3)), 
	CHECK ((workflow_format_version = 1 AND agent_binding_set_hash IS NULL) OR (workflow_format_version = 2 AND agent_binding_set_hash IS NOT NULL AND length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version = 3 AND (agent_binding_set_hash IS NULL OR (length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*')))), 
	CHECK (state IN ('STARTED', 'WAITING_RECONCILIATION', 'WAITING_INPUT', 'COMPLETED', 'FAILED')), 
	CHECK (state_version >= 0), 
	CHECK (last_event_sequence >= 0), 
	CHECK ((state IN ('COMPLETED', 'FAILED') AND terminal_hash IS NOT NULL AND length(terminal_hash) = 64 AND terminal_hash NOT GLOB '*[^0-9a-f]*') OR (state NOT IN ('COMPLETED', 'FAILED') AND terminal_hash IS NULL)), 
	CHECK ((workflow_format_version = 3 AND run_configuration_revision_hash IS NOT NULL AND length(run_configuration_revision_hash) = 64 AND run_configuration_revision_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version <> 3 AND run_configuration_revision_hash IS NULL)), 
	UNIQUE (bootstrap_workflow_id), 
	FOREIGN KEY(revision_hash) REFERENCES workflow_revisions (revision_hash), 
	FOREIGN KEY(run_configuration_revision_hash) REFERENCES run_configuration_revisions (revision_hash)
)


""",
    (20, "run_events"): """
CREATE TABLE run_events (
	run_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	event_sequence INTEGER NOT NULL, 
	node_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	round_ordinal INTEGER NOT NULL, 
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
	agent_receipt_hash TEXT, 
	PRIMARY KEY (run_id, event_sequence), 
	FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash), 
	FOREIGN KEY(receipt_logical_key, run_id, revision_hash, receipt_result_hash) REFERENCES effect_receipts (logical_key, run_id, workflow_revision_hash, result_hash), 
	CHECK (event_sequence > 0), 
	CHECK (length(node_id) > 0), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (round_ordinal >= 1), 
	CHECK (event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED', 'AGENT_CANCEL_REQUESTED', 'AGENT_CANCELLED', 'AGENT_INTERRUPTED', 'ACTION_RECONCILIATION_REQUIRED', 'ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED', 'WAITING_INPUT', 'WAIT_ANSWERED', 'SUBWORKFLOW_COMPLETED')), 
	CHECK (length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK ((event_kind IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NOT NULL AND length(receipt_logical_key) > 0 AND receipt_result_hash IS NOT NULL AND length(receipt_result_hash) = 64 AND receipt_result_hash NOT GLOB '*[^0-9a-f]*' AND receipt_result_hash = payload_hash) OR (event_kind NOT IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NULL AND receipt_result_hash IS NULL)), 
	CHECK ((agent_attempt_id IS NULL AND attempt_ordinal IS NULL AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (length(agent_attempt_id) = 64 AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' AND attempt_ordinal IN (1, 2) AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind = 'AGENT_CANCEL_REQUESTED' AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NOT NULL)))), 
	CHECK ((event_kind = 'AGENT_COMPLETED' AND (agent_receipt_hash IS NULL OR (length(agent_receipt_hash) = 64 AND agent_receipt_hash NOT GLOB '*[^0-9a-f]*'))) OR (event_kind <> 'AGENT_COMPLETED' AND agent_receipt_hash IS NULL))
)


""",
    (20, "node_execution_requests_v3"): """
CREATE TABLE node_execution_requests_v3 (
	request_hash TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	run_configuration_revision_hash TEXT NOT NULL, 
	context_package_hash TEXT NOT NULL, 
	preimage BLOB NOT NULL, 
	PRIMARY KEY (node_execution_id), 
	UNIQUE (node_execution_id, request_hash), 
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(context_package_hash) = 64 AND context_package_hash NOT GLOB '*[^0-9a-f]*'), 
	FOREIGN KEY(context_package_hash) REFERENCES context_packages_v3 (package_hash), 
	FOREIGN KEY(run_configuration_revision_hash) REFERENCES run_configuration_revisions (revision_hash)
)


""",
    (20, "agent_receipts_v2"): """
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
	round_ordinal INTEGER NOT NULL, 
	PRIMARY KEY (node_execution_id), 
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
	CHECK (round_ordinal >= 1), 
	UNIQUE (receipt_hash)
)


""",
    (21, "agent_configuration_revisions"): """
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
	CHECK (requested_capability IN ('headless', 'headless_with_tools', 'interactive')), 
	CHECK (revision_format_version = 2 OR requested_capability = 'headless'), 
	FOREIGN KEY(auth_profile_revision_hash) REFERENCES auth_profile_revisions (revision_hash)
)


""",
    (22, "agent_attempts"): """
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
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND process_phase = 'CLEANUP_ATTESTED' AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED') AND receipt_hash IS NULL)), 
	UNIQUE (cancellation_workflow_id), 
	UNIQUE (receipt_hash), 
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)


""",
    (23, "agent_attempts"): """
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
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND process_phase = 'CLEANUP_ATTESTED' AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED', 'AGENT_REFUSED') AND receipt_hash IS NULL)), 
	UNIQUE (cancellation_workflow_id), 
	UNIQUE (receipt_hash), 
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)

""",
    (24, "agent_attempts"): """
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
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND process_phase = 'CLEANUP_ATTESTED' AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED', 'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED') AND receipt_hash IS NULL)), 
	UNIQUE (cancellation_workflow_id), 
	UNIQUE (receipt_hash), 
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)

""",
    (15, "tool_redemptions"): """
CREATE TABLE tool_redemptions (
	node_execution_id TEXT NOT NULL, 
	run_id TEXT NOT NULL, 
	workflow_revision_hash TEXT NOT NULL, 
	node_id TEXT NOT NULL, 
	attempt_id TEXT NOT NULL, 
	tool_revision_hash TEXT NOT NULL, 
	capability TEXT NOT NULL, 
	command TEXT NOT NULL, 
	exit_code INTEGER NOT NULL, 
	standard_output_hash TEXT NOT NULL, 
	receipt_hash TEXT NOT NULL, 
	PRIMARY KEY (node_execution_id), 
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(run_id) > 0), 
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_id) BETWEEN 1 AND 1024), 
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(tool_revision_hash) = 64 AND tool_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (capability IN ('run-project-verification')), 
	CHECK (length(command) > 0), 
	CHECK (exit_code BETWEEN -9223372036854775808 AND 9223372036854775807), 
	CHECK (length(standard_output_hash) = 64 AND standard_output_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'), 
	FOREIGN KEY(node_execution_id) REFERENCES agent_receipts_v2 (node_execution_id), 
	FOREIGN KEY(attempt_id) REFERENCES agent_attempts (attempt_id), 
	UNIQUE (receipt_hash)
)

""",
    (25, "tool_redemptions"): """
CREATE TABLE tool_redemptions (
	node_execution_id TEXT NOT NULL, 
	run_id TEXT NOT NULL, 
	workflow_revision_hash TEXT NOT NULL, 
	node_id TEXT NOT NULL, 
	attempt_id TEXT NOT NULL, 
	tool_revision_hash TEXT NOT NULL, 
	capability TEXT NOT NULL, 
	command TEXT NOT NULL, 
	exit_code INTEGER NOT NULL, 
	standard_output_hash TEXT NOT NULL, 
	receipt_hash TEXT NOT NULL, 
	PRIMARY KEY (node_execution_id), 
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(run_id) > 0), 
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_id) BETWEEN 1 AND 1024), 
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(tool_revision_hash) = 64 AND tool_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (capability IN ('run-project-verification')), 
	CHECK (length(command) > 0), 
	CHECK (exit_code BETWEEN -9223372036854775808 AND 9223372036854775807), 
	CHECK (length(standard_output_hash) = 64 AND standard_output_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'), 
	FOREIGN KEY(node_execution_id) REFERENCES agent_receipts_v2 (node_execution_id), 
	FOREIGN KEY(attempt_id) REFERENCES agent_attempts (attempt_id), 
	UNIQUE (receipt_hash)
)

""",
}
