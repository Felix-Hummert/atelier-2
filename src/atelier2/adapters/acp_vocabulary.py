"""What one provider's spelling of a standard ACP message means, and nothing more.

**Why this is its own module.** The conversation owns the protocol -- the
handshake, the session, the correlation of every question to its answer, the
bounds and the terminal reading -- and none of that changes when a vendor
spells a tool call its own way. What does change lives here: reading a message
into this product's own values. A vendor vocabulary is therefore written beside
this one and never inside the state machine, and no extension can widen what a
provider may do by reaching into the lifecycle.

**Why it never hands back a provider's own object.** A classifier that returned
the raw message would move the decision -- what effect is this, which file,
which tool -- back to the caller, which must not decide it. Every answer here is
a typed value of this module or `Unrepresentable`, and the caller refuses an
unrepresentable effect closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from atelier2.adapters.newline_json_rpc import JsonObject, JsonValue
from atelier2.contracts.agent_permissions import (
    PermissionEffect,
    PermissionScope,
    PermissionScopeKind,
)
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS


class AcpSessionUpdate(StrEnum):
    """The standard update variants this core reads."""

    USER_MESSAGE_CHUNK = "user_message_chunk"
    AGENT_MESSAGE_CHUNK = "agent_message_chunk"
    AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
    TOOL_CALL = "tool_call"
    TOOL_CALL_UPDATE = "tool_call_update"
    AVAILABLE_COMMANDS_UPDATE = "available_commands_update"


class AcpToolKind(StrEnum):
    """The tool kinds whose effect the standard vocabulary can name by itself.

    `execute` and `fetch` are absent, and their absence is the decision: a
    standard permission request names no command and no host, so a shell or a
    network right read from it would be one nobody could scope.
    """

    READ = "read"
    EDIT = "edit"
    DELETE = "delete"
    MOVE = "move"


class AcpToolCallStatus(StrEnum):
    """How far one tool call has got, as a standard update reports it."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


_EFFECT_OF_TOOL_KIND = {
    AcpToolKind.READ: PermissionEffect.WORKSPACE_READ,
    AcpToolKind.EDIT: PermissionEffect.WORKSPACE_WRITE,
    AcpToolKind.DELETE: PermissionEffect.WORKSPACE_WRITE,
    AcpToolKind.MOVE: PermissionEffect.WORKSPACE_WRITE,
}

_SETTLED_TOOL_CALL_STATUSES = frozenset(
    {AcpToolCallStatus.COMPLETED, AcpToolCallStatus.FAILED}
)


@dataclass(frozen=True, slots=True)
class AssistantText:
    """Prose the agent produced, to be read together with what stands beside it."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallAnnounced:
    """The agent said which door it is opening, and where."""

    title: str
    locations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolCallSettled:
    """That door is finished, and this is how it went."""

    status: AcpToolCallStatus
    content: str


@dataclass(frozen=True, slots=True)
class NothingToRecord:
    """A standard message whose content this transcript already owns."""


@dataclass(frozen=True, slots=True)
class Unrepresentable:
    """This vocabulary cannot say what the message said."""


type ClassifiedUpdate = (
    AssistantText
    | ToolCallAnnounced
    | ToolCallSettled
    | NothingToRecord
    | Unrepresentable
)


@dataclass(frozen=True, slots=True)
class ClassifiedEffect:
    """What one permission request would actually do, in Atelier's vocabulary."""

    effect: PermissionEffect
    scope: PermissionScope


type ClassifiedPermission = ClassifiedEffect | Unrepresentable


class AcpVocabulary(Protocol):
    """What one provider's spelling of a standard message means, or nothing.

    The seam a vendor extension arrives through: it reads the message and
    answers with this module's own values, so a field an implementation does
    not know cannot become a permission, a path or a step by accident.
    """

    def classify_update(self, update: JsonObject) -> ClassifiedUpdate:
        """Read one `session/update` as a step of the story, or as nothing."""
        ...

    def classify_permission(self, tool_call: JsonObject) -> ClassifiedPermission:
        """Read what one permission request would do, or refuse to name it."""
        ...


def cut_to_field(text: str) -> str:
    """A word read off the wire, held to what one agent field may cost."""

    return text[:MAXIMUM_AGENT_FIELD_CHARACTERS]


def _text_of(block: JsonValue) -> str:
    """The text of one standard content block, or nothing where it carries none."""

    if not isinstance(block, dict):
        return ""
    text = block.get("text")
    return text if isinstance(text, str) else ""


def _tool_content_of(blocks: JsonValue) -> str:
    """Every text a standard tool call reported, in the order it reported it."""

    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        text
        for block in blocks
        if isinstance(block, dict)
        for text in (_text_of(block.get("content")),)
        if text
    )


def _locations_of(update: JsonObject) -> tuple[str, ...]:
    """Every path a standard tool call named, and nothing a vendor named."""

    locations = update.get("locations")
    if not isinstance(locations, list):
        return ()
    return tuple(
        path
        for location in locations
        if isinstance(location, dict)
        for path in (location.get("path"),)
        if isinstance(path, str) and path
    )


@dataclass(frozen=True, slots=True)
class StandardAcpVocabulary:
    """The published protocol read exactly as published, and not one field more."""

    def classify_update(self, update: JsonObject) -> ClassifiedUpdate:
        try:
            variant = AcpSessionUpdate(update.get("sessionUpdate"))
        except ValueError:
            return Unrepresentable()
        match variant:
            case (
                AcpSessionUpdate.AGENT_MESSAGE_CHUNK
                | AcpSessionUpdate.AGENT_THOUGHT_CHUNK
            ):
                spoken = _text_of(update.get("content"))
                return AssistantText(spoken) if spoken else NothingToRecord()
            case AcpSessionUpdate.TOOL_CALL | AcpSessionUpdate.TOOL_CALL_UPDATE:
                return self._tool_call(update)
            case (
                AcpSessionUpdate.USER_MESSAGE_CHUNK
                | AcpSessionUpdate.AVAILABLE_COMMANDS_UPDATE
            ):
                return NothingToRecord()

    def classify_permission(self, tool_call: JsonObject) -> ClassifiedPermission:
        try:
            kind = AcpToolKind(tool_call.get("kind"))
        except ValueError:
            return Unrepresentable()
        locations = _locations_of(tool_call)
        if not locations:
            return Unrepresentable()
        return ClassifiedEffect(
            _EFFECT_OF_TOOL_KIND[kind],
            PermissionScope(
                PermissionScopeKind.PATH_PREFIX, cut_to_field(locations[0])
            ),
        )

    def _tool_call(self, update: JsonObject) -> ClassifiedUpdate:
        status = update.get("status")
        if status is not None:
            return self._status(status, update)
        title = update.get("title")
        if not isinstance(title, str) or not title:
            return Unrepresentable()
        return ToolCallAnnounced(cut_to_field(title), _locations_of(update))

    def _status(self, status: JsonValue, update: JsonObject) -> ClassifiedUpdate:
        try:
            settled = AcpToolCallStatus(status)
        except ValueError:
            return Unrepresentable()
        if settled not in _SETTLED_TOOL_CALL_STATUSES:
            return NothingToRecord()
        return ToolCallSettled(settled, _tool_content_of(update.get("content")))
