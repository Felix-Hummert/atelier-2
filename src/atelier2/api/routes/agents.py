from __future__ import annotations

from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    require_json_media_dependency,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import ApiProblem
from atelier2.api.projection.agents import (
    agent_configuration_revision_resource,
    auth_profile_revision_resource,
)
from atelier2.api.wire.requests import (
    PublishAgentConfigurationRevisionRequestResource,
    PublishAuthProfileRevisionRequestResource,
)
from atelier2.api.wire.resources import (
    AgentConfigurationRevisionResource,
    AuthProfileRevisionResource,
)
from atelier2.application.publish_agent_configurations import (
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
)
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable

router = APIRouter()


@router.post(
    API_PREFIX + "/auth-profile-revisions",
    response_model=AuthProfileRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": AuthProfileRevisionResource}},
)
async def publish_auth_profile_revision_route(
    body: PublishAuthProfileRevisionRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_auth_profile_revision(
            body.profile_id, body.revision_number, body.provider_id, body.auth_mode
        ),
    )
    match result:
        case AuthProfileRevisionPublished(stored):
            status = HTTPStatus.CREATED
        case AuthProfileRevisionUnchanged(stored):
            status = HTTPStatus.OK
        case UnpublishableAuthProfile():
            raise ApiProblem("invalid-request")
        case AuthProfileRevisionConflict():
            raise ApiProblem("auth-profile-revision-conflict")
        case AuthProfileRevisionCollision():
            raise ApiProblem("auth-profile-revision-collision")
        case WriteUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(auth_profile_revision_resource(stored), status)


@router.post(
    API_PREFIX + "/agent-configuration-revisions",
    response_model=AgentConfigurationRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": AgentConfigurationRevisionResource}},
)
async def publish_agent_configuration_revision_route(
    body: PublishAgentConfigurationRevisionRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_agent_configuration_revision(
            body.model,
            body.auth_profile_revision_hash,
            body.executor_revision,
            body.requested_capability,
        ),
    )
    match result:
        case AgentConfigurationRevisionPublished(stored, auth_profile):
            status = HTTPStatus.CREATED
        case AgentConfigurationRevisionUnchanged(stored, auth_profile):
            status = HTTPStatus.OK
        case UnpublishableAgentConfiguration():
            raise ApiProblem("invalid-request")
        case AuthProfileRevisionNotFound():
            raise ApiProblem("auth-profile-revision-not-found")
        case AgentExecutorBindingUnavailable():
            raise ApiProblem("agent-executor-binding-unavailable")
        case AgentConfigurationRevisionCollision():
            raise ApiProblem("agent-configuration-revision-collision")
        case WriteUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        agent_configuration_revision_resource(stored, auth_profile), status
    )
