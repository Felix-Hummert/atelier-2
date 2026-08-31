"""What the library recognises a loose document as, before anything is written.

One operator gives the library *something* -- a file, with or without its name --
and the library says what it is (ADR 0018 §2: the intake reads only the
selection marker and the kind's own parser). Recognition is write-free: this
contract carries no revision hash, because nothing was published.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from atelier2.contracts.agents import ProviderId
from atelier2.contracts.workflow_formats import WorkflowFormatVersion


class LibraryDocumentKind(StrEnum):
    """Every building block ADR 0018 §2 gives its own library entry."""

    WORKFLOW = "workflow"
    AGENT_DEFINITION = "agent_definition"
    SKILL = "skill"
    MCP_SERVER = "mcp_server"


type NotHeldKind = Literal[LibraryDocumentKind.SKILL, LibraryDocumentKind.MCP_SERVER]
"""The kinds the library recognises but has no store for today."""

SKILL_DOCUMENT_FILE_NAME = "SKILL.md"
MCP_DECLARATION_FILE_NAME = ".mcp.json"
MCP_SERVERS_KEY = "mcpServers"

AGENT_DEFINITION_PROVIDER = ProviderId("anthropic")
"""The provider mark every authored agent definition carries (ADR 0018 §2).

The Markdown frontmatter form the parser reads is Claude Code's own; an agent
file is provider-bound and is shown with that provider, never translated.
"""

EXPECTED_DOCUMENT_FORMS: Mapping[LibraryDocumentKind, str] = {
    LibraryDocumentKind.WORKFLOW: (
        "a YAML workflow document declaring `format_version`"
    ),
    LibraryDocumentKind.AGENT_DEFINITION: (
        "a Markdown agent definition with `name` and `description` frontmatter"
    ),
    LibraryDocumentKind.SKILL: (
        f"a file named `{SKILL_DOCUMENT_FILE_NAME}` with a closed frontmatter block"
    ),
    LibraryDocumentKind.MCP_SERVER: (
        f"a `{MCP_DECLARATION_FILE_NAME}` JSON object holding `{MCP_SERVERS_KEY}`"
    ),
}
"""What each marker looks for, in the words an unrecognised document is told."""

NOT_HELD_REASONS: Mapping[NotHeldKind, str] = {
    LibraryDocumentKind.SKILL: (
        "skills are recognised but not yet held: no executor loads one, so the "
        "library has no store to keep it in"
    ),
    LibraryDocumentKind.MCP_SERVER: (
        "MCP server declarations are recognised but not yet held: starting a "
        "process is a trust boundary and no blessing exists to cross it"
    ),
}
"""Why a recognised kind stays outside the library today (ADR 0018 §2 table)."""


@dataclass(frozen=True)
class RecognizedWorkflow:
    """The bytes are one workflow document, in the one format the parser admits."""

    format_version: Literal[WorkflowFormatVersion.V3]
    name: str
    description: str | None


@dataclass(frozen=True)
class RecognizedAgentDefinition:
    name: str
    description: str
    provider: ProviderId


@dataclass(frozen=True)
class DocumentNotHeld:
    """A kind the library recognises but has no store for, and says why."""

    kind: NotHeldKind
    reason: str


@dataclass(frozen=True)
class KindRefusal:
    """One marker's honest answer: what it expected and why these bytes are not it."""

    kind: LibraryDocumentKind
    expected: str
    refused_because: str


@dataclass(frozen=True)
class DocumentUnrecognized:
    """No marker matched; each names what it looked for."""

    refusals: tuple[KindRefusal, ...]

    def __post_init__(self) -> None:
        if {refusal.kind for refusal in self.refusals} != set(LibraryDocumentKind):
            raise ValueError("an unrecognised document names every kind it is not")


@dataclass(frozen=True)
class DocumentAmbiguous:
    """More than one marker claimed the bytes; naming one would be a guess."""

    kinds: tuple[LibraryDocumentKind, ...]

    def __post_init__(self) -> None:
        if len(self.kinds) < 2 or len(set(self.kinds)) != len(self.kinds):
            raise ValueError("an ambiguous document names two or more distinct kinds")
