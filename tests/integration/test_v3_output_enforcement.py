"""What an agent answered is held to the schema its node declared, or nothing lands.

**The belegte case this file exists for.** The first real self-run proved the
engine and not the work: a V3 agent was asked for compact JSON, the provider
answered prose, and the atelier wrote `AGENT_COMPLETED` anyway, ran the successor
and ended the run successfully. The contract was declared, pinned and resolved --
and never read against the bytes that came back.

**Where the proof sits, and why there.** The seam under test is the success
write: it is the one moment a provider's answer becomes a fact the product cannot
take back, it is inside the transaction that writes the receipt, the event and
the run's advance, and it is where the graph and the pinned schema are both in
hand. Driving it directly is what makes the refusal case deterministic -- a
launched run whose write refuses stands still, and waiting for a run to keep not
completing proves nothing a clock could not fake.

What is asserted after a refusal is therefore the durable state an operator can
read: no agent receipt, no completion event, no advanced run -- and, since the
record family got its writer, a `failed` `node-receipt/v3` carrying the schema
owner's words, an attempt ended under `OUTPUT_SCHEMA_REFUSED`, and an
`AGENT_FAILED` event, instead of an exception that killed the driver and left a
silent `STARTED`/`LAUNCH_ARMED` zombie (the live class of run 8846bf47).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.claude_subscription import ClaudeSubscriptionExecutorFactory
from atelier2.adapters.codex_subscription import (
    CodexSandboxMode,
    CodexSubscriptionExecutorFactory,
    CodexSubscriptionSettings,
)
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.artifact_store import read_stored_artifact
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.node_records import keep_node_receipt
from atelier2.adapters.dbos.run_store import run_from_record_with_bindings
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_attempt_receipts_v3,
    agent_attempts,
    agent_receipts_v2,
    context_packages_v3,
    node_artifacts_v3,
    node_execution_requests_v3,
    node_receipt_outputs_v3,
    node_receipts_v3,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.grok_subscription import (
    GrokSubscriptionExecutorFactory,
    GrokSubscriptionSettings,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.compose_node_job import (
    OUTPUT_SCHEMA_REPAIR_HEADING,
    NodeJobCompositionVersion,
    OutputSchemaRepair,
    node_job,
)
from atelier2.contracts.agent_attempts import (
    MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES,
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
    ProcessExitSignature,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.artifacts import Artifact, ArtifactHash
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    NodeExecutionId,
    RunEventKind,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    DeclaredContextPackageHash,
    NodeExecutionRequestHash,
    NodeReceipt,
    NodeReceiptReason,
    PersistedReceiptDisposition,
    node_receipt_reason,
    read_stored_node_receipt_reason,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationTerminalConflict,
    AgentAttemptFailed,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.agent_executions import (
    AgentExecutionResult,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import NodeDetailFound, RunFound
from tests.scenarios.agents import (
    agent_scratch_root,
    claude_subscription_deployment,
    failing_agent_executor_factory,
    process_invocation,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_queries

PLAN_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    b'{"type": "object", "properties": {"steps": {"type": "integer", '
    b'"minimum": 1}}, "required": ["steps"], "additionalProperties": false}',
)
"""What the node's author asked for: one object naming how many steps it plans."""

NODE = "plan"
SUCCESSOR = "review"
INSTRUCTION = b"Answer with the plan this node declared."
RUN = RunId("v3/output-contract")
THE_ANSWER_THE_SCHEMA_ADMITS = b'{"steps": 3}'
THE_ANSWER_THE_SCHEMA_REFUSES = b"Sure! I would plan this in three steps."


def planning_document(schema: PublishedRevision) -> bytes:
    """One agent node whose one output pins a schema that means something."""
    return f"""format_version: 3
name: Plan the work
nodes:
  - id: {NODE}
    type: agent
    role: builder
    mode: headless
    instruction: {INSTRUCTION.decode("ascii")}
    outputs:
      - name: plan
        schema:
          ref: plan-schema
          revision: {schema.revision_hash.value}
""".encode()


def reviewed_planning_document(schema: PublishedRevision) -> bytes:
    """The same node, followed by the one node that waits on what it produced.

    Its author wrote no `join`, and that omission *is* `all_succeeded` -- the one
    join ADR 0006 leaves implicit and the only one a run of this build applies.
    So this document is where the join can be measured at all: whether the
    successor starts is the whole observable difference between a released and a
    blocked edge.
    """
    return (
        planning_document(schema)
        + f"""  - id: {SUCCESSOR}
    type: agent
    role: builder
    mode: headless
    instruction: Judge the plan you were handed.
    depends_on: [{NODE}]
    inputs:
      - name: plan
        from: {{node: {NODE}, output: plan}}
    outputs:
      - name: verdict
        schema:
          ref: plan-schema
          revision: {schema.revision_hash.value}
""".encode()
    )


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    """A runtime with no provider that can answer: this file drives the store."""
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-output-contract-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (failing_agent_executor_factory("exact", []),),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def armed_attempt(
    runtime: DbosRuntime,
    document: bytes | None = None,
    schema: PublishedRevision = PLAN_SCHEMA,
) -> AgentAttemptExecution:
    """One started run of a planning document, armed at its first agent node."""
    document = planning_document(schema) if document is None else document
    published = DbosCatalogStore(runtime.engine).publish_revision(schema)
    assert isinstance(
        published, (PublishedRevisionCreated, PublishedRevisionExisting)
    ), published
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    created = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(created, DurableRunCreated), created

    revision_hash = WorkflowRevisionHash(workflow.revision_hash.value)
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    assert isinstance(run, RunV3)
    binding = run.agent_bindings[0]
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(RUN, revision_hash, NODE),
        RUN,
        revision_hash,
        NODE,
        binding,
        AgentExecutorOperationalIdentity("exact-operation"),
        INSTRUCTION,
    )
    execution = AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)
    store.claim(execution)
    return execution


def repair_attempt(
    runtime: DbosRuntime, refused: AgentAttemptExecution
) -> AgentAttemptExecution:
    """Read the durable verdict into the one repair request it ordered."""
    with runtime.engine.connect() as connection:
        reason = connection.scalar(
            sa.select(agent_attempt_receipts_v3.c.reason).where(
                agent_attempt_receipts_v3.c.attempt_id == refused.attempt_id.value
            )
        )
        attempt_id = connection.scalar(
            sa.select(agent_attempts.c.attempt_id).where(
                agent_attempts.c.node_execution_id
                == refused.request.node_execution_id.value,
                agent_attempts.c.attempt_ordinal == REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
            )
        )
    assert reason is not None
    request = AgentExecutionRequestV2(
        refused.request.node_execution_id,
        refused.request.run_id,
        refused.request.workflow_revision_hash,
        refused.request.node_id,
        refused.request.resolved_binding,
        refused.request.executor_operational_identity,
        node_job(
            INSTRUCTION.decode("ascii"),
            composition_version=NodeJobCompositionVersion.OUTPUT_SCHEMA_REPAIR,
            output_schema_repair=OutputSchemaRepair(str(reason)),
        ).encode("utf-8"),
        refused.request.declared_output_schema_bytes,
        refused.request.round_ordinal,
        refused.request.maximum_assistant_turns,
    )
    repair = AgentAttemptExecution(
        request,
        AgentAttemptId(str(attempt_id)),
        REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    )
    assert repair.attempt_id == AgentAttemptId.for_execution(
        request.node_execution_id,
        request.request_hash,
        REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    )
    DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version).claim(
        repair
    )
    return repair


def durable_answer(runtime: DbosRuntime) -> tuple[int, int, str, str]:
    """What an operator can read afterwards: receipts, events, run and attempt."""
    with runtime.engine.connect() as connection:
        receipts = connection.scalar(
            sa.select(sa.func.count()).select_from(agent_receipts_v2)
        )
        completions = connection.scalar(
            sa.select(sa.func.count())
            .select_from(run_events)
            .where(run_events.c.run_id == RUN.value)
        )
        state = connection.scalar(
            sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
        )
        attempt = connection.scalar(
            sa.select(agent_attempts.c.state).where(
                agent_attempts.c.run_id == RUN.value
            )
        )
    return int(receipts or 0), int(completions or 0), str(state), str(attempt)


@pytest.mark.proves("bytes-their-own-schema-refuses-never-become-a-success")
@pytest.mark.proves("a-refused-output-ends-its-attempt-durably-named")
@pytest.mark.parametrize(
    ("answered", "expected_reason"),
    [
        pytest.param(
            b"Sure! I would plan this in three steps.",
            "output-schema-refused: instance-not-json: Expecting value",
            id="prose where JSON was declared",
        ),
        pytest.param(
            b'{"steps": 0}',
            "output-schema-refused: schema-violated: /steps: is less than the minimum of 1",
            id="a value out of bounds",
        ),
        pytest.param(
            b'{"stps": 3}',
            "output-schema-refused: schema-violated: the value itself: 'steps' is a required property",
            id="a misspelled field",
        ),
        pytest.param(
            b'["one", "two"]',
            "output-schema-refused: schema-violated: the value itself: is not of type 'object'",
            id="the wrong JSON shape",
        ),
        pytest.param(
            b'{"steps": 3}\xff',
            "output-schema-refused: instance-not-utf8: invalid start byte",
            id="broken UTF-8",
        ),
        pytest.param(
            b'{"steps": 1, "steps": 2}',
            "output-schema-refused: duplicate-object-key: steps",
            id="one field answered twice",
        ),
    ],
)
def test_an_answer_its_own_schema_refuses_never_becomes_a_success(
    runtime: DbosRuntime, answered: bytes, expected_reason: str
) -> None:
    """The belegte case and its neighbours: refused by name, and durably so.

    Each of these reached `AGENT_COMPLETED` before #57, because the pinned
    schema was resolved and then never applied to what came back. After #57 the
    refusal was an exception that killed the driver: run `STARTED`, attempt
    `LAUNCH_ARMED`, no trace anywhere -- the silent zombie class of the live
    store. Now the refusal is returned instead of raised, and everything a
    success would have written is still absent -- no agent receipt, no
    completion event, no advanced run -- while the refusal itself is durable:
    the attempt is `FAILED` under `OUTPUT_SCHEMA_REFUSED`, the `AGENT_FAILED`
    event carries that code, and the `failed` `node-receipt/v3` names the
    schema owner's verdict, bound to the request the start persisted.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(execution, AgentExecutionResult(answered))

    assert isinstance(outcome, AgentAttemptFailed), outcome
    assert outcome.attempt.state is AgentAttemptState.FAILED
    assert outcome.attempt.failure_code is AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED
    assert durable_answer(runtime) == (
        0,
        0,
        RunState.STARTED.value,
        AgentAttemptState.FAILED.value,
    )
    with runtime.engine.connect() as connection:
        receipt = (
            connection.execute(
                sa.select(agent_attempt_receipts_v3).where(
                    agent_attempt_receipts_v3.c.attempt_id == execution.attempt_id.value
                )
            )
            .mappings()
            .one()
        )
        request = (
            connection.execute(
                sa.select(node_execution_requests_v3).where(
                    node_execution_requests_v3.c.node_execution_id
                    == execution.request.node_execution_id.value
                )
            )
            .mappings()
            .one()
        )
        artifacts = connection.scalar(
            sa.select(sa.func.count()).select_from(node_artifacts_v3)
        )
        outputs = connection.scalar(
            sa.select(sa.func.count()).select_from(node_receipt_outputs_v3)
        )
    reason = str(receipt["reason"])
    assert reason == expected_reason
    assert answered.decode("utf-8", errors="replace") not in reason
    assert len(reason) <= MAXIMUM_AGENT_FIELD_CHARACTERS
    assert receipt["schema_revision_hash"] == PLAN_SCHEMA.revision_hash.value
    assert receipt["value_hash"] == Sha256Hash.of(answered).value
    assert request["node_execution_id"] == execution.request.node_execution_id.value
    assert (int(artifacts or 0), int(outputs or 0)) == (0, 0)


@pytest.mark.proves("a-schema-refusal-orders-one-durable-repair-round")
def test_a_schema_refusal_orders_one_repair_that_can_succeed(
    runtime: DbosRuntime,
) -> None:
    first = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    refused = store.complete_success(
        first, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )

    assert isinstance(refused, AgentAttemptFailed), refused
    repair = repair_attempt(runtime, first)
    reason = repair.request.job_bytes.decode("utf-8")
    assert OUTPUT_SCHEMA_REPAIR_HEADING in reason
    assert "output-schema-refused: instance-not-json" in reason

    succeeded = store.complete_success(
        repair, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_ADMITS)
    )

    assert isinstance(succeeded, AgentAttemptSucceeded), succeeded
    with runtime.engine.connect() as connection:
        attempts = tuple(
            connection.execute(
                sa.select(
                    agent_attempts.c.attempt_ordinal,
                    agent_attempts.c.state,
                    agent_attempts.c.failure_code,
                ).order_by(agent_attempts.c.attempt_ordinal)
            ).all()
        )
        refusal_receipts = connection.scalar(
            sa.select(sa.func.count()).select_from(agent_attempt_receipts_v3)
        )
    assert attempts == (
        (
            1,
            AgentAttemptState.FAILED.value,
            AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED.value,
        ),
        (2, AgentAttemptState.SUCCEEDED.value, None),
    )
    assert refusal_receipts == 1
    assert durable_answer(runtime) == (
        1,
        1,
        RunState.COMPLETED.value,
        AgentAttemptState.FAILED.value,
    )


@pytest.mark.proves("a-schema-refusal-orders-only-one-repair-round")
def test_two_schema_refusals_end_failed_under_output_schema_refused(
    runtime: DbosRuntime,
) -> None:
    first = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.complete_success(first, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES))
    repair = repair_attempt(runtime, first)

    failed = store.complete_success(
        repair, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )

    assert isinstance(failed, AgentAttemptFailed), failed
    assert failed.attempt.failure_code is AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED
    with runtime.engine.connect() as connection:
        attempts = tuple(
            connection.execute(
                sa.select(
                    agent_attempts.c.attempt_ordinal,
                    agent_attempts.c.state,
                    agent_attempts.c.failure_code,
                ).order_by(agent_attempts.c.attempt_ordinal)
            ).all()
        )
        receipts = connection.scalar(
            sa.select(sa.func.count()).select_from(node_receipts_v3)
        )
    assert attempts == (
        (
            1,
            AgentAttemptState.FAILED.value,
            AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED.value,
        ),
        (
            2,
            AgentAttemptState.FAILED.value,
            AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED.value,
        ),
    )
    assert receipts == 1
    assert durable_answer(runtime)[2] == RunState.FAILED.value


@pytest.mark.proves("an-admitted-answer-does-not-open-a-repair-round")
def test_an_admitted_answer_opens_no_repair_round(runtime: DbosRuntime) -> None:
    execution = armed_attempt(runtime)

    outcome = DbosAgentAttemptStore(
        runtime.engine, runtime.settings.application_version
    ).complete_success(execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_ADMITS))

    assert isinstance(outcome, AgentAttemptSucceeded), outcome
    with runtime.engine.connect() as connection:
        attempts = connection.scalar(
            sa.select(sa.func.count()).select_from(agent_attempts)
        )
        refusals = connection.scalar(
            sa.select(sa.func.count()).select_from(agent_attempt_receipts_v3)
        )
    assert (attempts, refusals) == (1, 0)


@pytest.mark.proves("a-refused-output-ends-its-attempt-durably-named")
def test_a_large_schema_refusal_has_a_compact_reason_and_exact_detail_output(
    runtime: DbosRuntime,
) -> None:
    """The receipt names the refusal; node detail holds the refused bytes.

    JSON Schema includes the rejected value in this diagnostic, which used to
    make the durable reason exceed the event field limit. A new refusal keeps
    the stable schema judgment instead, while the receipt identity still points
    node detail at the exact bytes the schema read.
    """
    rejected = b'{"steps":"' + b"not an integer " * 400 + b'"}'
    assert len(rejected) > MAXIMUM_AGENT_FIELD_CHARACTERS
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(execution, AgentExecutionResult(rejected))

    assert isinstance(outcome, AgentAttemptFailed), outcome
    with runtime.engine.connect() as connection:
        receipt = (
            connection.execute(
                sa.select(agent_attempt_receipts_v3).where(
                    agent_attempt_receipts_v3.c.attempt_id == execution.attempt_id.value
                )
            )
            .mappings()
            .one()
        )
    reason = str(receipt["reason"])
    assert reason.startswith("output-schema-refused: schema-violated: /steps: ")
    assert "is not of type 'integer'" in reason
    assert rejected.decode("utf-8") not in reason
    assert len(reason) <= MAXIMUM_AGENT_FIELD_CHARACTERS
    assert receipt["schema_revision_hash"] == PLAN_SCHEMA.revision_hash.value
    assert receipt["value_hash"] == Sha256Hash.of(rejected).value

    found = durable_queries(runtime.engine).get_node_detail(RUN, NODE)
    assert isinstance(found, NodeDetailFound), found
    assert found.detail.refusal == reason
    assert found.detail.refusal_output is not None
    assert found.detail.refusal_output.value == rejected
    assert found.detail.refusal_output.value_hash == Sha256Hash.of(rejected)


@pytest.mark.proves("a-refused-output-ends-its-attempt-durably-named")
@pytest.mark.parametrize(
    ("schema", "answered", "expected_rule", "rejected_value_repr"),
    [
        pytest.param(
            PublishedRevision(RevisionKind.SCHEMA, b"false"),
            b'"private"',
            "False schema does not allow",
            "'private'",
            id="false-schema rule ends with the rejected string",
        ),
        pytest.param(
            PublishedRevision(RevisionKind.SCHEMA, b'{"type": "integer"}'),
            b'"as is okay"',
            "is not of type 'integer'",
            "'as is okay'",
            id="string repr contains a rule separator",
        ),
    ],
)
def test_a_schema_refusal_elides_its_exact_value_repr_from_the_stored_reason(
    runtime: DbosRuntime,
    schema: PublishedRevision,
    answered: bytes,
    expected_rule: str,
    rejected_value_repr: str,
) -> None:
    """The durable diagnosis retains the rule, never JSON Schema's value repr."""
    execution = armed_attempt(runtime, schema=schema)

    outcome = DbosAgentAttemptStore(
        runtime.engine, runtime.settings.application_version
    ).complete_success(execution, AgentExecutionResult(answered))

    assert isinstance(outcome, AgentAttemptFailed), outcome
    with runtime.engine.connect() as connection:
        stored = (
            connection.execute(
                sa.select(agent_attempt_receipts_v3).where(
                    agent_attempt_receipts_v3.c.attempt_id == execution.attempt_id.value
                )
            )
            .mappings()
            .one()
        )
    reason = str(stored["reason"])
    assert reason.startswith("output-schema-refused: schema-violated: ")
    assert expected_rule in reason
    assert rejected_value_repr not in reason
    assert stored["schema_revision_hash"] == schema.revision_hash.value
    assert stored["value_hash"] == Sha256Hash.of(answered).value


@pytest.mark.parametrize(
    ("answered", "kept"),
    [
        pytest.param(
            THE_ANSWER_THE_SCHEMA_REFUSES,
            THE_ANSWER_THE_SCHEMA_REFUSES,
            id="the exact bytes the schema read",
        ),
        pytest.param(b"", None, id="nothing to keep when nothing was written"),
    ],
)
def test_a_refused_answer_stays_readable_under_the_hash_its_receipt_kept(
    runtime: DbosRuntime, answered: bytes, kept: bytes | None
) -> None:
    """The live gap of #664: the verdict survived the refusal and the evidence did not.

    A refused execution has no agent receipt and no completion event, so the
    only record of what the provider actually wrote is the value hash its
    `failed` receipt kept -- and that hash addressed nothing, which made the
    episode undiagnosable: the operator was told an answer had been refused and
    could never see the answer. The terminal write now holds those exact bytes
    under exactly that address, so the hash an operator reads off the receipt
    resolves to the material it names.

    Bytes nobody wrote are the honest exception. There is nothing to keep, and
    keeping nothing must not turn the failure into an exception -- that would
    resurrect the silent `STARTED`/`LAUNCH_ARMED` zombie this whole seam exists
    to end -- so the attempt still ends `FAILED` and the address stays empty.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(execution, AgentExecutionResult(answered))

    assert isinstance(outcome, AgentAttemptFailed), outcome
    with runtime.engine.connect() as connection:
        receipt = (
            connection.execute(
                sa.select(
                    agent_attempt_receipts_v3.c.value_hash,
                    agent_attempt_receipts_v3.c.artifact_hash,
                ).where(
                    agent_attempt_receipts_v3.c.attempt_id == execution.attempt_id.value
                )
            )
            .mappings()
            .one()
        )
        assert receipt["value_hash"] == Sha256Hash.of(answered).value
        held = (
            None
            if receipt["artifact_hash"] is None
            else read_stored_artifact(
                connection, ArtifactHash(str(receipt["artifact_hash"]))
            )
        )
    assert held == (None if kept is None else Artifact(kept))


@pytest.mark.proves("bytes-their-own-schema-refuses-never-become-a-success")
@pytest.mark.proves("a-succeeded-node-writes-artifact-and-receipt-atomically")
def test_the_answer_its_own_schema_admits_is_written_as_the_one_success(
    runtime: DbosRuntime,
) -> None:
    """The contrast, so the refusals above cannot pass by refusing everything.

    The same seam, the same schema, one answer that satisfies it: the receipt
    keeps the exact bytes, the completion event carries them under their own
    hash, and the run reaches its terminal state because this node is its sink.
    The record family keeps the same success durably in its own vocabulary: the
    produced value as `node-artifact/v3`, the `succeeded` `node-receipt/v3`
    naming it, and the receipt-output row binding the two -- written in the same
    transaction, bound to the request the start persisted.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    succeeded = store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_ADMITS)
    )

    assert isinstance(succeeded, AgentAttemptSucceeded), succeeded
    assert durable_answer(runtime) == (
        1,
        1,
        RunState.COMPLETED.value,
        AgentAttemptState.SUCCEEDED.value,
    )
    with runtime.engine.connect() as connection:
        payload = connection.scalar(
            sa.select(run_events.c.payload).where(run_events.c.run_id == RUN.value)
        )
        kept = connection.scalar(sa.select(agent_receipts_v2.c.output_bytes))
        receipt = connection.execute(sa.select(node_receipts_v3)).mappings().one()
        artifact = connection.execute(sa.select(node_artifacts_v3)).mappings().one()
        output = connection.execute(sa.select(node_receipt_outputs_v3)).mappings().one()
        request = (
            connection.execute(sa.select(node_execution_requests_v3)).mappings().one()
        )
    assert bytes(payload or b"") == THE_ANSWER_THE_SCHEMA_ADMITS
    assert bytes(kept or b"") == THE_ANSWER_THE_SCHEMA_ADMITS
    assert receipt["disposition"] == "succeeded"
    reason, schema_revision, value_hash = read_stored_node_receipt_reason(
        str(receipt["reason"])
    )
    assert reason == NodeReceiptReason.OUTPUT_ACCEPTED.value
    assert schema_revision == PLAN_SCHEMA.revision_hash
    assert value_hash == Sha256Hash.of(THE_ANSWER_THE_SCHEMA_ADMITS)
    assert receipt["request_hash"] == request["request_hash"]
    assert receipt["context_package_hash"] == request["context_package_hash"]
    assert bytes(artifact["value"]) == THE_ANSWER_THE_SCHEMA_ADMITS
    assert artifact["output_name"] == "plan"
    assert output["node_execution_id"] == receipt["node_execution_id"]
    assert output["value_hash"] == artifact["value_hash"]


@pytest.mark.proves("a-refused-output-ends-its-attempt-durably-named")
@pytest.mark.proves("a-node-that-stops-the-run-says-what-it-is-waiting-on")
def test_the_refused_nodes_detail_names_the_durably_stored_reason(
    runtime: DbosRuntime,
) -> None:
    """The operator's click reads the stored verdict, not a recomputation.

    The refusal struck this node's own output, so no successor composition can
    resurface it -- the only voice it has is the `failed` receipt the terminal
    write kept. The node detail answers with exactly that reason.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    outcome = store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )
    assert isinstance(outcome, AgentAttemptFailed), outcome

    found = durable_queries(runtime.engine).get_node_detail(RUN, NODE)

    assert isinstance(found, NodeDetailFound), found
    refusal = found.detail.refusal
    assert refusal is not None
    assert refusal.startswith(f"{NodeReceiptReason.OUTPUT_SCHEMA_REFUSED.value}: ")
    assert "instance-not-json" in refusal


def test_a_crash_inside_the_terminal_write_leaves_no_partial_receipt(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt, the attempt and the event become durable together or not at all.

    The refusal path writes the node receipt first, then the attempt CAS, then
    the event -- one transaction. A crash between the first write and the commit
    must leave none of them, because a receipt beside a still-armed attempt
    would say the execution ended while the run disagrees.
    """
    import atelier2.adapters.dbos.agent_attempt_store as store_module

    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    def crash(*arguments: object, **keywords: object) -> None:
        raise RuntimeError("crash inside the terminal write")

    monkeypatch.setattr(store_module, "replacement_workflow_id_for", crash)
    with pytest.raises(RuntimeError, match="crash inside the terminal write"):
        store.complete_success(execution, AgentExecutionResult(b"prose, not JSON"))

    with runtime.engine.connect() as connection:
        receipts = connection.scalar(
            sa.select(sa.func.count()).select_from(node_receipts_v3)
        )
        refusal_receipts = connection.scalar(
            sa.select(sa.func.count()).select_from(agent_attempt_receipts_v3)
        )
        events = connection.scalar(
            sa.select(sa.func.count())
            .select_from(run_events)
            .where(run_events.c.run_id == RUN.value)
        )
    assert (int(receipts or 0), int(refusal_receipts or 0), int(events or 0)) == (
        0,
        0,
        0,
    )
    assert durable_answer(runtime)[2:] == (
        RunState.STARTED.value,
        AgentAttemptState.LAUNCH_ARMED.value,
    )


@dataclass(frozen=True)
class ChainAfterTheAnswer:
    """What an operator can read about a two-node chain once its first node ended."""

    run_state: str
    current_node_id: str
    attempted_node_ids: tuple[str, ...]
    artifact_count: int
    receipt_dispositions: tuple[tuple[str, str], ...]


def chain_after_the_answer(
    runtime: DbosRuntime, revision_hash: WorkflowRevisionHash
) -> ChainAfterTheAnswer:
    """Read the chain's durable state, with every node named as its author did."""
    node_of_execution = {
        NodeExecutionId.for_node(RUN, revision_hash, node).value: node
        for node in (NODE, SUCCESSOR)
    }
    with runtime.engine.connect() as connection:
        run = (
            connection.execute(
                sa.select(runs.c.state, runs.c.current_node_id).where(
                    runs.c.run_id == RUN.value
                )
            )
            .mappings()
            .one()
        )
        attempted = tuple(
            connection.scalars(
                sa.select(agent_attempts.c.node_id)
                .where(agent_attempts.c.run_id == RUN.value)
                .order_by(agent_attempts.c.node_id)
            )
        )
        artifacts = connection.scalar(
            sa.select(sa.func.count()).select_from(node_artifacts_v3)
        )
        receipts = connection.execute(
            sa.select(
                node_receipts_v3.c.node_execution_id, node_receipts_v3.c.disposition
            )
        ).all()
    return ChainAfterTheAnswer(
        str(run["state"]),
        str(run["current_node_id"]),
        attempted,
        int(artifacts or 0),
        tuple(
            sorted(
                (node_of_execution[str(execution)], str(disposition))
                for execution, disposition in receipts
            )
        ),
    )


@pytest.mark.proves("a-refused-output-never-releases-the-node-behind-it")
def test_a_refused_output_never_releases_the_node_behind_it(
    runtime: DbosRuntime,
) -> None:
    """`all_succeeded` blocks, measured where blocking is the whole difference.

    The successor declares one dependency and no `join`, and ADR 0006 says that
    omission *is* `all_succeeded`. The refusal therefore has to end the edge as
    well as the node: the run does not move off the node that was refused, no
    attempt is ever opened for the successor, and the successor holds no receipt
    of its own -- it is not blocked by a receipt, it is simply never reached.
    Nothing else may start it either: there is no path past a node that did not
    succeed.
    """
    execution = armed_attempt(runtime, reviewed_planning_document(PLAN_SCHEMA))
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )

    assert isinstance(outcome, AgentAttemptFailed), outcome
    assert chain_after_the_answer(
        runtime, execution.request.workflow_revision_hash
    ) == ChainAfterTheAnswer(RunState.STARTED.value, NODE, (NODE, NODE), 0, ())


@pytest.mark.proves("a-refused-output-never-releases-the-node-behind-it")
def test_the_answer_its_schema_admits_releases_the_node_behind_it(
    runtime: DbosRuntime,
) -> None:
    """The contrast, so the block above cannot pass by never releasing anything.

    One character of difference in the answer, and the same edge releases: the
    run stands on the successor, the value the schema admitted is the one durable
    artifact, and the refused node's receipt reads `succeeded` instead.
    """
    execution = armed_attempt(runtime, reviewed_planning_document(PLAN_SCHEMA))
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_ADMITS)
    )

    assert isinstance(outcome, AgentAttemptSucceeded), outcome
    assert chain_after_the_answer(
        runtime, execution.request.workflow_revision_hash
    ) == ChainAfterTheAnswer(
        RunState.STARTED.value, SUCCESSOR, (NODE,), 1, ((NODE, "succeeded"),)
    )


def replacement_of(
    runtime: DbosRuntime, execution: AgentAttemptExecution
) -> AgentAttemptExecution:
    """Stop this attempt with the one replacement the store mints, and arm it.

    The replacement is the only retry construct a run of this build has: a second
    attempt of the *same* node execution, under the same request hash, carrying
    ordinal two. Everything a retry could duplicate is keyed by that shared node
    execution, which is what makes the counts below the honest measurement.
    """
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    armed = store.load(execution.attempt_id)
    command = CancelAgentAttemptRequest(
        RUN,
        execution.attempt_id,
        "retry-the-plan",
        armed.state_version,
        AgentAttemptReplacement.ONE,
    )
    assert isinstance(
        store.request_cancellation(command), AgentAttemptCancellationAccepted
    )
    stopped = store.attest_cancellation_cleanup(
        command, AgentAttemptCancellationDisposition.NEVER_LAUNCHED, None, None
    )
    replacement_attempt_id = stopped.replacement_attempt_id
    assert replacement_attempt_id is not None, stopped
    replacement = AgentAttemptExecution(
        execution.request, replacement_attempt_id, REPLACEMENT_AGENT_ATTEMPT_ORDINAL
    )
    store.claim(replacement)
    return replacement


def kept_for_the_node_execution(
    runtime: DbosRuntime,
) -> tuple[int, int, tuple[str, ...]]:
    """The artifacts kept, the attempts that stood, and every terminal receipt.

    The attempt count is carried beside the records because it is what makes the
    records mean something: one receipt after one attempt proves nothing about a
    retry, and one receipt after two is the sentence.
    """
    with runtime.engine.connect() as connection:
        artifacts = connection.scalar(
            sa.select(sa.func.count()).select_from(node_artifacts_v3)
        )
        dispositions = tuple(
            connection.scalars(sa.select(node_receipts_v3.c.disposition))
        )
        ordinals = tuple(
            connection.scalars(
                sa.select(agent_attempts.c.attempt_ordinal).order_by(
                    agent_attempts.c.attempt_ordinal
                )
            )
        )
    return int(artifacts or 0), len(ordinals), dispositions


@pytest.mark.proves("a-retry-is-held-to-the-same-contract-and-leaves-one-of-each")
@pytest.mark.parametrize(
    ("answered", "ending", "disposition", "artifacts"),
    [
        pytest.param(
            THE_ANSWER_THE_SCHEMA_ADMITS,
            AgentAttemptSucceeded,
            "succeeded",
            1,
            id="admitted",
        ),
        pytest.param(
            THE_ANSWER_THE_SCHEMA_REFUSES,
            AgentAttemptFailed,
            "failed",
            0,
            id="refused",
        ),
    ],
)
def test_a_replacement_attempt_answers_to_the_same_declared_schema(
    runtime: DbosRuntime,
    answered: bytes,
    ending: type[AgentAttemptSucceeded | AgentAttemptFailed],
    disposition: str,
    artifacts: int,
) -> None:
    """A second attempt buys no second contract, and no second record.

    The first attempt is stopped before it answered, so nothing it did is durable
    and the node execution is still owed exactly one ending. Whatever the
    replacement answers, the seam that judges it is the one the first attempt
    would have met -- and what it leaves behind is one terminal receipt for the
    node execution and, only where the schema admitted the bytes, one artifact.
    Two attempts stand in the store; one ending stands for the node.
    """
    execution = armed_attempt(runtime)
    replacement = replacement_of(runtime, execution)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(replacement, AgentExecutionResult(answered))

    assert isinstance(outcome, ending), outcome
    assert kept_for_the_node_execution(runtime) == (artifacts, 2, (disposition,))


@pytest.mark.proves("a-retry-is-held-to-the-same-contract-and-leaves-one-of-each")
def test_a_refused_output_can_never_be_retried_into_a_second_ending(
    runtime: DbosRuntime,
) -> None:
    """Once the node execution has its ending, no retry construct can reach it.

    This is the other half of "no second terminal receipt", and it holds one step
    earlier than a write conflict would: the refusal ended the attempt, and the
    replacement is minted by a cancellation, which an ended attempt refuses. So
    the `failed` receipt is not defended against a second writer -- there is no
    second writer to defend against, and the store says so in its own word rather
    than by silently doing nothing.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    refused = store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )
    assert isinstance(refused, AgentAttemptFailed), refused

    retried = store.request_cancellation(
        CancelAgentAttemptRequest(
            RUN,
            execution.attempt_id,
            "retry-the-refused-plan",
            refused.attempt.state_version,
            AgentAttemptReplacement.ONE,
        )
    )

    assert isinstance(retried, AgentAttemptCancellationTerminalConflict), retried
    assert kept_for_the_node_execution(runtime) == (0, 2, ())


def test_the_start_persists_the_request_and_package_the_receipt_will_bind(
    runtime: DbosRuntime,
) -> None:
    """The start transaction leaves the durable half every receipt names.

    One `node-execution-request/v3` and its `context-package/v3` per node,
    written with the run itself -- so a terminal receipt can bind a request
    that already existed before anything ran, and a run that exists without
    its requests can only be one from before this writer.
    """
    execution = armed_attempt(runtime)
    with runtime.engine.connect() as connection:
        request = (
            connection.execute(sa.select(node_execution_requests_v3)).mappings().one()
        )
        package_hashes = set(
            connection.scalars(sa.select(context_packages_v3.c.package_hash))
        )
    assert request["node_execution_id"] == execution.request.node_execution_id.value
    assert request["context_package_hash"] in package_hashes


@pytest.mark.proves("a-dead-process-ends-its-attempt-durably-named")
def test_a_process_that_died_leaves_a_failed_receipt_naming_what_it_left(
    runtime: DbosRuntime,
) -> None:
    """The live class of `desk/review-pr313-b`: a provider dies, nobody can read why.

    Everything a refused answer leaves was already durable; a dead process left
    the attempt row and the event, which say *that* it failed and never *why* --
    the provider's own last words lived in a log line and nowhere else. Now the
    same seam keeps them: a `failed` `node-receipt/v3` under the process token,
    carrying the exit and the standard error, bound to the request the start
    persisted. The `AGENT_FAILED` event deliberately does not grow: it stays the
    bare code, because the event stream is a surface anybody may subscribe to.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_known_failure(
        execution, ProcessExitSignature(1, b"grok: prompt file rejected")
    )

    assert isinstance(outcome, AgentAttemptFailed), outcome
    assert (
        outcome.attempt.failure_code
        is AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
    )
    assert durable_answer(runtime) == (
        0,
        1,
        RunState.FAILED.value,
        AgentAttemptState.FAILED.value,
    )
    with runtime.engine.connect() as connection:
        event = (
            connection.execute(
                sa.select(run_events.c.event_kind, run_events.c.payload).where(
                    run_events.c.run_id == RUN.value
                )
            )
            .mappings()
            .one()
        )
        receipt = (
            connection.execute(
                sa.select(node_receipts_v3).where(
                    node_receipts_v3.c.node_execution_id
                    == execution.request.node_execution_id.value
                )
            )
            .mappings()
            .one()
        )
        request = (
            connection.execute(sa.select(node_execution_requests_v3)).mappings().one()
        )
    assert event["event_kind"] == RunEventKind.AGENT_FAILED.value
    assert bytes(
        event["payload"]
    ) == AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY.value.encode("ascii")
    assert receipt["disposition"] == "failed"
    reason = str(receipt["reason"])
    assert reason.startswith(
        f"{NodeReceiptReason.PROCESS_EXITED_UNSUCCESSFULLY.value}: "
    )
    assert "exited with code 1" in reason
    assert "grok: prompt file rejected" in reason
    assert receipt["request_hash"] == request["request_hash"]
    assert receipt["context_package_hash"] == request["context_package_hash"]


@pytest.mark.proves("a-dead-process-ends-its-attempt-durably-named")
def test_the_dead_nodes_detail_names_what_supervision_saw(
    runtime: DbosRuntime,
) -> None:
    """The operator's click answers the question the code alone never could.

    A process failure has no schema owner to quote, so the node detail's reason
    is the only voice this ending has -- and it is read from the store rather
    than recomputed, because nothing can recompute what a process said as it
    died.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.complete_known_failure(execution, ProcessExitSignature(-9, b"out of memory"))

    found = durable_queries(runtime.engine).get_node_detail(RUN, NODE)

    assert isinstance(found, NodeDetailFound), found
    refusal = found.detail.refusal
    assert refusal is not None
    assert refusal.startswith(
        f"{NodeReceiptReason.PROCESS_EXITED_UNSUCCESSFULLY.value}: "
    )
    assert "killed by signal 9" in refusal
    assert "out of memory" in refusal


def test_only_a_bounded_tail_of_what_the_process_said_becomes_durable(
    runtime: DbosRuntime,
) -> None:
    """A talkative provider cannot turn the receipt table into its log.

    Supervision already collects far more standard error than a receipt should
    hold, and the words that explain an ending are the last ones -- so the
    stored reason keeps the tail under its own bound and the opening of a long
    complaint never reaches the store at all.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    said = b"opening-line " + b"z" * 100_000 + b" closing-line"

    store.complete_known_failure(execution, ProcessExitSignature(1, said))

    with runtime.engine.connect() as connection:
        reason = str(connection.scalar(sa.select(node_receipts_v3.c.reason)))
    assert "closing-line" in reason
    assert "opening-line" not in reason
    assert len(reason) < 2 * MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES


@pytest.mark.proves("an-uncontinuable-run-ends-under-the-node-reason")
def test_a_refused_answer_ends_the_run_as_failed(runtime: DbosRuntime) -> None:
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )
    store.complete_success(
        repair_attempt(runtime, execution),
        AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES),
    )

    assert durable_answer(runtime)[2:] == (
        RunState.FAILED.value,
        AgentAttemptState.FAILED.value,
    )
    found = durable_queries(runtime.engine).get_run(RUN)
    assert isinstance(found, RunFound), found
    assert found.projection.run.state is RunState.FAILED
    assert found.projection.run.terminal_hash is not None


@pytest.mark.proves("a-serve-start-ends-uncontinuable-inventory")
def test_a_serve_start_ends_a_started_run_whose_current_node_already_failed(
    runtime: DbosRuntime,
) -> None:
    """The leftover inventory: attempt already FAILED, run still STARTED."""

    from atelier2.adapters.dbos.uncontinuable_runs import DbosUncontinuableRunStore
    from atelier2.application.converge_uncontinuable_runs import (
        converge_uncontinuable_runs,
    )

    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )
    store.complete_success(
        repair_attempt(runtime, execution),
        AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES),
    )
    assert durable_answer(runtime)[2] == RunState.FAILED.value

    with runtime.engine.connect() as connection:
        reverted = connection.execute(
            runs.update()
            .where(runs.c.run_id == RUN.value)
            .values(state=RunState.STARTED.value, terminal_hash=None)
        )
        assert reverted.rowcount == 1
        connection.commit()

    assert durable_answer(runtime)[2] == RunState.STARTED.value

    ended = converge_uncontinuable_runs(
        DbosUncontinuableRunStore(runtime.engine, runtime.settings.application_version)
    )

    assert ended == (RUN,)
    assert durable_answer(runtime)[2] == RunState.FAILED.value
    found = durable_queries(runtime.engine).get_run(RUN)
    assert isinstance(found, RunFound), found
    assert found.projection.run.terminal_hash is not None


def leftover_started_run_whose_current_node_failed(
    runtime: DbosRuntime,
) -> AgentAttemptExecution:
    """The leftover inventory shape: attempt already FAILED, run still STARTED."""

    execution = armed_attempt(runtime)
    attempts = DbosAgentAttemptStore(
        runtime.engine, runtime.settings.application_version
    )
    attempts.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )
    attempts.complete_success(
        repair_attempt(runtime, execution),
        AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES),
    )
    with runtime.engine.connect() as connection:
        reverted = connection.execute(
            runs.update()
            .where(runs.c.run_id == RUN.value)
            .values(state=RunState.STARTED.value, terminal_hash=None)
        )
        assert reverted.rowcount == 1
        connection.commit()
    assert durable_answer(runtime)[2] == RunState.STARTED.value
    return execution


def test_converge_uncontinuable_runs_does_not_end_a_run_that_can_still_continue(
    runtime: DbosRuntime,
) -> None:
    """A replacement still in flight is not leftover inventory.

    Dropping the ``~sa.exists`` clause lists this row, and this test dies.
    """

    from atelier2.adapters.dbos.uncontinuable_runs import DbosUncontinuableRunStore
    from atelier2.application.converge_uncontinuable_runs import (
        converge_uncontinuable_runs,
    )

    execution = armed_attempt(runtime)
    DbosAgentAttemptStore(
        runtime.engine, runtime.settings.application_version
    ).complete_success(execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES))
    store = DbosUncontinuableRunStore(
        runtime.engine, runtime.settings.application_version
    )
    assert store.uncontinuable_runs() == ()
    assert converge_uncontinuable_runs(store) == ()
    assert durable_answer(runtime)[2] == RunState.STARTED.value


def test_converge_uncontinuable_runs_does_not_end_a_run_waiting_for_input(
    runtime: DbosRuntime,
) -> None:
    """A run waiting for a person is not leftover inventory.

    Dropping the STARTED filter lists this row, and this test dies.
    """

    from atelier2.adapters.dbos.uncontinuable_runs import DbosUncontinuableRunStore
    from atelier2.application.converge_uncontinuable_runs import (
        converge_uncontinuable_runs,
    )

    leftover_started_run_whose_current_node_failed(runtime)
    with runtime.engine.connect() as connection:
        waiting = connection.execute(
            runs.update()
            .where(runs.c.run_id == RUN.value)
            .values(state=RunState.WAITING_INPUT.value)
        )
        assert waiting.rowcount == 1
        connection.commit()

    store = DbosUncontinuableRunStore(
        runtime.engine, runtime.settings.application_version
    )
    assert store.uncontinuable_runs() == ()
    assert converge_uncontinuable_runs(store) == ()
    assert durable_answer(runtime)[2] == RunState.WAITING_INPUT.value


@pytest.mark.proves("a-judged-receipt-names-the-schema-identity-it-judged")
def test_a_receipt_without_schema_identity_when_a_verdict_exists_is_refused(
    runtime: DbosRuntime,
) -> None:
    """The mutation that bites: a judged receipt that drops the identity dies."""

    execution = armed_attempt(runtime)
    with (
        pytest.raises(ValueError, match="schema identity"),
        canonical_write_transaction(runtime.engine) as connection,
    ):
        keep_node_receipt(
            connection,
            execution.request.node_execution_id,
            PersistedReceiptDisposition.FAILED,
            node_receipt_reason(
                NodeReceiptReason.OUTPUT_SCHEMA_REFUSED, "instance-not-json"
            ),
        )


@pytest.mark.proves("a-judged-receipt-names-the-schema-identity-it-judged")
def test_an_old_receipt_without_schema_identity_remains_readable(
    runtime: DbosRuntime,
) -> None:
    """A pre-identity row is honestly empty, not a refusal and not corruption."""

    execution = armed_attempt(runtime)
    words = node_receipt_reason(
        NodeReceiptReason.OUTPUT_SCHEMA_REFUSED, "instance-not-json"
    )
    with canonical_write_transaction(runtime.engine) as connection:
        request = (
            connection.execute(
                sa.select(node_execution_requests_v3).where(
                    node_execution_requests_v3.c.node_execution_id
                    == execution.request.node_execution_id.value
                )
            )
            .mappings()
            .one()
        )
        receipt = NodeReceipt(
            execution.request.node_execution_id,
            PersistedReceiptDisposition.FAILED,
            words,
            NodeExecutionRequestHash(str(request["request_hash"])),
            DeclaredContextPackageHash(str(request["context_package_hash"])),
            (),
        )
        connection.execute(
            node_receipts_v3.insert().values(
                node_execution_id=receipt.node_execution_id.value,
                disposition=receipt.disposition.value,
                reason=words,
                request_hash=receipt.request_hash.value,
                context_package_hash=receipt.context_package_hash.value,
                receipt_hash=receipt.receipt_hash.value,
            )
        )

    reason, schema_revision, value_hash = read_stored_node_receipt_reason(words)
    assert reason == words
    assert schema_revision is None
    assert value_hash is None
    found = durable_queries(runtime.engine).get_node_detail(RUN, NODE)
    assert isinstance(found, NodeDetailFound), found
    assert found.detail.refusal == words


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _run_fake_process(script: str, working_directory: Path) -> AgentProcessCompletion:
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=working_directory,
        capture_output=True,
        check=False,
    )
    return AgentProcessCompletion(
        completed.returncode, completed.stdout, completed.stderr
    )


def _fake_invocation(working_directory: Path) -> AgentProcessInvocation:
    return process_invocation(
        AgentAttemptId.of(b"parity-attempt"),
        ("fake",),
        working_directory,
    )


def _decoded_by_claude(directory: Path, payload: bytes) -> AgentExecutionResult:
    working = directory / "lease"
    working.mkdir(parents=True)
    envelope = json.dumps(
        {"type": "result", "is_error": False, "result": payload.decode("utf-8")}
    )
    deploy = directory / "deploy"
    deploy.mkdir()
    result = (
        ClaudeSubscriptionExecutorFactory(
            claude_subscription_deployment(deploy, "pass\n")
        )
        .open()
        .decode_process_completion(
            _fake_invocation(working),
            _run_fake_process(f"import sys; sys.stdout.write({envelope!r})", working),
        )
    )
    assert isinstance(result, AgentExecutionResult), result
    return result


def _decoded_by_codex(directory: Path, payload: bytes) -> AgentExecutionResult:
    working = directory / "lease"
    working.mkdir(parents=True)
    credentials = directory / "codex-home"
    credentials.mkdir()
    result = (
        CodexSubscriptionExecutorFactory(
            CodexSubscriptionSettings(
                _write_executable(directory / "codex", "pass\n"),
                credentials,
                os.environ.get("PATH", "/usr/bin"),
                CodexSandboxMode.READ_ONLY,
            )
        )
        .open()
        .decode_process_completion(
            _fake_invocation(working),
            _run_fake_process(
                f"from pathlib import Path; Path('last-message').write_bytes({payload!r})",
                working,
            ),
        )
    )
    assert isinstance(result, AgentExecutionResult), result
    return result


def _decoded_by_grok(directory: Path, payload: bytes) -> AgentExecutionResult:
    working = directory / "lease"
    working.mkdir(parents=True)
    workspace = directory / "workspace"
    credentials = directory / "grok-home"
    workspace.mkdir()
    credentials.mkdir()
    authentication = credentials / "auth.json"
    authentication.write_bytes(b"{}")
    authentication.chmod(0o600)
    envelope = json.dumps({"text": payload.decode("utf-8")})
    result = (
        GrokSubscriptionExecutorFactory(
            GrokSubscriptionSettings(
                _write_executable(directory / "grok", "pass\n"),
                workspace,
                credentials,
                os.environ.get("PATH", "/usr/bin"),
            )
        )
        .open()
        .decode_process_completion(
            _fake_invocation(working),
            _run_fake_process(f"import sys; sys.stdout.write({envelope!r})", working),
        )
    )
    assert isinstance(result, AgentExecutionResult), result
    return result


@pytest.mark.proves("every-executor-meets-the-same-output-contract-seam")
@pytest.mark.parametrize(
    "decode",
    (
        pytest.param(_decoded_by_claude, id="claude"),
        pytest.param(_decoded_by_codex, id="codex"),
        pytest.param(_decoded_by_grok, id="grok"),
    ),
)
def test_every_executor_adapter_refuses_a_schema_violation_on_the_same_seam(
    runtime: DbosRuntime,
    tmp_path: Path,
    decode: Callable[[Path, bytes], AgentExecutionResult],
) -> None:
    """The #326 finding, now by proof: each adapter's decode hits the same seam.

    A fake child writes that adapter's envelope around the same refused bytes.
    Decode succeeds -- the provider answered -- and the success write names the
    same refusal for all three. No billed call.
    """

    execution = armed_attempt(runtime)
    result = decode(tmp_path / "adapter", THE_ANSWER_THE_SCHEMA_REFUSES)
    assert result.output_bytes == THE_ANSWER_THE_SCHEMA_REFUSES

    outcome = DbosAgentAttemptStore(
        runtime.engine, runtime.settings.application_version
    ).complete_success(execution, result)

    assert isinstance(outcome, AgentAttemptFailed), outcome
    assert outcome.attempt.failure_code is AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED
    with runtime.engine.connect() as connection:
        stored = (
            connection.execute(sa.select(agent_attempt_receipts_v3)).mappings().one()
        )
    reason = str(stored["reason"])
    assert reason.startswith(f"{NodeReceiptReason.OUTPUT_SCHEMA_REFUSED.value}: ")
    assert stored["schema_revision_hash"] == PLAN_SCHEMA.revision_hash.value
    assert stored["value_hash"] == Sha256Hash.of(THE_ANSWER_THE_SCHEMA_REFUSES).value
