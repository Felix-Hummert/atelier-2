"""What a reader is told about the events of one run.

The durable adapter builds these, the use cases carry them and the API
projects them, so they are shared values rather than the seam itself: the
port next door keeps the protocol and the answers it may give.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.effects import EffectReceipt
from atelier2.contracts.executions import (
    LegacyWaitAnswerAttribution,
    RunEvent,
    RunEventKind,
    WaitAnswerActor,
    WaitAnswerAttribution,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion


@dataclass(frozen=True)
class PersistedRunEvent:
    event: RunEvent
    receipt: EffectReceipt | None
    workflow_format_version: WorkflowFormatVersion = WorkflowFormatVersion.V1
    node_receipt_reason: str | None = None
    wait_answer_actor: WaitAnswerAttribution | None = None

    def __post_init__(self) -> None:
        value = self.workflow_format_version
        if type(value) is bool:
            raise ValueError("persisted event workflow format must be V1, V2, or V3")
        try:
            version = WorkflowFormatVersion(value)
        except ValueError:
            raise ValueError(
                "persisted event workflow format must be V1, V2, or V3"
            ) from None
        object.__setattr__(self, "workflow_format_version", version)
        actor_required = (
            version is WorkflowFormatVersion.V3
            and self.event.event_kind is RunEventKind.WAIT_ANSWERED
        )
        if actor_required != (self.wait_answer_actor is not None):
            raise ValueError("V3 wait answer event and actor attribution disagree")
        if self.wait_answer_actor is not None and not isinstance(
            self.wait_answer_actor,
            WaitAnswerActor | LegacyWaitAnswerAttribution,
        ):
            raise TypeError("wait answer event attribution must use its closed type")


@dataclass(frozen=True)
class RunEventPage:
    events: tuple[PersistedRunEvent, ...]
    terminal_seen: bool
