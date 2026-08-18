from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from atelier2.api.references import encode_event_cursor, encode_public_run_reference
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits
from atelier2.contracts.effects import OperatorFoundEffect
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_events import (
    PersistedRunEvent,
)
from atelier2.contracts.run_projections import (
    RunProjection,
)
from atelier2.contracts.runs import RunId
from atelier2.contracts.workflow_formats import WorkflowFormatVersion


def durable_projection_limit(limits: ApiLimits) -> WorkflowPublicationLimits:
    """The bound a durable reader must hold, derived from the API's own limits.

    It has one owner because it has one value: the whole application reads through
    the same bound, and a reader that could be handed a different one per call is a
    reader whose answers cannot be compared. Deriving it here rather than inside
    the application builder is what lets the reader be constructed with it.
    """
    return WorkflowPublicationLimits(
        maximum_document_bytes=min(
            limits.maximum_request_body_bytes,
            limits.maximum_base64_decoded_bytes,
        ),
        maximum_nodes=limits.maximum_workflow_nodes,
        maximum_string_characters=limits.maximum_field_characters,
        maximum_payload_bytes=min(
            limits.maximum_decoded_payload_bytes,
            limits.maximum_base64_decoded_bytes,
        ),
    )


class ApiLimitExceeded(ValueError):
    pass


def base64_characters_for(payload_bytes: int) -> int:
    """The exact base64 length of a payload of this many bytes.

    Base64 encodes three source bytes as four characters and pads the final
    group, so a payload occupies four characters per started group of three:
    4 * ceil(payload_bytes / 3), in exact integer arithmetic.
    """
    started_groups_of_three = (payload_bytes + 2) // 3
    return 4 * started_groups_of_three


@dataclass(frozen=True)
class ApiLimits:
    maximum_request_body_bytes: int
    maximum_field_characters: int
    maximum_base64_characters: int
    maximum_decoded_payload_bytes: int
    maximum_workflow_nodes: int
    maximum_enriched_page_nodes: int
    maximum_enriched_page_document_bytes: int
    event_page_size: PageLimit
    maximum_control_queries: int
    maximum_event_poll_queries: int
    maximum_query_admission_wait_milliseconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.event_page_size, PageLimit):
            raise TypeError("event_page_size must be a PageLimit")
        for name, value in self.__dict__.items():
            if name == "event_page_size":
                continue
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.maximum_base64_characters < 4:
            raise ValueError(
                "maximum_base64_characters must accommodate a nonempty payload"
            )

    @property
    def maximum_base64_decoded_bytes(self) -> int:
        return 3 * (self.maximum_base64_characters // 4)

    def require_field(self, value: str) -> None:
        if len(value) > self.maximum_field_characters:
            raise ApiLimitExceeded("request field exceeds its character limit")

    def require_base64(self, value: str) -> None:
        if len(value) > self.maximum_base64_characters:
            raise ApiLimitExceeded("base64 field exceeds its character limit")

    def require_payload(self, value: bytes) -> None:
        if len(value) > self.maximum_decoded_payload_bytes:
            raise ApiLimitExceeded("decoded payload exceeds its byte limit")

    def require_encoded_payload(self, value: bytes) -> None:
        self.require_payload(value)
        if base64_characters_for(len(value)) > self.maximum_base64_characters:
            raise ApiLimitExceeded("encoded payload exceeds its character limit")

    def require_public_run_reference(self, run_id: RunId) -> None:
        if len(encode_public_run_reference(run_id)) > self.maximum_field_characters:
            raise ApiLimitExceeded("public run reference exceeds its character limit")

    def require_event_cursor(self, run_id: RunId, sequence: int) -> None:
        if len(encode_event_cursor(run_id, sequence)) > self.maximum_field_characters:
            raise ApiLimitExceeded("event cursor exceeds its character limit")

    def require_run_projection(self, projection: RunProjection) -> None:
        self.require_field(projection.run.run_id.value)
        self.require_public_run_reference(projection.run.run_id)
        if projection.run.last_event_sequence > 0:
            self.require_event_cursor(
                projection.run.run_id, projection.run.last_event_sequence
            )
        self.require_field(projection.run.current_node_id)
        attempt = projection.current_agent_attempt
        if attempt is not None:
            self.require_field(attempt.attempt_id.value)
            self.require_field(attempt.node_execution_id.value)
            self.require_field(attempt.request_hash.value)
            self.require_field(attempt.state)
            if attempt.failure_code is not None:
                self.require_field(attempt.failure_code.value)
        reconciliation = projection.reconciliation
        if reconciliation is None:
            return
        intent = reconciliation.intent.intent
        self.require_field(intent.binding.logical_key.value)
        self.require_encoded_payload(intent.request.payload)
        pending = reconciliation.pending_command
        if pending is None:
            return
        command = pending.command
        self.require_field(command.command_id.value)
        self.require_field(command.actor.value)
        self.require_field(command.evidence)
        determination = command.determination
        if isinstance(determination, OperatorFoundEffect):
            self.require_field(determination.effect_id.value)
            self.require_encoded_payload(determination.result.payload)

    def require_event_projection(self, projection: PersistedRunEvent) -> None:
        event = projection.event
        self.require_field(event.run_id.value)
        self.require_public_run_reference(event.run_id)
        self.require_event_cursor(event.run_id, event.event_sequence)
        self.require_field(event.node_id)
        if event.event_kind is RunEventKind.AGENT_FAILED:
            self.require_field(event.payload.decode("ascii"))
        self.require_encoded_payload(event.payload)
        if (
            event.event_kind is RunEventKind.AGENT_COMPLETED
            and projection.workflow_format_version is WorkflowFormatVersion.V1
        ) or event.event_kind is RunEventKind.WAIT_ANSWERED:
            self.require_field(event.payload.decode("utf-8"))
        receipt = projection.receipt
        if receipt is None:
            return
        self.require_field(receipt.intent.binding.logical_key.value)
        self.require_field(receipt.effect_id.value)
        self.require_encoded_payload(receipt.result.payload)
        if receipt.reconcile_command_id is not None:
            self.require_field(receipt.reconcile_command_id.value)


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        maximum_body_bytes: int,
        api_prefix: str,
    ) -> None:
        self._app = app
        self._maximum_body_bytes = maximum_body_bytes
        self._workflow_publication_path = api_prefix + "/workflow-revisions"
        self._start_run_path = api_prefix + "/runs"
        self._run_command_path = re.compile(
            re.escape(api_prefix) + r"/runs/[^/]+/(?:answers|reconciliations)"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        limit_code = self._limit_code(scope)
        if limit_code is None:
            await self._app(scope, receive, send)
            return
        content_lengths = [
            value
            for name, value in cast(list[tuple[bytes, bytes]], scope["headers"])
            if name.lower() == b"content-length"
        ]
        if content_lengths:
            if (
                len(content_lengths) != 1
                or re.fullmatch(rb"[0-9]+", content_lengths[0]) is None
            ):
                await self._problem(scope, receive, send, limit_code)
                return
            try:
                declared_length = int(content_lengths[0])
            except ValueError:
                await self._problem(scope, receive, send, limit_code)
                return
            if declared_length > self._maximum_body_bytes:
                await self._problem(
                    scope,
                    receive,
                    send,
                    limit_code,
                    "Request body exceeds its byte limit.",
                )
                return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._maximum_body_bytes:
                    from atelier2.api.problems import ApiProblem

                    raise ApiProblem(limit_code, "Request body exceeds its byte limit.")
            return message

        await self._app(scope, limited_receive, send)

    def _limit_code(self, scope: Scope) -> str | None:
        if scope["method"] != "POST":
            return None
        path = scope["path"]
        if path == self._workflow_publication_path:
            return "invalid-workflow-document"
        if path == self._start_run_path or self._run_command_path.fullmatch(path):
            return "invalid-request"
        return None

    @staticmethod
    async def _problem(
        scope: Scope,
        receive: Receive,
        send: Send,
        code: str,
        detail: str | None = None,
    ) -> None:
        from atelier2.api.problems import problem_response

        await problem_response(code, detail)(scope, receive, send)
