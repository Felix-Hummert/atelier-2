"""The Agent Client Protocol as one conversation, in its standard vocabulary only.

**Why a provider-neutral core.** Every duplex vector this product takes speaks
the same published protocol: a handshake, a session, one prompt, and a stream
of updates in which the agent asks its client for a permission or a file. What
differs per vendor is the vocabulary inside those messages, not the protocol
around them, so the lifecycle, the correlation and the bounds live here once
and a vendor's own spelling reaches them through `AcpVocabulary`.

**Why the vocabulary never hands back a provider's own object.** A classifier
that returned the raw message would move the decision -- what effect is this,
which file, which tool -- back into the caller, which must not decide it. It
answers with typed values or with `Unrepresentable`, and an unrepresentable
request that would have reached a file or a shell is refused closed: an
extension cannot widen what a provider may do by being unreadable.

**Why nothing here raises.** This runs inside the loop that owns the child
process, where a raised exception is a state nobody can answer for. Every
refused frame has an answer instead: a JSON-RPC error where one is owed,
bounded transcript evidence, a latched terminal reading, or an orderly close.
An exchange that stopped being one ends as `ProviderTerminalReason.PROTOCOL_FAULT`
and never in a provider's own word: only a `stopReason` the agent itself spelled
reaches `CANCELLED_BY_PROVIDER`, and which promise broke is kept as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from atelier2.adapters.newline_json_rpc import (
    INTERNAL_ERROR_CODE,
    INVALID_PARAMS_CODE,
    INVALID_REQUEST_CODE,
    METHOD_NOT_FOUND_CODE,
    PARSE_ERROR_CODE,
    IncomingFrame,
    JsonObject,
    JsonRpcAnswer,
    JsonRpcError,
    JsonRpcFailure,
    JsonRpcFault,
    JsonRpcId,
    JsonRpcNotification,
    JsonRpcProtocolFault,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonValue,
    NewlineJsonRpc,
    OutgoingMessage,
    UnsendableFrame,
    rendered,
)
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agent_permissions import (
    MINIMUM_PERMISSION_CALL_ORDINAL,
    PermissionCorrelationId,
    PermissionDecision,
    PermissionEffect,
    PermissionRequest,
    PermissionScope,
    PermissionScopeKind,
)
from atelier2.contracts.agent_transcripts import (
    MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    AssistantTurn,
    ToolCalled,
    ToolReturned,
    UnrecognisedProviderOutput,
)
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS
from atelier2.ports.agent_executions import (
    ProviderCancellationCause,
    ProviderCancellationFrame,
    ProviderCancellationRequest,
    ProviderConversationAction,
    ProviderConversationBounds,
    ProviderConversationClosing,
    ProviderConversationComplete,
    ProviderConversationEnding,
    ProviderFilesystemAnswer,
    ProviderFilesystemEffect,
    ProviderFilesystemReply,
    ProviderFilesystemRequest,
    ProviderFilesystemRequestId,
    ProviderSessionEvent,
    ProviderStandardInput,
    ProviderTerminalOutcome,
    ProviderTerminalReason,
)

type Actions = tuple[ProviderConversationAction, ...]
type Steps = tuple[ProviderSessionEvent, ...]

ACP_PROTOCOL_VERSION = 1
MAXIMUM_UNRECOGNISED_UPDATE_STEPS = 32
"""How much of a vocabulary this core cannot read it keeps as evidence.

Past this many, a provider whose whole stream is unreadable would spend the
transcript on repetitions of one finding instead of on the steps around it.
"""


class AcpMethod(StrEnum):
    """Every method this conversation sends or answers, and no other."""

    INITIALIZE = "initialize"
    SESSION_NEW = "session/new"
    SESSION_PROMPT = "session/prompt"
    SESSION_CANCEL = "session/cancel"
    SESSION_UPDATE = "session/update"
    REQUEST_PERMISSION = "session/request_permission"
    READ_TEXT_FILE = "fs/read_text_file"
    WRITE_TEXT_FILE = "fs/write_text_file"


class AcpStopReason(StrEnum):
    """The stop reasons that mean a turn simply ended.

    `cancelled` and `refusal` are deliberately absent: a provider that stopped
    itself is read as one, and its own word is data on the outcome.
    """

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"


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


class AcpSelectableOption(StrEnum):
    """The only two permission options this client will ever select.

    A persistent option answers questions nobody has asked yet, under a policy
    revision bound to this attempt alone.
    """

    ALLOW_ONCE = "allow_once"
    REJECT_ONCE = "reject_once"


class AcpSessionUpdate(StrEnum):
    """The standard update variants this core reads."""

    USER_MESSAGE_CHUNK = "user_message_chunk"
    AGENT_MESSAGE_CHUNK = "agent_message_chunk"
    AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
    TOOL_CALL = "tool_call"
    TOOL_CALL_UPDATE = "tool_call_update"
    AVAILABLE_COMMANDS_UPDATE = "available_commands_update"


class AcpConversationFault(StrEnum):
    """Which of the protocol's promises broke, where one did."""

    HANDSHAKE_REFUSED = "handshake-refused"
    NO_SESSION = "no-session"
    NO_STOP_REASON = "no-stop-reason"
    NO_TERMINAL_ANSWER = "no-terminal-answer"
    UNSENDABLE_FRAME = "unsendable-frame"
    LOST_FRAMING = "lost-framing"
    UNEXPECTED_ANSWER = "unexpected-answer"


_ANSWERED_FRAME_FAULTS = {
    JsonRpcFault.UNPARSEABLE: PARSE_ERROR_CODE,
    JsonRpcFault.NOT_A_MESSAGE: INVALID_REQUEST_CODE,
}
"""A frame that is still framed is answered where the protocol owes an answer."""

_FATAL_FRAME_FAULTS = {
    JsonRpcFault.OVERSIZE_FRAME: AcpConversationFault.LOST_FRAMING,
    JsonRpcFault.UNEXPECTED_RESPONSE: AcpConversationFault.UNEXPECTED_ANSWER,
}
"""A frame nobody can answer is the exchange itself having stopped being one."""

_EFFECT_OF_TOOL_KIND = {
    AcpToolKind.READ: PermissionEffect.WORKSPACE_READ,
    AcpToolKind.EDIT: PermissionEffect.WORKSPACE_WRITE,
    AcpToolKind.DELETE: PermissionEffect.WORKSPACE_WRITE,
    AcpToolKind.MOVE: PermissionEffect.WORKSPACE_WRITE,
}

_EFFECT_OF_FILE_METHOD: dict[str, ProviderFilesystemEffect] = {
    AcpMethod.READ_TEXT_FILE: ProviderFilesystemEffect.READ,
    AcpMethod.WRITE_TEXT_FILE: ProviderFilesystemEffect.WRITE,
}

_STOP_REASONS_THAT_ONLY_END_A_TURN = frozenset(AcpStopReason)

_SETTLED_TOOL_CALL_STATUSES = frozenset(
    {AcpToolCallStatus.COMPLETED, AcpToolCallStatus.FAILED}
)

_REASON_OF_ENDING = {
    ProviderConversationEnding.CANCELLED_BY_OPERATOR: (
        ProviderTerminalReason.CANCELLED_BY_OPERATOR
    ),
    ProviderConversationEnding.CANCELLED_FOR_POLICY: (
        ProviderTerminalReason.POLICY_REFUSED
    ),
    ProviderConversationEnding.CANCELLED_FOR_BUDGET: (
        ProviderTerminalReason.BUDGET_EXHAUSTED
    ),
}

_CLIENT_HANDSHAKE: JsonObject = {
    "protocolVersion": ACP_PROTOCOL_VERSION,
    "clientCapabilities": {
        "fs": {"readTextFile": True, "writeTextFile": True},
        "terminal": False,
    },
}
_REFUSED_FILE_MESSAGE = "this client refused the file"
_UNANSWERABLE_FILE_MESSAGE = "this client cannot answer the file as text"
_UNKNOWN_METHOD_MESSAGE = "this client does not serve that method"
_UNREADABLE_FRAME_MESSAGE = "this client could not read that frame"
_UNREADABLE_PARAMS_MESSAGE = "this client could not read that request"
_WRITE_ACKNOWLEDGED: JsonObject = {}
PROTOCOL_FAULT_EVIDENCE = "acp protocol fault: "
"""How a broken promise is named in the one step that keeps it."""


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


def _cut(text: str) -> str:
    return text[:MAXIMUM_AGENT_FIELD_CHARACTERS]


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
            PermissionScope(PermissionScopeKind.PATH_PREFIX, _cut(locations[0])),
        )

    def _tool_call(self, update: JsonObject) -> ClassifiedUpdate:
        status = update.get("status")
        if status is not None:
            return self._status(status, update)
        title = update.get("title")
        if not isinstance(title, str) or not title:
            return Unrepresentable()
        return ToolCallAnnounced(_cut(title), _locations_of(update))

    def _status(self, status: JsonValue, update: JsonObject) -> ClassifiedUpdate:
        try:
            settled = AcpToolCallStatus(status)
        except ValueError:
            return Unrepresentable()
        if settled not in _SETTLED_TOOL_CALL_STATUSES:
            return NothingToRecord()
        return ToolCallSettled(settled, _tool_content_of(update.get("content")))


@dataclass(frozen=True, slots=True)
class _PendingPermission:
    """One question in flight: who asked it, and which options it may be answered with."""

    identifier: JsonRpcId
    allowed: str
    refused: str


@dataclass(frozen=True, slots=True)
class _PendingFile:
    """One file request in flight: who asked it, and what it asked for."""

    identifier: JsonRpcId
    effect: ProviderFilesystemEffect


@dataclass
class AgentClientProtocolConversation:
    """One attempt's ACP session, from its handshake to its terminal reading.

    It opens with `initialize`, takes the session it is given, prompts once and
    reads what comes back until that prompt is answered. Everything it wants
    done it publishes as an action; nothing here writes, decides or opens.
    """

    attempt_id: AgentAttemptId
    prompt: str
    working_directory: Path
    bounds: ProviderConversationBounds
    maximum_tool_calls: int
    vocabulary: AcpVocabulary = field(default_factory=StandardAcpVocabulary)
    _codec: NewlineJsonRpc = field(init=False)
    _said: str = field(default="", init=False)
    _questions: dict[PermissionCorrelationId, _PendingPermission] = field(
        default_factory=dict, init=False
    )
    _files: dict[ProviderFilesystemRequestId, _PendingFile] = field(
        default_factory=dict, init=False
    )
    _tool_calls: dict[str, str] = field(default_factory=dict, init=False)
    _asked_questions: int = field(default=0, init=False)
    _asked_files: int = field(default=0, init=False)
    _unrecognised: int = field(default=0, init=False)
    _local_cause: ProviderTerminalReason | None = field(default=None, init=False)
    _fault: AcpConversationFault | None = field(default=None, init=False)
    _stop_reason: str = field(default="", init=False)
    _answered_prompt: bool = field(default=False, init=False)
    _ended: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if type(self.maximum_tool_calls) is not int or self.maximum_tool_calls < 1:
            raise ValueError("a conversation admits at least one tool call")
        self._codec = NewlineJsonRpc(self.bounds.maximum_incomplete_frame_bytes)

    def open(self) -> Actions:
        """Say the handshake, before this process has said anything."""

        return self._asking(AcpMethod.INITIALIZE, _CLIENT_HANDSHAKE)

    def receive_output(self, chunk: bytes) -> Actions:
        actions: list[ProviderConversationAction] = []
        for frame in self._codec.receive(chunk):
            if self._ended:
                break
            actions.extend(self._read(frame))
        return tuple(actions)

    def input_written(self, written_bytes: int) -> Actions:
        """Nothing this conversation says depends on what the child already has."""

        return ()

    def answer_permission(self, decision: PermissionDecision) -> ProviderStandardInput:
        pending = self._questions.pop(decision.correlation_id)
        chosen = pending.allowed if decision.granted else pending.refused
        if not decision.granted or not chosen:
            self._latch(ProviderTerminalReason.POLICY_REFUSED)
        return self._answering(pending.identifier, _permission_outcome(chosen))

    def answer_filesystem(
        self, reply: ProviderFilesystemReply
    ) -> ProviderStandardInput:
        pending = self._files.pop(reply.request_id)
        if reply.answer is ProviderFilesystemAnswer.REFUSED:
            return self._file_refused(pending.identifier, _REFUSED_FILE_MESSAGE)
        if pending.effect is ProviderFilesystemEffect.WRITE:
            return self._answering(pending.identifier, _WRITE_ACKNOWLEDGED)
        try:
            content = reply.content.decode("utf-8")
        except UnicodeDecodeError:
            return self._file_refused(pending.identifier, _UNANSWERABLE_FILE_MESSAGE)
        return self._answering(pending.identifier, {"content": content})

    def _file_refused(
        self, identifier: JsonRpcId, message: str
    ) -> ProviderStandardInput:
        return self._delivered(JsonRpcError(identifier, INTERNAL_ERROR_CODE, message))

    def finish(self, ending: ProviderConversationEnding) -> ProviderConversationClosing:
        if not self._answered_prompt:
            self._stop_talking(AcpConversationFault.NO_TERMINAL_ANSWER)
        return ProviderConversationClosing(
            self._outcome(ending),
            self._flushed() + self._broken_promise() + self._half_frame(),
        )

    def _read(self, frame: IncomingFrame) -> Actions:
        match frame:
            case JsonRpcNotification(method, params):
                return self._notified(method, params)
            case JsonRpcRequest() as asked:
                return self._questioned(asked)
            case JsonRpcAnswer(method, result):
                return self._answered(method, result)
            case JsonRpcFailure():
                return self._faulted(AcpConversationFault.HANDSHAKE_REFUSED)
            case JsonRpcProtocolFault(fault):
                return self._refused_frame(fault)

    def _refused_frame(self, fault: JsonRpcFault) -> Actions:
        code = _ANSWERED_FRAME_FAULTS.get(fault)
        if code is None:
            return self._faulted(_FATAL_FRAME_FAULTS[fault])
        return self._sending(JsonRpcError(None, code, _UNREADABLE_FRAME_MESSAGE))

    def _notified(self, method: str, params: JsonObject) -> Actions:
        if method != AcpMethod.SESSION_UPDATE:
            return ()
        update = params.get("update")
        if not isinstance(update, dict):
            return self._flushed() + self._evidence(rendered(params))
        return self._updated(update)

    def _updated(self, update: JsonObject) -> Actions:
        identifier = update.get("toolCallId")
        named = identifier if isinstance(identifier, str) else ""
        classified = self.vocabulary.classify_update(update)
        if isinstance(classified, AssistantText):
            return self._spoken(classified.text)
        if isinstance(classified, NothingToRecord):
            return ()
        if isinstance(classified, Unrepresentable) or not named:
            return self._flushed() + self._evidence(rendered(update))
        recorded = self._charged(named) + self._flushed()
        if isinstance(classified, ToolCallAnnounced):
            return recorded + self._announced(
                named, classified.title, classified.locations
            )
        return recorded + self._settled(named, classified.status, classified.content)

    def _questioned(self, asked: JsonRpcRequest) -> Actions:
        if asked.method == AcpMethod.REQUEST_PERMISSION:
            return self._permission_asked(asked)
        effect = _EFFECT_OF_FILE_METHOD.get(asked.method)
        if effect is not None:
            return self._file_asked(asked, effect)
        return self._sending(
            JsonRpcError(asked.id, METHOD_NOT_FOUND_CODE, _UNKNOWN_METHOD_MESSAGE)
        )

    def _answered(self, method: str, result: JsonObject) -> Actions:
        match method:
            case AcpMethod.INITIALIZE:
                return self._asking(
                    AcpMethod.SESSION_NEW,
                    {"cwd": str(self.working_directory), "mcpServers": []},
                )
            case AcpMethod.SESSION_NEW:
                return self._session_opened(result)
            case AcpMethod.SESSION_PROMPT:
                return self._turn_ended(result)
            case _:
                return self._faulted(AcpConversationFault.NO_TERMINAL_ANSWER)

    def _session_opened(self, result: JsonObject) -> Actions:
        session = result.get("sessionId")
        if not isinstance(session, str) or not session:
            return self._faulted(AcpConversationFault.NO_SESSION)
        cancellation = self._codec.encode(
            JsonRpcNotification(AcpMethod.SESSION_CANCEL, {"sessionId": session}),
            self.bounds.maximum_cancel_bytes,
        )
        if isinstance(cancellation, UnsendableFrame):
            return self._faulted(AcpConversationFault.UNSENDABLE_FRAME)
        return (ProviderCancellationFrame(cancellation.data),) + self._asking(
            AcpMethod.SESSION_PROMPT,
            {"sessionId": session, "prompt": [{"type": "text", "text": self.prompt}]},
        )

    def _turn_ended(self, result: JsonObject) -> Actions:
        self._answered_prompt = True
        self._ended = True
        stopped = result.get("stopReason")
        spoken = stopped if isinstance(stopped, str) else ""
        if not spoken:
            self._stop_talking(AcpConversationFault.NO_STOP_REASON)
        elif spoken not in _STOP_REASONS_THAT_ONLY_END_A_TURN:
            self._stop_reason = _cut(spoken)
        return self._flushed() + (ProviderConversationComplete(),)

    def _permission_asked(self, asked: JsonRpcRequest) -> Actions:
        offered = _PendingPermission(
            asked.id,
            _option_of(asked.params, AcpSelectableOption.ALLOW_ONCE),
            _option_of(asked.params, AcpSelectableOption.REJECT_ONCE),
        )
        tool_call = asked.params.get("toolCall")
        if not isinstance(tool_call, dict):
            return self._closed_refusal(asked.params, offered)
        named = tool_call.get("toolCallId")
        charged = self._charged(named) if isinstance(named, str) and named else ()
        classified = self.vocabulary.classify_permission(tool_call)
        if isinstance(classified, Unrepresentable):
            return charged + self._closed_refusal(tool_call, offered)
        self._asked_questions += 1
        correlation = PermissionCorrelationId.for_call(
            self.attempt_id, MINIMUM_PERMISSION_CALL_ORDINAL + self._asked_questions - 1
        )
        self._questions[correlation] = offered
        return charged + (
            PermissionRequest(classified.effect, classified.scope, correlation),
        )

    def _closed_refusal(
        self, asked: JsonObject, offered: _PendingPermission
    ) -> Actions:
        self._latch(ProviderTerminalReason.POLICY_REFUSED)
        return (
            self._flushed()
            + self._evidence(rendered(asked))
            + self._sending(
                JsonRpcResponse(
                    offered.identifier, _permission_outcome(offered.refused)
                )
            )
        )

    def _file_asked(
        self, asked: JsonRpcRequest, effect: ProviderFilesystemEffect
    ) -> Actions:
        path = asked.params.get("path")
        content = asked.params.get("content", "")
        if not isinstance(path, str) or not isinstance(content, str):
            return self._sending(
                JsonRpcError(asked.id, INVALID_PARAMS_CODE, _UNREADABLE_PARAMS_MESSAGE)
            )
        self._asked_files += 1
        request_id = ProviderFilesystemRequestId(self._asked_files)
        self._files[request_id] = _PendingFile(asked.id, effect)
        return (
            ProviderFilesystemRequest(
                effect, Path(path), request_id, content.encode("utf-8")
            ),
        )

    def _asking(self, method: AcpMethod, params: JsonObject) -> Actions:
        return self._sending(self._codec.ask(method, params))

    def _written(self, message: OutgoingMessage) -> ProviderStandardInput | None:
        """The bytes this message costs, or nothing where it will not fit."""

        encoded = self._codec.encode(message, self.bounds.maximum_reply_bytes)
        if isinstance(encoded, UnsendableFrame):
            return None
        return ProviderStandardInput(encoded.data)

    def _sending(self, message: OutgoingMessage) -> Actions:
        written = self._written(message)
        if written is None:
            return self._faulted(AcpConversationFault.UNSENDABLE_FRAME)
        return (written,)

    def _answering(
        self, identifier: JsonRpcId, result: JsonObject
    ) -> ProviderStandardInput:
        return self._delivered(JsonRpcResponse(identifier, result))

    def _delivered(self, message: OutgoingMessage) -> ProviderStandardInput:
        written = self._written(message)
        if written is not None:
            return written
        self._stop_talking(AcpConversationFault.UNSENDABLE_FRAME)
        return ProviderStandardInput(b"")

    def _faulted(self, fault: AcpConversationFault) -> Actions:
        self._stop_talking(fault)
        return self._flushed() + (ProviderConversationComplete(),)

    def _stop_talking(self, fault: AcpConversationFault) -> None:
        self._ended = True
        self._fault = self._fault or fault

    def _latch(self, reason: ProviderTerminalReason) -> None:
        if self._local_cause is None:
            self._local_cause = reason

    def _charged(self, identifier: str) -> Actions:
        if identifier in self._tool_calls or self._local_cause is not None:
            return ()
        self._tool_calls[identifier] = ""
        if len(self._tool_calls) <= self.maximum_tool_calls:
            return ()
        self._latch(ProviderTerminalReason.BUDGET_EXHAUSTED)
        return (ProviderCancellationRequest(ProviderCancellationCause.BUDGET),)

    def _announced(
        self, identifier: str, title: str, locations: tuple[str, ...]
    ) -> Steps:
        already = bool(self._tool_calls.get(identifier))
        self._tool_calls[identifier] = title
        if already:
            return ()
        return (ProviderSessionEvent(ToolCalled(title, ", ".join(locations))),)

    def _settled(
        self, identifier: str, status: AcpToolCallStatus, content: str
    ) -> Steps:
        answered = f"{status.value}: {content}" if content else status.value
        return (
            ProviderSessionEvent(
                ToolReturned(self._tool_calls.get(identifier, ""), answered)
            ),
        )

    def _spoken(self, text: str) -> Actions:
        self._said += text
        if len(self._said) < MAXIMUM_TRANSCRIPT_STEP_CHARACTERS:
            return ()
        return self._flushed()

    def _flushed(self) -> Steps:
        if not self._said:
            return ()
        said, self._said = self._said, ""
        return (ProviderSessionEvent(AssistantTurn(said)),)

    def _evidence(self, text: str) -> Steps:
        if self._unrecognised >= MAXIMUM_UNRECOGNISED_UPDATE_STEPS:
            return ()
        self._unrecognised += 1
        return _kept(text)

    def _broken_promise(self) -> Steps:
        """Which of the protocol's promises broke, where one did.

        The reading itself is `PROTOCOL_FAULT`, and the seam's one word belongs
        to a provider that stopped itself -- so what broke is evidence, kept in
        the transcript that owns its width.
        """

        if self._fault is None:
            return ()
        return _kept(PROTOCOL_FAULT_EVIDENCE + self._fault.value)

    def _half_frame(self) -> Steps:
        rest = self._codec.incomplete_frame()
        return _kept(rest.decode("utf-8", "replace")) if rest else ()

    def _outcome(self, ending: ProviderConversationEnding) -> ProviderTerminalOutcome:
        if self._local_cause is not None:
            return ProviderTerminalOutcome(self._local_cause)
        cancelled = _REASON_OF_ENDING.get(ending)
        if cancelled is not None:
            return ProviderTerminalOutcome(cancelled)
        if self._fault is not None:
            return ProviderTerminalOutcome(ProviderTerminalReason.PROTOCOL_FAULT)
        if self._stop_reason:
            return ProviderTerminalOutcome(
                ProviderTerminalReason.CANCELLED_BY_PROVIDER, self._stop_reason
            )
        return ProviderTerminalOutcome(ProviderTerminalReason.ENDED)


def _kept(text: str) -> Steps:
    return (ProviderSessionEvent(UnrecognisedProviderOutput(text)),)


def _permission_outcome(chosen: str) -> JsonObject:
    """The standard answer to one permission question, refusal included."""

    if not chosen:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": chosen}}


def _option_of(params: JsonObject, wanted: AcpSelectableOption) -> str:
    """The provider's own opaque id for the one option of this kind, if it offered one.

    The id travels back as it arrived and is never read: what an option means
    is its `kind`. Above the field bound it is refused rather than cut, because
    a cut id addresses nothing.
    """

    options = params.get("options")
    if not isinstance(options, list):
        return ""
    for option in options:
        if not isinstance(option, dict) or option.get("kind") != wanted:
            continue
        offered = option.get("optionId")
        if (
            isinstance(offered, str)
            and 0 < len(offered) <= MAXIMUM_AGENT_FIELD_CHARACTERS
        ):
            return offered
    return ""
