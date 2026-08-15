from __future__ import annotations

from fastapi import APIRouter

from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.models import HealthResource
from atelier2.api.openapi import API_PREFIX

router = APIRouter()


@router.get(API_PREFIX + "/health", response_model=HealthResource)
async def health(context: ApiContext = api_context_dependency) -> HealthResource:
    return HealthResource(
        status="serving",
        source_commit=context.source_commit,
        source_tree=context.source_tree,
    )
