"""What a reader is told about the events of one run.

The durable adapter builds these, the use cases carry them and the API
projects them, so they are shared values rather than the seam itself: the
port next door keeps the protocol and the answers it may give.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.effects import EffectReceipt
from atelier2.contracts.executions import RunEvent


@dataclass(frozen=True)
class PersistedRunEvent:
    event: RunEvent
    receipt: EffectReceipt | None
    workflow_format_version: int = 1

    def __post_init__(self) -> None:
        if self.workflow_format_version not in (1, 2, 3):
            raise ValueError("persisted event workflow format must be V1, V2, or V3")


@dataclass(frozen=True)
class RunEventPage:
    events: tuple[PersistedRunEvent, ...]
    terminal_seen: bool
