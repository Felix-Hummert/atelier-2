"""What a document published as a `tool` revision grants, and what redeeming it leaves.

ADR 0006 makes a node's `tools` entry a versioned reference into the `tool`
registry, so a capability is never a mark an author writes: it is a published
revision the document pins by hash, exactly like the schema of an output. Bytes
published under the name `tool` are a grant only because someone called them one,
so this module is the reading that turns them into one -- a pure function over
bytes, asked once by the reference resolution that binds a run.

The vocabulary is closed and deliberately holds one capability. A grant naming
anything else is refused where it was declared rather than resolved and then
silently unredeemed, because a run started under a capability nothing performs
would tell its author that the atelier did what the document asked.

The redemption receipt is the other half: what the attempt that redeemed the
grant actually ran, how it ended, and what it said. It is a record of its own
rather than a field of the agent receipt, because the two answer different
questions -- the agent receipt says what the provider produced, and an exit code
sitting beside those bytes would read as the provider's.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self

from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.runs import RunId, WorkflowRevisionHash

MAXIMUM_TOOL_GRANT_DOCUMENT_BYTES = 4_096
"""How large a grant document may be. It names one capability; nothing needs more."""

MAXIMUM_VERIFICATION_COMMAND_BYTES = 4_096
"""How long the exact argv of one verification may be, as its durable record holds it."""


class ToolGrantCapability(StrEnum):
    """The closed set of capabilities a published tool grant may name.

    One entry, because one is what this runtime redeems. A second capability
    enters here together with the executor that performs it, so the set and the
    performance never disagree.
    """

    RUN_PROJECT_VERIFICATION = "run-project-verification"


class ToolGrantRefusal(StrEnum):
    """Why published bytes are not a tool grant, as a stable token."""

    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_NOT_UTF8 = "document_not_utf8"
    NOT_A_GRANT_OBJECT = "not_a_grant_object"
    MISSING_CAPABILITY = "missing_capability"
    UNKNOWN_CAPABILITY = "unknown_capability"
    UNKNOWN_FIELD = "unknown_field"


@dataclass(frozen=True, slots=True)
class ToolGrantAccepted:
    """These bytes grant exactly this capability."""

    capability: ToolGrantCapability


@dataclass(frozen=True, slots=True)
class ToolGrantRefused:
    """These bytes are not a grant this runtime redeems, and why."""

    reason: ToolGrantRefusal
    detail: str = ""

    def __str__(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"{self.reason.value}{suffix}"


type ToolGrantVerdict = ToolGrantAccepted | ToolGrantRefused

_CAPABILITY_FIELD = "capability"


def read_tool_grant_document(document: bytes) -> ToolGrantVerdict:
    """Whether these exact published bytes grant a capability this runtime redeems."""
    if len(document) > MAXIMUM_TOOL_GRANT_DOCUMENT_BYTES:
        return ToolGrantRefused(
            ToolGrantRefusal.DOCUMENT_TOO_LARGE,
            f"{len(document)} bytes exceeds {MAXIMUM_TOOL_GRANT_DOCUMENT_BYTES}",
        )
    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as broken:
        return ToolGrantRefused(ToolGrantRefusal.DOCUMENT_NOT_UTF8, broken.reason)
    try:
        decoded = json.loads(text)
    except ValueError as broken:
        return ToolGrantRefused(ToolGrantRefusal.NOT_A_GRANT_OBJECT, str(broken))
    if not isinstance(decoded, dict):
        return ToolGrantRefused(
            ToolGrantRefusal.NOT_A_GRANT_OBJECT,
            f"a tool grant is an object, not {type(decoded).__name__}",
        )
    unknown = sorted(name for name in decoded if name != _CAPABILITY_FIELD)
    if unknown:
        return ToolGrantRefused(
            ToolGrantRefusal.UNKNOWN_FIELD,
            f"a tool grant names {_CAPABILITY_FIELD} and nothing else, "
            f"not {', '.join(unknown)}",
        )
    named = decoded.get(_CAPABILITY_FIELD)
    if not isinstance(named, str):
        return ToolGrantRefused(
            ToolGrantRefusal.MISSING_CAPABILITY,
            f"a tool grant names the capability it grants under {_CAPABILITY_FIELD}",
        )
    try:
        capability = ToolGrantCapability(named)
    except ValueError:
        return ToolGrantRefused(
            ToolGrantRefusal.UNKNOWN_CAPABILITY,
            f"no runtime here redeems {named!r}",
        )
    return ToolGrantAccepted(capability)


@dataclass(frozen=True, slots=True)
class DeclaredToolGrant:
    """The published grant one node declared, as the run froze it."""

    revision_hash: PublishedRevisionHash
    capability: ToolGrantCapability

    def __post_init__(self) -> None:
        if not isinstance(self.revision_hash, PublishedRevisionHash):
            raise TypeError("a declared tool grant names its published revision")
        if not isinstance(self.capability, ToolGrantCapability):
            raise TypeError("a declared tool grant uses the closed vocabulary")


class ToolRedemptionReceiptHash(Sha256Hash):
    """The immutable identity of one `tool-redemption-receipt/v1`."""


@dataclass(frozen=True)
class ToolRedemptionReceipt:
    """What redeeming one tool grant ran, how it ended, and what it said.

    The output is kept as a hash rather than as bytes: what this record proves is
    that exactly this command produced exactly this answer, and the answer of a
    project verification is a build log whose size no author declared.
    """

    node_execution_id: NodeExecutionId
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    node_id: str
    attempt_id: AgentAttemptId
    tool_revision_hash: PublishedRevisionHash
    capability: ToolGrantCapability
    command: tuple[str, ...]
    exit_code: int
    standard_output_hash: Sha256Hash
    receipt_hash: ToolRedemptionReceiptHash = field(init=False)

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("a tool redemption receipt names the node it belongs to")
        if self.node_execution_id != NodeExecutionId.for_node(
            self.run_id, self.workflow_revision_hash, self.node_id
        ):
            raise ValueError(
                "tool redemption execution identity differs from its node binding"
            )
        if not isinstance(self.capability, ToolGrantCapability):
            raise TypeError("a tool redemption receipt uses the closed vocabulary")
        if not self.command or any(not argument for argument in self.command):
            raise ValueError("a redeemed verification command is nonempty throughout")
        if type(self.exit_code) is not int:
            raise TypeError("a redeemed verification exit code must be an integer")
        if not -MAXIMUM_SIGNED_INT64 - 1 <= self.exit_code <= MAXIMUM_SIGNED_INT64:
            raise ValueError("a redeemed verification exit code must fit signed int64")
        object.__setattr__(self, "receipt_hash", self._hash())

    def _hash(self) -> ToolRedemptionReceiptHash:
        return ToolRedemptionReceiptHash.of(
            frame(
                "tool-redemption-receipt/v1",
                self.node_execution_id.value.encode("ascii"),
                self.run_id.value.encode("utf-8"),
                self.workflow_revision_hash.value.encode("ascii"),
                self.node_id.encode("utf-8"),
                self.attempt_id.value.encode("ascii"),
                self.tool_revision_hash.value.encode("ascii"),
                self.capability.value.encode("ascii"),
                frame(
                    "tool-redemption-command/v1",
                    *(argument.encode("utf-8") for argument in self.command),
                ),
                struct.pack(">q", self.exit_code),
                self.standard_output_hash.value.encode("ascii"),
            )
        )

    @classmethod
    def of(
        cls,
        node_execution_id: NodeExecutionId,
        run_id: RunId,
        workflow_revision_hash: WorkflowRevisionHash,
        node_id: str,
        attempt_id: AgentAttemptId,
        grant: DeclaredToolGrant,
        command: tuple[str, ...],
        exit_code: int,
        standard_output_hash: Sha256Hash,
    ) -> Self:
        return cls(
            node_execution_id,
            run_id,
            workflow_revision_hash,
            node_id,
            attempt_id,
            grant.revision_hash,
            grant.capability,
            command,
            exit_code,
            standard_output_hash,
        )
