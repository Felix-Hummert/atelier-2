"""What each write use-case decides, one case per outcome.

These carried no `proves` mark while the cancellation was still a pass-through:
the sentence they would have claimed -- every write decides through a use-case
that owns the store's answer -- was not true of this tree, and a mark would have
made the gate agree with a sentence the code did not keep. The cancellation is
translated now and its cases stand below with the rest, so the mark joins them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from atelier2.application.answer_wait import UnanswerableWait, answer_wait_result
from atelier2.application.cancel_agent_attempt import (
    AttemptAlreadyTerminal,
    AttemptMissing,
    AttemptNotCurrent,
    CancellationAccepted,
    CancellationRunMissing,
    CancellationStale,
    CommandConflict,
    ReplacementNotAllowed,
    cancel_agent_attempt,
)
from atelier2.application.publish_agent_configurations import (
    PUBLISHED_CONFIGURATION_FORMAT,
    AgentConfigurationRevisionCollision,
    AgentConfigurationRevisionPublished,
    AgentConfigurationRevisionUnchanged,
    AgentExecutorBindingUnavailable,
    AuthProfileRevisionCollision,
    AuthProfileRevisionConflict,
    AuthProfileRevisionNotFound,
    AuthProfileRevisionPublished,
    AuthProfileRevisionUnchanged,
    UnpublishableAgentConfiguration,
    UnpublishableAuthProfile,
    publish_agent_configuration_revision,
    publish_auth_profile_revision,
)
from atelier2.application.publish_budget_revision import (
    BudgetPublicationCollision,
    BudgetPublicationCreated,
    BudgetPublicationExisting,
    BudgetPublicationInvalid,
    publish_budget_revision,
)
from atelier2.application.publish_schema_revision import (
    SchemaPublicationCollision,
    SchemaPublicationCreated,
    SchemaPublicationExisting,
    SchemaPublicationInvalid,
    publish_schema_revision,
)
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.application.start_published_run import (
    AuthoredAgentBinding,
    AuthoredOrder,
    InvalidAgentBindings,
    RunCreated,
    start_published_run,
)
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellation,
    AgentAttemptId,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionRequestHash,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.budgets_v3 import BudgetRevisionRefusal
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.orders import InlineOrderValue
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import SchemaDocumentRefusal
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted as DurableCancellationAccepted,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationCommandConflict as DurableCommandConflict,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationNotCurrent as DurableNotCurrent,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationRunMissing as DurableRunMissing,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationStale as DurableStale,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationTargetMissing as DurableTargetMissing,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationTerminalConflict as DurableTerminalConflict,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptReplacementNotAllowed as DurableReplacementNotAllowed,
)
from atelier2.ports.agent_attempts import (
    DurableWriteUnavailable as PortDurableWriteUnavailable,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCollision as PortConfigurationCollision,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated as PortConfigurationCreated,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionExisting as PortConfigurationExisting,
)
from atelier2.ports.agent_configurations import (
    AgentExecutorBindingUnavailable as PortExecutorBindingUnavailable,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionCollision as PortProfileCollision,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionConflict as PortProfileConflict,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionCreated as PortProfileCreated,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionExisting as PortProfileExisting,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionMissing as PortProfileMissing,
)
from atelier2.ports.durable_runs import (
    DurableAnswerCreated,
    DurableAnswerNotAdmitted,
    DurableRunCreated,
    DurableWriteUnavailable,
    StartPublishedRunRequestV3,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCollision,
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)

REVISION_HASH = WorkflowRevisionHash("a" * 64)
RUN_ID = RunId("run")
STORED: Any = object()
AUTH_PROFILE: Any = object()
RUN: Any = object()
SNAPSHOT: Any = object()


class ScriptedCatalog:
    """A catalog that answers each publication with the one answer a case scripts."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.published: list[Any] = []

    def _record(self, revision: Any) -> Any:
        self.published.append(revision)
        return self.answer

    def publish_auth_profile_revision(self, revision: Any) -> Any:
        return self._record(revision)

    def publish_agent_configuration_revision(self, revision: Any) -> Any:
        return self._record(revision)

    def auth_profile_revision(self, revision_hash: Any) -> Any:
        raise AssertionError("a publication under test read the catalog back")

    def agent_configuration_revision(self, revision_hash: Any) -> Any:
        raise AssertionError("a publication under test read the catalog back")

    def list_agent_configuration_revisions(self, after: Any, limit: int) -> Any:
        raise AssertionError("a publication under test listed the catalog")

    def list_auth_profile_revisions(self, after: Any, limit: int) -> Any:
        raise AssertionError("a publication under test listed the catalog")


class ScriptedStarter:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.started: list[Any] = []

    def start_published(self, request: Any) -> Any:
        self.started.append(request)
        return self.answer


WRITE_REFUSALS = [
    (DurableWriteUnavailable(), WriteUnavailable()),
    (PortDurableStateCorrupt(), DurableStateCorrupt()),
]


def publish_profile(catalog: ScriptedCatalog) -> object:
    return publish_auth_profile_revision(
        "profile", 1, "anthropic", "subscription", catalog
    )


def publish_configuration(catalog: ScriptedCatalog) -> object:
    return publish_agent_configuration_revision(
        "claude", "b" * 64, "executor@1", "headless", catalog
    )


PUBLICATIONS: list[
    tuple[str, Callable[[ScriptedCatalog], object], list[tuple[Any, Any]]]
] = [
    (
        "auth-profile",
        publish_profile,
        [
            (PortProfileCreated(STORED), AuthProfileRevisionPublished(STORED)),
            (PortProfileExisting(STORED), AuthProfileRevisionUnchanged(STORED)),
            (PortProfileConflict(), AuthProfileRevisionConflict()),
            (PortProfileCollision(), AuthProfileRevisionCollision()),
            *WRITE_REFUSALS,
        ],
    ),
    (
        "agent-configuration",
        publish_configuration,
        [
            (
                PortConfigurationCreated(STORED, AUTH_PROFILE),
                AgentConfigurationRevisionPublished(STORED, AUTH_PROFILE),
            ),
            (
                PortConfigurationExisting(STORED, AUTH_PROFILE),
                AgentConfigurationRevisionUnchanged(STORED, AUTH_PROFILE),
            ),
            (PortProfileMissing(), AuthProfileRevisionNotFound()),
            (PortExecutorBindingUnavailable(), AgentExecutorBindingUnavailable()),
            (PortConfigurationCollision(), AgentConfigurationRevisionCollision()),
            *WRITE_REFUSALS,
        ],
    ),
]


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
@pytest.mark.parametrize(
    ("publish", "port_answer", "expected"),
    [
        pytest.param(
            publish, port_answer, expected, id=f"{name}-{type(port_answer).__name__}"
        )
        for name, publish, outcomes in PUBLICATIONS
        for port_answer, expected in outcomes
    ],
)
def test_every_port_answer_of_a_publication_becomes_this_layers_own_outcome(
    publish: Callable[[ScriptedCatalog], object], port_answer: Any, expected: Any
) -> None:
    catalog = ScriptedCatalog(port_answer)

    assert publish(catalog) == expected
    assert len(catalog.published) == 1


@pytest.mark.parametrize(
    ("publish", "authored"),
    [
        (publish_auth_profile_revision, ("profile", 1, "anthropic", "not-a-mode")),
        (publish_agent_configuration_revision, ("claude", "short", "e@1", "headless")),
    ],
    ids=["unknown-auth-mode", "malformed-auth-profile-hash"],
)
def test_authored_values_that_make_no_revision_refuse_before_the_catalog_is_asked(
    publish: Callable[..., Any], authored: tuple[Any, ...]
) -> None:
    """The construction is the use-case's, so its failure is an outcome, not an
    exception a caller above has to know to catch — and nothing is published."""
    catalog = ScriptedCatalog(PortProfileCreated(STORED))

    result = publish(*authored, catalog)

    assert isinstance(
        result, (UnpublishableAuthProfile, UnpublishableAgentConfiguration)
    )
    assert catalog.published == []


SCHEMA_DOCUMENT = b'{"type": "object"}'
SCHEMA_REVISION = PublishedRevision(RevisionKind.SCHEMA, SCHEMA_DOCUMENT)
BUDGET_DOCUMENT = b'{"attempt_deadline_seconds": 900}'
BUDGET_REVISION = PublishedRevision(RevisionKind.BUDGET_POLICY, BUDGET_DOCUMENT)


class ScriptedRegistry:
    """A published-revision registry that answers with the one scripted result."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.published: list[PublishedRevision] = []

    def publish_revision(self, revision: PublishedRevision) -> Any:
        self.published.append(revision)
        return self.answer

    def resolve(self, kind: object, revision_hash: object) -> Any:
        del kind, revision_hash
        raise AssertionError("schema publication never resolves")


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
@pytest.mark.parametrize(
    ("port_answer", "expected"),
    [
        (
            PublishedRevisionCreated(SCHEMA_REVISION),
            SchemaPublicationCreated(SCHEMA_REVISION),
        ),
        (
            PublishedRevisionExisting(SCHEMA_REVISION),
            SchemaPublicationExisting(SCHEMA_REVISION),
        ),
        (PublishedRevisionCollision(), SchemaPublicationCollision()),
        *WRITE_REFUSALS,
    ],
    ids=lambda value: type(value).__name__,
)
def test_every_port_answer_of_a_schema_publication_becomes_this_layers_own_outcome(
    port_answer: Any, expected: Any
) -> None:
    registry = ScriptedRegistry(port_answer)

    assert publish_schema_revision(SCHEMA_DOCUMENT, registry) == expected
    assert registry.published == [SCHEMA_REVISION]


def test_a_schema_outside_the_profile_is_refused_before_the_store_is_asked() -> None:
    registry = ScriptedRegistry(PublishedRevisionCreated(SCHEMA_REVISION))

    result = publish_schema_revision(b"Guten Morgen", registry)

    assert isinstance(result, SchemaPublicationInvalid)
    assert result.verdict.refusal is SchemaDocumentRefusal.DOCUMENT_NOT_JSON
    assert registry.published == []


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
@pytest.mark.parametrize(
    ("port_answer", "expected"),
    [
        (
            PublishedRevisionCreated(BUDGET_REVISION),
            BudgetPublicationCreated(BUDGET_REVISION),
        ),
        (
            PublishedRevisionExisting(BUDGET_REVISION),
            BudgetPublicationExisting(BUDGET_REVISION),
        ),
        (PublishedRevisionCollision(), BudgetPublicationCollision()),
        *WRITE_REFUSALS,
    ],
    ids=lambda value: type(value).__name__,
)
def test_every_port_answer_of_a_budget_publication_becomes_this_layers_own_outcome(
    port_answer: Any, expected: Any
) -> None:
    registry = ScriptedRegistry(port_answer)

    assert publish_budget_revision(BUDGET_DOCUMENT, registry) == expected
    assert registry.published == [BUDGET_REVISION]


@pytest.mark.proves("a-published-budget-bounds-an-attempt-or-is-refused-by-name")
def test_a_budget_bounding_nothing_is_refused_before_the_store_is_asked() -> None:
    registry = ScriptedRegistry(PublishedRevisionCreated(BUDGET_REVISION))

    result = publish_budget_revision(b'{"maximum_assistant_turns": 8}', registry)

    assert isinstance(result, BudgetPublicationInvalid)
    assert result.verdict.reason is BudgetRevisionRefusal.MISSING_ATTEMPT_DEADLINE
    assert registry.published == []


def test_a_configuration_is_recorded_under_the_format_this_publication_decides() -> (
    None
):
    """No caller chooses the format, so the use-case owns it rather than a route."""
    catalog = ScriptedCatalog(PortConfigurationCreated(STORED, AUTH_PROFILE))

    publish_configuration(catalog)

    assert (
        catalog.published[0].revision_format_version is PUBLISHED_CONFIGURATION_FORMAT
    )
    assert PUBLISHED_CONFIGURATION_FORMAT is AgentConfigurationRevisionFormatVersion.V2


def test_a_start_that_binds_no_agent_asks_for_the_run_without_a_binding_set() -> None:
    starter = ScriptedStarter(DurableRunCreated(RUN))

    assert start_published_run(RUN_ID, REVISION_HASH, None, starter) == RunCreated(RUN)
    assert not hasattr(starter.started[0], "agent_bindings")


def test_a_start_whose_authored_binding_is_no_binding_refuses_before_the_store() -> (
    None
):
    """Building the durable request is part of the decision, so a role that is not
    one refuses the start in the same vocabulary as everything else — and the store
    is never asked."""
    starter = ScriptedStarter(DurableRunCreated(RUN))

    result = start_published_run(
        RUN_ID, REVISION_HASH, (AuthoredAgentBinding("", "c" * 64),), starter
    )

    assert result == InvalidAgentBindings()
    assert starter.started == []


def test_a_start_that_binds_an_agent_carries_the_authored_roles_to_the_store() -> None:
    starter = ScriptedStarter(DurableRunCreated(RUN))

    start_published_run(
        RUN_ID,
        REVISION_HASH,
        (AuthoredAgentBinding("builder", "c" * 64),),
        starter,
    )

    bound = starter.started[0].agent_bindings
    assert [binding.role.value for binding in bound.bindings] == ["builder"]


def test_a_start_that_carries_orders_asks_for_the_v3_shape_without_a_schema_hash() -> (
    None
):
    """The caller names the material. The start pins the schema the document named."""
    starter = ScriptedStarter(DurableRunCreated(RUN))

    start_published_run(
        RUN_ID,
        REVISION_HASH,
        (AuthoredAgentBinding("builder", "c" * 64),),
        starter,
        (AuthoredOrder("order", InlineOrderValue(b'{"portions": 4}')),),
    )

    requested = starter.started[0]
    assert isinstance(requested, StartPublishedRunRequestV3)
    assert requested.run_inputs == ()
    assert [(order.name, order.value) for order in requested.orders] == [
        ("order", InlineOrderValue(b'{"portions": 4}'))
    ]


class ScriptedAnswerer:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.submitted: list[Any] = []

    def submit_result(self, request: Any) -> Any:
        self.submitted.append(request)
        return self.answer


def test_an_answer_that_makes_no_submission_refuses_before_the_store_is_asked() -> None:
    """Building the submission belongs to the decision, so a value that makes none
    is an outcome of it — and the store is never asked."""
    answerer = ScriptedAnswerer(DurableAnswerCreated(SNAPSHOT))

    result = answer_wait_result(RUN_ID, REVISION_HASH, "", b"6", answerer)

    assert isinstance(result, UnanswerableWait)
    assert answerer.submitted == []


def test_bytes_the_waiting_node_does_not_admit_read_back_as_an_unanswerable_wait() -> (
    None
):
    """Which vocabulary the node declares is the store's to know, not this layer's.

    A V1 wait admits canonical integer text and a V3 wait admits what its own
    schema admits, so the bytes are carried to the store and its refusal is what
    decides. The caller is told the same thing either way: this was no answer.
    """
    answerer = ScriptedAnswerer(DurableAnswerNotAdmitted("not what this node admits"))

    result = answer_wait_result(RUN_ID, REVISION_HASH, "waiting", b"06", answerer)

    assert isinstance(result, UnanswerableWait)
    assert [submitted.answer_bytes for submitted in answerer.submitted] == [b"06"]


def test_an_answer_carries_the_authored_values_into_the_submission() -> None:
    answerer = ScriptedAnswerer(DurableAnswerCreated(SNAPSHOT))

    answer_wait_result(RUN_ID, REVISION_HASH, "waiting", b"6", answerer)

    submitted = answerer.submitted[0]
    assert (submitted.run_id, submitted.node_id, submitted.answer_bytes) == (
        RUN_ID,
        "waiting",
        b"6",
    )


class ScriptedCanceller:
    """One canceller that answers with exactly what it was scripted to say."""

    def __init__(self, answer: Any) -> None:
        self._answer = answer
        self.asked: list[CancelAgentAttemptRequest] = []

    def request_cancellation(self, request: CancelAgentAttemptRequest) -> Any:
        self.asked.append(request)
        return self._answer


# Derived rather than invented: the attempt id is bound to its execution and
# request, so a made-up one is refused before any use-case is reached.
CANCELLED_EXECUTION = NodeExecutionId("b" * 64)
CANCELLED_REQUEST_HASH = AgentExecutionRequestHash("c" * 64)
CANCELLED_ATTEMPT_ID = AgentAttemptId.for_execution(
    CANCELLED_EXECUTION, CANCELLED_REQUEST_HASH
)
CANCELLATION_REQUEST = CancelAgentAttemptRequest(
    RunId("run/cancel"),
    CANCELLED_ATTEMPT_ID,
    "command-1",
    1,
    AgentAttemptReplacement.NONE,
)
CANCELLED_ATTEMPT = AgentAttempt(
    CANCELLED_ATTEMPT_ID,
    CANCELLED_EXECUTION,
    CANCELLED_REQUEST_HASH,
    AgentExecutorOperationalIdentity("exact-operation"),
    RunId("run/cancel"),
    WorkflowRevisionHash("d" * 64),
    "implement",
    1,
    AgentAttemptState.CANCEL_REQUESTED,
    1,
    cancellation=AgentAttemptCancellation("command-1", 1, AgentAttemptReplacement.NONE),
)


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
@pytest.mark.parametrize(
    ("port_answer", "expected"),
    [
        pytest.param(
            DurableCancellationAccepted(CANCELLED_ATTEMPT, False),
            CancellationAccepted(CANCELLED_ATTEMPT, False),
            id="accepted",
        ),
        pytest.param(DurableRunMissing(), CancellationRunMissing(), id="run-missing"),
        pytest.param(DurableTargetMissing(), AttemptMissing(), id="attempt-missing"),
        pytest.param(DurableNotCurrent(), AttemptNotCurrent(), id="not-current"),
        pytest.param(DurableStale(), CancellationStale(), id="stale"),
        pytest.param(
            DurableTerminalConflict(), AttemptAlreadyTerminal(), id="already-terminal"
        ),
        pytest.param(
            DurableCommandConflict(), CommandConflict(), id="command-conflict"
        ),
        pytest.param(
            DurableReplacementNotAllowed(),
            ReplacementNotAllowed(),
            id="replacement-not-allowed",
        ),
        pytest.param(
            PortDurableWriteUnavailable(), WriteUnavailable(), id="write-unavailable"
        ),
        pytest.param(
            PortDurableStateCorrupt(), DurableStateCorrupt(), id="state-corrupt"
        ),
    ],
)
def test_every_port_answer_of_a_cancellation_becomes_this_layers_own_outcome(
    port_answer: Any, expected: Any
) -> None:
    """The last write to be translated, answered in this layer's words.

    It handed the store's union straight to the route until now, which is what
    made the application layer a corridor for this one call: the route read the
    store's vocabulary and the record that binds use cases named a port type.
    """
    canceller = ScriptedCanceller(port_answer)

    assert cancel_agent_attempt(CANCELLATION_REQUEST, canceller) == expected
    assert canceller.asked == [CANCELLATION_REQUEST]
