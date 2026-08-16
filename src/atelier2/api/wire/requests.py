"""The schemas the API accepts: publications, run starts, answers, commands."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from atelier2.api.references import (
    MAX_SIGNED_INT64,
    REVISION_HASH_PATTERN,
    SHA256_HASH_PATTERN,
)
from atelier2.api.wire.resources import ApiModel, ReconciliationDeterminationResource


class RevisionListingView(StrEnum):
    """Which representation of a revision listing the caller asked for.

    Two representations exist because they cost differently: the summary reads
    one column per revision, while the described one reads and parses every
    document it lists. Making the caller name the one it needs keeps the cheaper
    answer the default and keeps both of them reachable, rather than serving one
    shape the document still advertises the other for.
    """

    SUMMARY = "summary"
    DESCRIBED = "described"


class PublishAuthProfileRevisionRequestResource(ApiModel):
    profile_id: str = Field(min_length=1, max_length=1_024)
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    provider_id: str = Field(min_length=1, max_length=64)
    auth_mode: Literal["subscription", "api_key"]


class PublishAgentConfigurationRevisionRequestResource(ApiModel):
    model: str = Field(min_length=1, max_length=1_024)
    auth_profile_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    executor_revision: str = Field(min_length=1, max_length=1_024)
    requested_capability: Literal["headless", "interactive"] = "headless"


class StartRunRequestResource(ApiModel):
    run_id: str = Field(min_length=1)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class StartRunAgentBindingResourceV2(ApiModel):
    role: str = Field(min_length=1, max_length=1_024)
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class StartRunRequestResourceV2(ApiModel):
    workflow_format_version: Literal[2]
    run_id: str = Field(min_length=1)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    agent_bindings: tuple[StartRunAgentBindingResourceV2, ...] = Field(
        max_length=100, strict=False
    )


AnyStartRunRequestResource = StartRunRequestResource | StartRunRequestResourceV2


class AnswerWaitRequestResource(ApiModel):
    revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    node_id: str = Field(min_length=1)
    answer_base64: str = Field(min_length=1)


class ReconcileRunRequestResource(ApiModel):
    command_id: str = Field(min_length=1)
    expected_intent_state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    actor: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    determination: ReconciliationDeterminationResource


class CancelAgentAttemptRequestResource(ApiModel):
    command_id: str = Field(min_length=1, max_length=1_024)
    expected_attempt_state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    replacement: Literal["NONE", "ONE"]
