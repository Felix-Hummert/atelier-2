from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    AnswerBytesConflict,
    AnswerExistingApplied,
    AnswerExistingPending,
    AnswerRevisionConflict,
    AnswerStateConflict,
    DurableStateCorrupt,
    NodeMissing,
    RunMissing,
    WriteUnavailable,
    answer_wait_result,
)
from atelier2.application.publish_workflow_revision import (
    UNPROJECTABLE_FORMAT_DETAIL,
    PublicationCollision,
    PublicationCreated,
    PublicationExisting,
    PublicationInvalid,
    publish_workflow_revision,
)
from atelier2.application.reconcile_effect import (
    ReconciliationAcceptedPending,
    ReconciliationCommandConflict,
    ReconciliationDeterminationConflict,
    ReconciliationExistingApplied,
    ReconciliationExistingPending,
    ReconciliationExistingRejected,
    ReconciliationStale,
    ReconciliationTargetMissing,
    reconcile_effect_result,
)
from atelier2.application.start_published_run import (
    AgentExecutorBindingUnavailable,
    RevisionMissing,
    RunCreated,
    RunExisting,
    RunIdentityConflict,
    start_published_run,
)
from atelier2.contracts.effects import (
    ReconcileCommand,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    WaitAnswer,
    WaitAnswerSnapshot,
    WaitAnswerState,
)
from atelier2.contracts.runs import Run, RunId, WorkflowRevision, WorkflowRevisionHash
from atelier2.ports.durable_runs import (
    DurableAgentExecutorCapabilityUnavailable,
    DurableAnswerBytesConflict,
    DurableAnswerCreated,
    DurableAnswerExisting,
    DurableAnswerNodeMissing,
    DurableAnswerRevisionConflict,
    DurableAnswerRunMissing,
    DurableAnswerStateConflict,
    DurablePublishedRunStarter,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableWriteUnavailable,
    StartPublishedRunRequest,
    TransactionalWaitAnswerer,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.effects import (
    DurableReconciliationCommandConflict,
    DurableReconciliationCreated,
    DurableReconciliationDeterminationConflict,
    DurableReconciliationExisting,
    DurableReconciliationTargetMissing,
    TransactionalEffectReconcileCommander,
)
from atelier2.ports.workflow_revisions import (
    DurableRevisionCollision,
    DurableRevisionCreated,
    DurableRevisionExisting,
    WorkflowRevisionPublisher,
)


@dataclass
class FakePort:
    result: object

    def publish(self, _revision: WorkflowRevision) -> object:
        return self.result

    def start_published(self, _request: StartPublishedRunRequest) -> object:
        return self.result

    def submit_result(self, _request: object) -> object:
        return self.result


REVISION = WorkflowRevision(b"format_version: nope")
HASH = WorkflowRevisionHash("0" * 64)
RUN = cast(Run, object())
ANSWER_VALUE = WaitAnswer(
    RunId("run"),
    HASH,
    "wait",
    NodeExecutionId.for_node(RunId("run"), HASH, "wait"),
    b"3",
)
ANSWER = WaitAnswerSnapshot(ANSWER_VALUE, WaitAnswerState.PENDING, 0)
COMMAND = cast(ReconcileCommand, object())


@pytest.mark.parametrize(
    ("port_result", "application_type"),
    [
        (DurableRevisionCreated(REVISION), PublicationCreated),
        (DurableRevisionExisting(REVISION), PublicationExisting),
        (DurableRevisionCollision(), PublicationCollision),
        (DurableWriteUnavailable(), WriteUnavailable),
        (PortDurableStateCorrupt(), DurableStateCorrupt),
    ],
)
def test_publication_maps_every_durable_result(
    port_result: object, application_type: type[object]
) -> None:
    result = publish_workflow_revision(
        b"format_version: 1\nstart: final\nnodes:\n  - {id: final, type: subworkflow, operation: add, operands: [1, 2], next: null}\n",
        cast(WorkflowRevisionPublisher, FakePort(port_result)),
        parse_workflow_document,
    )

    assert isinstance(result, application_type)


def test_publication_rejects_invalid_yaml_before_the_write_port() -> None:
    result = publish_workflow_revision(
        b"!!python/object:unsafe {}",
        cast(WorkflowRevisionPublisher, FakePort(None)),
        parse_workflow_document,
    )

    assert isinstance(result, PublicationInvalid)


def test_publication_refuses_a_parsed_v3_document_before_the_write_port() -> None:
    result = publish_workflow_revision(
        b"format_version: 3\n"
        b"name: Land the comment\n"
        b"nodes:\n"
        b"  - {id: land, type: action, operation: {ref: comment, revision: r}}\n",
        cast(WorkflowRevisionPublisher, FakePort(None)),
        parse_workflow_document,
    )

    assert result == PublicationInvalid(UNPROJECTABLE_FORMAT_DETAIL)


def test_publication_returns_the_graph_validated_before_the_write() -> None:
    document = b"format_version: 1\nstart: final\nnodes:\n  - {id: final, type: subworkflow, operation: add, operands: [1, 2], next: null}\n"
    revision = WorkflowRevision(document)

    result = publish_workflow_revision(
        document,
        cast(
            WorkflowRevisionPublisher,
            FakePort(DurableRevisionCreated(revision)),
        ),
        parse_workflow_document,
    )

    assert isinstance(result, PublicationCreated)
    assert result.projection.revision == revision
    assert result.projection.graph.start == "final"


@pytest.mark.parametrize(
    ("port_result", "application_type"),
    [
        (DurableRunCreated(RUN), RunCreated),
        (DurableRunExisting(RUN), RunExisting),
        (DurableRunRevisionMissing(), RevisionMissing),
        (DurableRunIdentityConflict(), RunIdentityConflict),
        (
            DurableAgentExecutorCapabilityUnavailable(),
            AgentExecutorBindingUnavailable,
        ),
        (DurableWriteUnavailable(), WriteUnavailable),
        (PortDurableStateCorrupt(), DurableStateCorrupt),
    ],
)
def test_start_maps_every_durable_result(
    port_result: object, application_type: type[object]
) -> None:
    result = start_published_run(
        StartPublishedRunRequest(RunId("run"), HASH),
        cast(DurablePublishedRunStarter, FakePort(port_result)),
    )

    assert isinstance(result, application_type)


@pytest.mark.parametrize(
    ("port_result", "application_type"),
    [
        (DurableAnswerCreated(ANSWER), AnswerAcceptedPending),
        (DurableAnswerExisting(ANSWER), AnswerExistingPending),
        (DurableAnswerRunMissing(), RunMissing),
        (DurableAnswerNodeMissing(), NodeMissing),
        (DurableAnswerRevisionConflict(), AnswerRevisionConflict),
        (DurableAnswerStateConflict(), AnswerStateConflict),
        (DurableAnswerBytesConflict(), AnswerBytesConflict),
        (DurableWriteUnavailable(), WriteUnavailable),
        (PortDurableStateCorrupt(), DurableStateCorrupt),
    ],
)
def test_answer_maps_every_durable_result(
    port_result: object, application_type: type[object]
) -> None:
    result = answer_wait_result(
        cast(SubmitWaitAnswerRequest, object()),
        cast(TransactionalWaitAnswerer, FakePort(port_result)),
    )

    if isinstance(port_result, DurableAnswerExisting):
        assert isinstance(result, (AnswerExistingPending, AnswerExistingApplied))
    else:
        assert isinstance(result, application_type)


@pytest.mark.parametrize(
    ("port_result", "application_type"),
    [
        (
            DurableReconciliationCreated(
                ReconcileCommandSnapshot(COMMAND, ReconcileCommandState.PENDING)
            ),
            ReconciliationAcceptedPending,
        ),
        (
            DurableReconciliationCreated(
                ReconcileCommandSnapshot(
                    COMMAND, ReconcileCommandState.REJECTED_CONFLICT
                )
            ),
            ReconciliationStale,
        ),
        (
            DurableReconciliationExisting(
                ReconcileCommandSnapshot(COMMAND, ReconcileCommandState.PENDING)
            ),
            ReconciliationExistingPending,
        ),
        (
            DurableReconciliationExisting(
                ReconcileCommandSnapshot(COMMAND, ReconcileCommandState.APPLIED)
            ),
            ReconciliationExistingApplied,
        ),
        (
            DurableReconciliationExisting(
                ReconcileCommandSnapshot(
                    COMMAND, ReconcileCommandState.REJECTED_CONFLICT
                )
            ),
            ReconciliationExistingRejected,
        ),
        (DurableReconciliationTargetMissing(), ReconciliationTargetMissing),
        (DurableReconciliationCommandConflict(), ReconciliationCommandConflict),
        (
            DurableReconciliationDeterminationConflict(),
            ReconciliationDeterminationConflict,
        ),
        (DurableWriteUnavailable(), WriteUnavailable),
        (PortDurableStateCorrupt(), DurableStateCorrupt),
    ],
)
def test_reconciliation_maps_every_durable_result(
    port_result: object, application_type: type[object]
) -> None:
    result = reconcile_effect_result(
        COMMAND, cast(TransactionalEffectReconcileCommander, FakePort(port_result))
    )

    assert isinstance(result, application_type)
