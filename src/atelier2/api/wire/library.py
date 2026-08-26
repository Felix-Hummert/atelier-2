"""The schemas the library answers a recognition and an addition with."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from atelier2.api.references import REVISION_HASH_PATTERN, SHA256_HASH_PATTERN
from atelier2.api.wire.resources import ApiModel
from atelier2.contracts.agents import MAXIMUM_PROVIDER_ID_CHARACTERS
from atelier2.contracts.catalog_v3 import MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS


class RecognizedWorkflowResource(ApiModel):
    """The bytes are one workflow; a format older than 3 authors no name."""

    outcome: Literal["workflow"]
    workflow_format_version: Literal[1, 2, 3]
    name: str | None
    description: str | None


class RecognizedAgentDefinitionResource(ApiModel):
    """The bytes are one agent definition, shown with its provider mark."""

    outcome: Literal["agent_definition"]
    name: str
    description: str
    provider_id: str = Field(max_length=MAXIMUM_PROVIDER_ID_CHARACTERS)


class DocumentNotHeldResource(ApiModel):
    """A kind the library recognises but keeps no store for, and why."""

    outcome: Literal["not_held"]
    kind: Literal["skill", "mcp_server"]
    reason: str


class KindRefusalResource(ApiModel):
    kind: Literal["workflow", "agent_definition", "skill", "mcp_server"]
    expected: str
    refused_because: str


class DocumentUnrecognizedResource(ApiModel):
    """No marker matched; each names what it looked for and what it found."""

    outcome: Literal["unrecognized"]
    refusals: tuple[KindRefusalResource, ...]


LibraryRecognitionResource = Annotated[
    RecognizedWorkflowResource
    | RecognizedAgentDefinitionResource
    | DocumentNotHeldResource
    | DocumentUnrecognizedResource,
    Field(discriminator="outcome"),
]


class WorkflowInLibraryResource(ApiModel):
    """The catalog entry an added workflow document now has."""

    kind: Literal["workflow"]
    name: str = Field(min_length=1, max_length=MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS)
    description: str | None
    lineage_id: str = Field(pattern=SHA256_HASH_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    revision_number: int = Field(ge=1)


class AgentDefinitionInLibraryResource(ApiModel):
    """The library entry an added agent definition now has.

    An agent has no lineage: the library lists the published kind itself, so the
    entry is the revision and the name its own frontmatter authored.
    """

    kind: Literal["agent_definition"]
    name: str
    description: str
    provider_id: str = Field(max_length=MAXIMUM_PROVIDER_ID_CHARACTERS)
    agent_definition_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


LibraryAdditionResource = Annotated[
    WorkflowInLibraryResource | AgentDefinitionInLibraryResource,
    Field(discriminator="kind"),
]
