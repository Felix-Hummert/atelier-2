from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from enum import StrEnum

from atelier2.contracts.agent_attempts import (
    RunnerGenerationBinding,
    RunnerInvocationId,
)

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class RunnerSessionMessage(StrEnum):
    INVOCATION_OFFER = "INVOCATION_OFFER"
    PREPARE = "PREPARE"
    READY = "READY"
    LAUNCH = "LAUNCH"
    STARTED = "STARTED"
    CANCEL = "CANCEL"
    TERMINAL_AVAILABLE = "TERMINAL_AVAILABLE"
    READBACK = "READBACK"
    TERMINAL_RECORD = "TERMINAL_RECORD"
    ACK = "ACK"
    ACK_TOMBSTONE = "ACK_TOMBSTONE"
    RELEASE = "RELEASE"
    RELEASED = "RELEASED"
    REFUSE = "REFUSE"


_PAYLOAD_FIELD_COUNTS = {
    RunnerSessionMessage.INVOCATION_OFFER: 0,
    RunnerSessionMessage.PREPARE: 19,
    RunnerSessionMessage.READY: 12,
    RunnerSessionMessage.LAUNCH: 0,
    RunnerSessionMessage.STARTED: 1,
    RunnerSessionMessage.CANCEL: 4,
    RunnerSessionMessage.TERMINAL_AVAILABLE: 1,
    RunnerSessionMessage.READBACK: 0,
    RunnerSessionMessage.TERMINAL_RECORD: 1,
    RunnerSessionMessage.ACK: 1,
    RunnerSessionMessage.ACK_TOMBSTONE: 1,
    RunnerSessionMessage.RELEASE: 1,
    RunnerSessionMessage.RELEASED: 1,
    RunnerSessionMessage.REFUSE: 2,
}

# A REFUSE names its code first and, second, the retained evidence it is about.
# A refusal that precedes any evidence -- one a Runner sends before READY,
# because it cannot attest its own toolchain -- carries an empty second field,
# because there is honestly nothing yet for it to name.
REFUSAL_CODE_FIELD = 0
REFUSAL_EVIDENCE_FIELD = 1
NO_REFUSED_EVIDENCE = b""


@dataclass(frozen=True)
class RunnerSessionFrame:
    """One closed, binding-carrying session message before wire encoding."""

    message: RunnerSessionMessage
    sequence: int
    binding: RunnerGenerationBinding
    invocation_id: RunnerInvocationId
    payload: tuple[bytes, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.message, RunnerSessionMessage):
            raise TypeError("runner session message must be closed")
        if type(self.sequence) is not int or not 1 <= self.sequence <= 2**64 - 1:
            raise ValueError("runner session sequence must be a positive uint64")
        if not isinstance(self.binding, RunnerGenerationBinding):
            raise TypeError("runner session frame needs a typed generation binding")
        if not isinstance(self.invocation_id, RunnerInvocationId):
            raise TypeError("runner session frame needs a typed invocation")
        if _TOKEN.fullmatch(self.binding.generation_id.value) is None:
            raise ValueError(
                "runner session generation token must be canonical base64url"
            )
        if _TOKEN.fullmatch(self.invocation_id.value) is None:
            raise ValueError(
                "runner session invocation token must be canonical base64url"
            )
        if type(self.payload) is not tuple or any(
            type(field) is not bytes for field in self.payload
        ):
            raise TypeError("runner session payload must be exact byte fields")
        if len(self.payload) != _PAYLOAD_FIELD_COUNTS[self.message]:
            raise ValueError("runner session message has the wrong payload field count")

    def fields(self) -> tuple[bytes, ...]:
        return (
            self.message.value.encode("ascii"),
            b"1",
            struct.pack(">Q", self.sequence),
            self.binding.attempt_id.value.encode("ascii"),
            self.binding.request_hash.value.encode("ascii"),
            self.binding.generation_id.value.encode("ascii"),
            self.invocation_id.value.encode("ascii"),
            self.binding.manifest_id.value.encode("ascii"),
            *self.payload,
        )


MAXIMUM_RUNNER_A_TEXT_BYTES = 4_096

RUNNER_SESSION_REFUSAL_CODES = frozenset(
    {
        "runner-session-oversized",
        "runner-session-truncated",
        "runner-session-noncanonical",
        "runner-session-message-unknown",
        "runner-session-out-of-order",
        "runner-session-sequence-mismatch",
        "runner-session-replay",
        "runner-session-reconnect-unsupported-a",
        "runner-session-binding-mismatch",
        "runner-a-output-schema-unbound",
        "runner-a-turn-limit-unbound",
        "runner-a-round-out-of-range",
        "runner-a-text-oversized",
        "runner-a-executor-unavailable",
        "runner-request-hash-mismatch",
        "runner-manifest-mismatch",
        "runner-attestation-mismatch",
        "runner-provider-cli-drift",
        "runner-toolchain-unpinned",
        "runner-provider-cli-absent",
        "runner-provider-credential-absent",
        "runner-provider-policy-present",
        "runner-provider-toolchain-unusable",
        "runner-peer-unverified",
        "runner-peer-eku-mismatch",
        "runner-binding-san-mismatch",
        "auth-profile-unresolvable",
        "runner-generation-conflict",
        "runner-invocation-conflict",
        "runner-arm-conflict",
        "runner-child-boundary-unavailable",
        "runner-cancel-conflict",
        "runner-replacement-not-supported-a",
        "runner-terminal-record-missing",
        "runner-terminal-record-corrupt",
        "runner-terminal-record-oversized",
        "runner-ack-hash-mismatch",
        "runner-release-before-ack",
    }
)
