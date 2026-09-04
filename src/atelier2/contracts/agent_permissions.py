"""What a running provider may do, decided against one bound policy revision.

ADR 0020 §3: a permission is authorisation, never evidence. The authority is an
immutable typed policy revision bound to the execution before its session opens,
and every question a provider asks is answered against exactly that revision --
fail-closed, so anything the policy does not name is refused. The transcript
later projects what was asked and answered; it never decides it.

The correlation id ties one question to its answer and is minted from the
attempt the question belongs to. Provider bytes never enter it: a provider that
invents, repeats, or omits its own request id can then neither address an answer
meant for another question nor make two questions look like one.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.hashing import Sha256Hash, frame

MINIMUM_PERMISSION_CALL_ORDINAL = 1
MAXIMUM_PERMISSION_CALL_ORDINAL = 0xFFFFFFFFFFFFFFFF


class PermissionEffect(StrEnum):
    """The closed vocabulary of what a provider can ask to do.

    Closed on purpose: a provider request the adapter boundary cannot express as
    one of these never reaches the decider, so the decider judges a domain it
    fully knows rather than guessing at a vendor's own words.
    """

    WORKSPACE_READ = "workspace-read"
    WORKSPACE_WRITE = "workspace-write"
    COMMAND = "command"
    NETWORK = "network"
    # Named for the reading, not for the material: `tests/domain/
    # test_credential_channel.py` refuses any source name that could stand for a
    # credential channel, and this vocabulary opens none.
    SECRET_READ = "secret-read"


class PermissionScopeKind(StrEnum):
    """How a scope names the thing an effect would reach."""

    PATH_PREFIX = "path-prefix"
    COMMAND_NAME = "command-name"
    HOST = "host"


@dataclass(frozen=True, slots=True)
class PermissionScope:
    """The exact thing one effect would reach, in its own kind's spelling."""

    kind: PermissionScopeKind
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PermissionScopeKind):
            raise TypeError("a permission scope uses the closed kind vocabulary")
        if not self.value:
            raise ValueError("a permission scope names what it reaches")


class PermissionCorrelationId(Sha256Hash):
    """The identity of one permission question inside one attempt."""

    @classmethod
    def for_call(
        cls, attempt_id: AgentAttemptId, call_ordinal: int
    ) -> PermissionCorrelationId:
        """Mint the id of this attempt's `call_ordinal`-th question.

        Derived from durable truth alone, so the same call of the same attempt
        always addresses the same question however the provider spelled it.
        """

        if type(call_ordinal) is not int or not (
            MINIMUM_PERMISSION_CALL_ORDINAL
            <= call_ordinal
            <= MAXIMUM_PERMISSION_CALL_ORDINAL
        ):
            raise ValueError("a permission call ordinal counts from one, within uint64")
        return cls.of(
            frame(
                "agent-permission-correlation-id/v1",
                attempt_id.value.encode("ascii"),
                struct.pack(">Q", call_ordinal),  # minted-id family; see hashing.frame
            )
        )


class PermissionAuthority(StrEnum):
    """Who answered one question.

    Only the bound policy answers today. The vocabulary exists because a
    decision must say what stands behind it, and ADR 0020 names a human
    mid-turn decision as an edge this product has not decided.
    """

    POLICY = "policy"


class PermissionPolicyRevisionHash(Sha256Hash):
    """The immutable identity of one `agent-permission-policy-revision/v1`."""


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """One question a running provider asked, as Atelier understands it."""

    effect: PermissionEffect
    scope: PermissionScope
    correlation_id: PermissionCorrelationId

    def __post_init__(self) -> None:
        if not isinstance(self.effect, PermissionEffect):
            raise TypeError("a permission request uses the closed effect vocabulary")


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """The answer to one question, with the authority it was answered under.

    It carries the policy revision hash rather than the policy: what a later
    reader needs is which authorisation this answer stands on, and the revision
    hash says that in one comparable value.
    """

    correlation_id: PermissionCorrelationId
    granted: bool
    policy_revision_hash: PermissionPolicyRevisionHash
    authority: PermissionAuthority


@dataclass(frozen=True)
class PermissionPolicyRevision:
    """Everything a provider may do under this revision, and nothing else.

    The grants are a set because a policy states which pairs are permitted, not
    an order in which they were written; the hash sorts them so that two
    constructions of the same permission answer with the same identity.
    """

    grants: frozenset[tuple[PermissionEffect, PermissionScope]]
    revision_hash: PermissionPolicyRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision_hash", self._hash())

    def _hash(self) -> PermissionPolicyRevisionHash:
        return PermissionPolicyRevisionHash.of(
            frame(
                "agent-permission-policy-revision/v1",
                *(
                    frame(
                        "agent-permission-grant/v1",
                        effect.value.encode("ascii"),
                        scope.kind.value.encode("ascii"),
                        scope.value.encode("utf-8"),
                    )
                    for effect, scope in sorted(
                        self.grants,
                        key=lambda grant: (
                            grant[0].value,
                            grant[1].kind.value,
                            grant[1].value,
                        ),
                    )
                ),
            )
        )


GRANTS_NOTHING = PermissionPolicyRevision(frozenset())
"""The closed policy: an execution nobody bound an authorisation to may do nothing.

This is a policy, not an absence of one -- a decision made under it names its
revision hash exactly as any other does, so a refusal is readable rather than a
gap in the record.
"""


def decide(
    policy: PermissionPolicyRevision, request: PermissionRequest
) -> PermissionDecision:
    """Answer one question against one revision, and nothing else.

    Pure and total: the grant is the exact pair the policy states, so an effect
    the policy never names and a scope it names for another effect are refused
    alike. There is no widening rule -- a path prefix does not cover a longer
    one here, because a rule that reads beyond the exact pair is a rule someone
    has to re-derive at every reading of the record.
    """

    return PermissionDecision(
        request.correlation_id,
        (request.effect, request.scope) in policy.grants,
        policy.revision_hash,
        PermissionAuthority.POLICY,
    )


@dataclass(frozen=True, slots=True)
class PolicyPermissionDecider:
    """The bound policy, answering every question of one execution.

    What travels across the session seam: the seam needs something to ask, and
    binding the revision here is what makes "this execution was authorised by
    exactly that revision" true for every question it goes on to answer.
    """

    policy: PermissionPolicyRevision

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        return decide(self.policy, request)
