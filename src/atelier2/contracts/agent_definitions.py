from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthProfileRevisionHash,
)
from atelier2.contracts.hashing import Sha256Hash, frame


class AgentDefinitionField(StrEnum):
    """Every frontmatter key an authored agent definition may carry.

    The set is closed: a key outside it is refused by its own name rather than
    guessed at, so nothing an author writes is silently dropped.
    """

    NAME = "name"
    DESCRIPTION = "description"
    MODEL = "model"
    TOOLS = "tools"


REQUIRED_AGENT_DEFINITION_FIELDS = (
    AgentDefinitionField.NAME,
    AgentDefinitionField.DESCRIPTION,
)


class AgentDefinitionRefusal(StrEnum):
    """Every named way an authored agent definition is refused."""

    DOCUMENT_NOT_UTF8 = "document-not-utf8"
    FRONTMATTER_MISSING = "frontmatter-missing"
    FRONTMATTER_UNTERMINATED = "frontmatter-unterminated"
    FRONTMATTER_UNPARSABLE = "frontmatter-unparsable"
    FRONTMATTER_NOT_A_MAPPING = "frontmatter-not-a-mapping"
    FIELD_UNKNOWN = "field-unknown"
    FIELD_MISSING = "field-missing"
    FIELD_DUPLICATED = "field-duplicated"
    FIELD_TYPE_UNEXPECTED = "field-type-unexpected"
    FIELD_EMPTY = "field-empty"
    TOOL_DUPLICATED = "tool-duplicated"
    SYSTEM_PROMPT_MISSING = "system-prompt-missing"


class AgentDefinitionRefused(ValueError):
    """One named refusal of an authored agent definition.

    The subject is the exact key, field, or value the refusal is about, and is
    absent only when the refusal is about the document as a whole.
    """

    def __init__(
        self, refusal: AgentDefinitionRefusal, subject: str | None = None
    ) -> None:
        super().__init__(
            refusal.value if subject is None else f"{refusal.value}: {subject}"
        )
        self.refusal = refusal
        self.subject = subject


class AgentDefinitionHash(Sha256Hash):
    """Identity of one exact authored agent definition."""


def _require_authored_text(value: str, field_name: AgentDefinitionField) -> None:
    if not isinstance(value, str):
        raise AgentDefinitionRefused(
            AgentDefinitionRefusal.FIELD_TYPE_UNEXPECTED, field_name.value
        )
    if not value:
        raise AgentDefinitionRefused(
            AgentDefinitionRefusal.FIELD_EMPTY, field_name.value
        )


@dataclass(frozen=True)
class AgentToolName:
    """One tool name exactly as its author spelled it.

    The name stays opaque here: this contract owns that a tool was declared,
    not what any tool means. Binding a name to an executor capability is the
    tool executor's decision and does not exist yet, so no registry of known
    names is invented in its place.
    """

    value: str

    def __post_init__(self) -> None:
        _require_authored_text(self.value, AgentDefinitionField.TOOLS)


@dataclass(frozen=True)
class UnrestrictedTools:
    """No `tools` field: the agent may use every tool its executor offers."""


@dataclass(frozen=True)
class DeclaredTools:
    """A `tools` field: exactly these tools and no other.

    Tools are a set, so the order an author types them in is not part of the
    declaration and cannot change the definition's identity.
    """

    names: tuple[AgentToolName, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.names, key=lambda name: name.value.encode("utf-8")))
        seen: set[str] = set()
        for name in ordered:
            if name.value in seen:
                raise AgentDefinitionRefused(
                    AgentDefinitionRefusal.TOOL_DUPLICATED, name.value
                )
            seen.add(name.value)
        object.__setattr__(self, "names", ordered)


type AgentToolDeclaration = UnrestrictedTools | DeclaredTools


def _tool_declaration_frame(tools: AgentToolDeclaration) -> bytes:
    if isinstance(tools, UnrestrictedTools):
        return frame("agent-tool-declaration/unrestricted/v1")
    return frame(
        "agent-tool-declaration/declared/v1",
        *(name.value.encode("utf-8") for name in tools.names),
    )


@dataclass(frozen=True)
class AgentDefinition:
    """One agent exactly as a human authored it.

    An absent model means the deployment's model: what the file does not spell
    is not the file's decision. An absent tool declaration means every tool,
    because a restriction is only ever explicit.
    """

    name: str
    description: str
    model: str | None
    tools: AgentToolDeclaration
    system_prompt: str
    definition_hash: AgentDefinitionHash = field(init=False)

    def __post_init__(self) -> None:
        _require_authored_text(self.name, AgentDefinitionField.NAME)
        _require_authored_text(self.description, AgentDefinitionField.DESCRIPTION)
        if self.model is not None:
            _require_authored_text(self.model, AgentDefinitionField.MODEL)
        if not isinstance(self.tools, (UnrestrictedTools, DeclaredTools)):
            raise TypeError("agent definition tool declaration must be typed")
        if not self.system_prompt.strip():
            raise AgentDefinitionRefused(AgentDefinitionRefusal.SYSTEM_PROMPT_MISSING)
        object.__setattr__(
            self,
            "definition_hash",
            AgentDefinitionHash.of(
                frame(
                    "agent-definition/v1",
                    self.name.encode("utf-8"),
                    self.description.encode("utf-8"),
                    b"" if self.model is None else self.model.encode("utf-8"),
                    _tool_declaration_frame(self.tools),
                    self.system_prompt.encode("utf-8"),
                )
            ),
        )


@dataclass(frozen=True)
class AgentCatalogDeployment:
    """What a deployment owns of a published agent, and a file never spells.

    A definition names the agent; where it runs, which credentials it uses, and
    which model it falls back to are the serving deployment's facts.
    """

    default_model: str
    auth_profile_revision_hash: AuthProfileRevisionHash
    executor_revision: AgentExecutorRevision


def agent_configuration_revision_for(
    definition: AgentDefinition, deployment: AgentCatalogDeployment
) -> AgentConfigurationRevision:
    """The exact configuration revision one authored definition publishes.

    The catalog revision carries the model, authentication, executor, and
    requested capability -- it has no field for the definition's name,
    description, tool declaration, or system prompt, so those authored facts
    reach no durable owner through this mapping and two definitions differing
    only in them publish one revision. Closing that is a catalog change, not a
    field this authoring format may invent.
    """

    return AgentConfigurationRevision(
        deployment.default_model if definition.model is None else definition.model,
        deployment.auth_profile_revision_hash,
        deployment.executor_revision,
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
