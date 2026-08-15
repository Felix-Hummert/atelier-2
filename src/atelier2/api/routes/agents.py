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
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    AuthProfileRevisionHash,
    ProviderId,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCollision,
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionExisting,
    AgentExecutorBindingUnavailable,
    AuthProfileRevisionCollision,
    AuthProfileRevisionConflict,
    AuthProfileRevisionCreated,
    AuthProfileRevisionExisting,
    AuthProfileRevisionMissing,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable

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
    try:
        revision = AuthProfileRevision(
            body.profile_id,
            body.revision_number,
            ProviderId(body.provider_id),
            AuthMode(body.auth_mode),
        )
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error
    catalog = context.ports.agent_configuration_catalog
    result = await run_control_query(
        context.control_runner,
        lambda: catalog.publish_auth_profile_revision(revision),
    )
    match result:
        case AuthProfileRevisionCreated(stored):
            status = HTTPStatus.CREATED
        case AuthProfileRevisionExisting(stored):
            status = HTTPStatus.OK
        case AuthProfileRevisionConflict():
            raise ApiProblem("auth-profile-revision-conflict")
        case AuthProfileRevisionCollision():
            raise ApiProblem("auth-profile-revision-collision")
        case DurableWriteUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case PortDurableStateCorrupt():
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
    try:
        revision = AgentConfigurationRevision(
            body.model,
            AuthProfileRevisionHash(body.auth_profile_revision_hash),
            AgentExecutorRevision(body.executor_revision),
            AgentExecutionCapability(body.requested_capability),
            AgentConfigurationRevisionFormatVersion.V2,
        )
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error
    catalog = context.ports.agent_configuration_catalog
    result = await run_control_query(
        context.control_runner,
        lambda: catalog.publish_agent_configuration_revision(revision),
    )
    match result:
        case AgentConfigurationRevisionCreated(stored, auth_profile):
            status = HTTPStatus.CREATED
        case AgentConfigurationRevisionExisting(stored, auth_profile):
            status = HTTPStatus.OK
        case AuthProfileRevisionMissing():
            raise ApiProblem("auth-profile-revision-not-found")
        case AgentExecutorBindingUnavailable():
            raise ApiProblem("agent-executor-binding-unavailable")
        case AgentConfigurationRevisionCollision():
            raise ApiProblem("agent-configuration-revision-collision")
        case DurableWriteUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case PortDurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        agent_configuration_revision_resource(stored, auth_profile), status
    )
