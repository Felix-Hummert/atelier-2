from __future__ import annotations

from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    decode_base64,
    decode_public_reference,
    load_run_resource,
    parse_limit,
    require_field,
    require_fields,
    require_json_media_dependency,
    require_new_run_identity,
    require_run_projections,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.limits import ApiLimitExceeded
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import PROJECTION_LIMIT_DETAIL, ApiProblem
from atelier2.api.projection.runs import (
    node_detail_resource,
    run_receipt_resource,
    run_resource,
)
from atelier2.api.references import encode_public_run_reference, parse_revision_hash
from atelier2.api.wire.requests import (
    AnswerWaitRequestResource,
    AnyStartRunOrderResource,
    AnyStartRunRequestResource,
    ArtifactOrderResource,
    CancelAgentAttemptRequestResource,
    InlineOrderResource,
    ReconcileRunRequestResource,
    StartRunRequestResourceV2,
    StartRunRequestResourceV3,
)
from atelier2.api.wire.resources import (
    AnyRunPageResource,
    AnyRunResource,
    InvalidFieldResource,
    NodeDetailResource,
    OperatorFoundDeterminationResource,
    RunReceiptResource,
    VersionedRunPageResource,
)
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    AnswerBytesConflict,
    AnswerExistingApplied,
    AnswerExistingPending,
    AnswerRevisionConflict,
    AnswerStateConflict,
    NodeMissing,
    RunMissing,
    UnanswerableWait,
)
from atelier2.application.cancel_agent_attempt import (
    AttemptAlreadyTerminal,
    AttemptMissing,
    AttemptNotCurrent,
    CancellationAccepted,
    CancellationRunMissing,
    CancellationStale,
    CommandConflict,
    ReplacementNotAllowed,
)
from atelier2.application.read_runs import (
    NodeDetailRead,
    NodeNotFound,
    RunNotFound,
    RunReceiptsRead,
    RunsListed,
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
)
from atelier2.application.reconcile_run import ReconcileRunRequest
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.application.start_published_run import (
    AgentConfigurationRevisionMissing,
    AuthoredAgentBinding,
    AuthoredOrder,
    InvalidAgentBindings,
    RevisionMissing,
    RunCreated,
    RunExisting,
    RunFormatNotExecutable,
    RunIdentityConflict,
    RunInputRefused,
)
from atelier2.application.start_published_run import (
    AgentExecutorBindingUnavailable as StartAgentExecutorBindingUnavailable,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    AgentAttemptReplacement,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.artifacts import ArtifactHash
from atelier2.contracts.effects import (
    EffectId,
    EffectIntentStateVersion,
    EffectResult,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    ReconcileActor,
    ReconcileCommandId,
)
from atelier2.contracts.orders import ArtifactOrderValue, InlineOrderValue
from atelier2.contracts.runs import RunId, RunState

router = APIRouter()


def _authored_order(order: AnyStartRunOrderResource) -> AuthoredOrder:
    """The order one wire shape is, in the vocabulary the start speaks."""
    match order:
        case InlineOrderResource(name=name, value=value):
            return AuthoredOrder(name, InlineOrderValue(value.encode()))
        case ArtifactOrderResource(name=name, artifact_hash=artifact_hash):
            return AuthoredOrder(name, ArtifactOrderValue(ArtifactHash(artifact_hash)))
        case _ as unreachable:
            assert_never(unreachable)


@router.post(
    API_PREFIX + "/runs",
    response_model=AnyRunResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": AnyRunResource}},
)
async def start_run_route(
    body: AnyStartRunRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    run_id = RunId(body.run_id)
    require_new_run_identity(run_id, context.limits)
    revision_hash = parse_revision_hash(body.workflow_revision_hash)
    bindings = None
    orders: tuple[AuthoredOrder, ...] = ()
    if isinstance(body, (StartRunRequestResourceV2, StartRunRequestResourceV3)):
        bindings = tuple(
            AuthoredAgentBinding(
                binding.role, binding.agent_configuration_revision_hash
            )
            for binding in body.agent_bindings
        )
    if isinstance(body, StartRunRequestResourceV3):
        orders = tuple(_authored_order(order) for order in body.orders)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.start_published_run(
            run_id, revision_hash, bindings, orders
        ),
    )
    match result:
        case RunCreated():
            status = HTTPStatus.CREATED
        case RunExisting():
            status = HTTPStatus.OK
        case RevisionMissing():
            raise ApiProblem("workflow-revision-not-found")
        case RunIdentityConflict():
            raise ApiProblem("run-identity-conflict")
        case RunFormatNotExecutable():
            raise ApiProblem("workflow-format-not-executable")
        case RunInputRefused(name, refusal, _detail):
            # The input is named in the detail because it is what an operator
            # fixes; the refusal token says which of the named ways it is wrong.
            raise ApiProblem(
                "run-input-refused", detail=f"input {name!r} was refused: {refusal}"
            )
        case InvalidAgentBindings():
            raise ApiProblem("invalid-agent-bindings")
        case AgentConfigurationRevisionMissing():
            raise ApiProblem("agent-configuration-revision-not-found")
        case StartAgentExecutorBindingUnavailable():
            raise ApiProblem("agent-executor-binding-unavailable")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(await _run_resource_of(run_id, context), status)


@router.get(API_PREFIX + "/runs", response_model=AnyRunPageResource)
async def list_runs(
    after: str | None = None,
    limit: str = "50",
    state: str | None = None,
    context: ApiContext = api_context_dependency,
) -> AnyRunPageResource:
    boundary = None
    if after is not None:
        boundary = decode_public_reference(after, context.limits)
    parsed_limit = parse_limit(limit)
    parsed_state = None
    if state is not None:
        try:
            parsed_state = RunState(state)
        except ValueError:
            raise ApiProblem(
                "invalid-request",
                invalid_fields=(
                    InvalidFieldResource(
                        path="query/state",
                        reason="not a run state this list can filter",
                    ),
                ),
            ) from None
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_runs(boundary, parsed_limit, parsed_state),
    )
    match result:
        case RunsListed(runs, next_after):
            require_run_projections(runs, context.limits)
            return VersionedRunPageResource(
                items=tuple(run_resource(run) for run in runs),
                next_after=(
                    None
                    if next_after is None
                    else encode_public_run_reference(next_after)
                ),
            )
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("temporarily-unavailable", PROJECTION_LIMIT_DETAIL)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.get(API_PREFIX + "/runs/{public_ref}", response_model=AnyRunResource)
async def get_run_route(
    public_ref: str, context: ApiContext = api_context_dependency
) -> AnyRunResource:
    return await _run_resource_of(
        decode_public_reference(public_ref, context.limits), context
    )


@router.get(
    API_PREFIX + "/runs/{public_ref}/nodes/{node_id}",
    response_model=NodeDetailResource,
)
async def get_node_detail_route(
    public_ref: str, node_id: str, context: ApiContext = api_context_dependency
) -> NodeDetailResource:
    """What one node was asked, what it answered, who did it, and what stops it.

    The click into a node an operator makes on the run page, answered as one
    read: a panel that had to stitch this together from the run, the events and
    the receipts would be deriving what the server already knows.
    """

    run_id = decode_public_reference(public_ref, context.limits)
    try:
        context.limits.require_field(node_id)
    except ApiLimitExceeded as error:
        raise ApiProblem("node-not-found") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_node_detail(run_id, node_id),
    )
    match result:
        case NodeDetailRead(detail):
            return node_detail_resource(detail)
        case NodeNotFound():
            raise ApiProblem("node-not-found")
        case RunNotFound():
            raise ApiProblem("run-not-found")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("temporarily-unavailable", PROJECTION_LIMIT_DETAIL)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.get(
    API_PREFIX + "/runs/{public_ref}/receipt",
    response_model=RunReceiptResource,
)
async def get_run_receipt_route(
    public_ref: str, context: ApiContext = api_context_dependency
) -> RunReceiptResource:
    """The agent receipts this run has written.

    One address for the receipts that hang in the terminal-hash chain. A run
    that has not completed an agent yet answers with an empty list. A missing
    run is named, not dressed as empty.
    """

    run_id = decode_public_reference(public_ref, context.limits)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_run_receipts(run_id),
    )
    match result:
        case RunReceiptsRead(items):
            return run_receipt_resource(items)
        case RunNotFound():
            raise ApiProblem("run-not-found")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.post(
    API_PREFIX + "/runs/{public_ref}/agent-attempts/{attempt_id}/cancellations",
    response_model=AnyRunResource,
    status_code=HTTPStatus.ACCEPTED,
    responses={HTTPStatus.OK: {"model": AnyRunResource}},
)
async def cancel_agent_attempt_route(
    public_ref: str,
    attempt_id: str,
    body: CancelAgentAttemptRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    run_id = decode_public_reference(public_ref, context.limits)
    try:
        context.limits.require_field(attempt_id)
        parsed_attempt_id = AgentAttemptId(attempt_id)
        request = CancelAgentAttemptRequest(
            run_id,
            parsed_attempt_id,
            body.command_id,
            body.expected_attempt_state_version,
            AgentAttemptReplacement(body.replacement),
        )
    except (ApiLimitExceeded, TypeError, ValueError) as error:
        raise ApiProblem("invalid-agent-attempt-id") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.cancel_agent_attempt(request),
    )
    match result:
        case CancellationAccepted(terminal=terminal):
            status = HTTPStatus.OK if terminal else HTTPStatus.ACCEPTED
        case CancellationRunMissing():
            raise ApiProblem("run-not-found")
        case AttemptMissing():
            raise ApiProblem("agent-attempt-not-found")
        case AttemptNotCurrent():
            raise ApiProblem("agent-attempt-not-current")
        case CancellationStale():
            raise ApiProblem("agent-attempt-cancellation-stale")
        case AttemptAlreadyTerminal():
            raise ApiProblem("agent-attempt-terminal")
        case CommandConflict():
            raise ApiProblem("cancellation-command-conflict")
        case ReplacementNotAllowed():
            raise ApiProblem("replacement-not-allowed")
        case WriteUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(await _run_resource_of(run_id, context), status)


@router.post(
    API_PREFIX + "/runs/{public_ref}/answers",
    response_model=AnyRunResource,
    status_code=HTTPStatus.ACCEPTED,
    responses={HTTPStatus.OK: {"model": AnyRunResource}},
)
async def answer_run_route(
    public_ref: str,
    body: AnswerWaitRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    run_id = decode_public_reference(public_ref, context.limits)
    require_field(body.node_id, context.limits)
    revision_hash = parse_revision_hash(body.workflow_revision_hash)
    answer_bytes = decode_base64(body.answer_base64, context.limits)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.answer_wait(
            run_id, revision_hash, body.node_id, answer_bytes
        ),
    )
    match result:
        case UnanswerableWait():
            raise ApiProblem("invalid-request")
        case AnswerAcceptedPending() | AnswerExistingPending():
            status = HTTPStatus.ACCEPTED
        case AnswerExistingApplied():
            status = HTTPStatus.OK
        case RunMissing():
            raise ApiProblem("run-not-found")
        case NodeMissing():
            raise ApiProblem("node-not-found")
        case AnswerRevisionConflict():
            raise ApiProblem("answer-revision-conflict")
        case AnswerStateConflict():
            raise ApiProblem("answer-state-conflict")
        case AnswerBytesConflict():
            raise ApiProblem("answer-bytes-conflict")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(await _run_resource_of(run_id, context), status)


@router.post(
    API_PREFIX + "/runs/{public_ref}/reconciliations",
    response_model=AnyRunResource,
    status_code=HTTPStatus.ACCEPTED,
    responses={HTTPStatus.OK: {"model": AnyRunResource}},
)
async def reconcile_run_route(
    public_ref: str,
    body: ReconcileRunRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    run_id = decode_public_reference(public_ref, context.limits)
    require_fields(
        context.limits,
        body.command_id,
        body.actor,
        body.evidence,
    )
    determination_body = body.determination
    if isinstance(determination_body, OperatorFoundDeterminationResource):
        require_field(determination_body.effect_id, context.limits)
        determination = OperatorFoundEffect(
            EffectId(determination_body.effect_id),
            EffectResult(
                decode_base64(determination_body.result_base64, context.limits)
            ),
        )
    else:
        determination = OperatorAuthoritativeAbsence()
    reconciliation_request = ReconcileRunRequest(
        run_id,
        ReconcileCommandId(body.command_id),
        EffectIntentStateVersion(body.expected_intent_state_version),
        ReconcileActor(body.actor),
        body.evidence,
        determination,
    )
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.reconcile_run(reconciliation_request),
    )
    match result:
        case ReconciliationAcceptedPending() | ReconciliationExistingPending():
            status = HTTPStatus.ACCEPTED
        case ReconciliationExistingApplied():
            status = HTTPStatus.OK
        case RunMissing():
            raise ApiProblem("run-not-found")
        case ReconciliationTargetMissing():
            raise ApiProblem("reconciliation-target-missing")
        case ReconciliationStale():
            raise ApiProblem("reconciliation-stale")
        case ReconciliationCommandConflict():
            raise ApiProblem("reconciliation-command-conflict")
        case ReconciliationDeterminationConflict():
            raise ApiProblem("reconciliation-determination-conflict")
        case ReconciliationExistingRejected():
            raise ApiProblem("reconciliation-rejected")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("temporarily-unavailable", PROJECTION_LIMIT_DETAIL)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(await _run_resource_of(run_id, context), status)


async def _run_resource_of(run_id: RunId, context: ApiContext) -> AnyRunResource:
    return await load_run_resource(
        run_id,
        context.use_cases.get_run,
        context.control_runner,
        context.limits,
    )
