"""What a published `tool` revision has to say to be redeemable, and what it leaves.

Two pure questions meet here, and both are answered before any run exists: which
published bytes are a grant this runtime redeems, and which exact facts one
redemption is identified by. The seam that consumes the first is the reference
resolution -- the same door that already refuses a `schema` revision that is not
a schema -- so the refusals are measured there rather than only on the function.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

import pytest

from atelier2.application.resolve_references import resolve_declared_reference
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_configuration_v3 import (
    DeclaredReference,
    ReferenceRefusal,
    ReferenceRefusalReason,
    ReferenceSite,
    ResolvedReference,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.tool_grants_v3 import (
    MAXIMUM_TOOL_GRANT_DOCUMENT_BYTES,
    DeclaredToolGrant,
    ToolGrantAccepted,
    ToolGrantCapability,
    ToolGrantRefusal,
    ToolGrantRefused,
    ToolRedemptionReceipt,
    read_tool_grant_document,
)
from atelier2.contracts.workflows_v3 import VersionedReference
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishRevisionResult,
    ResolvePublishedRevisionResult,
)

THE_ONE_GRANT = json.dumps(
    {"capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION.value}
).encode("utf-8")

RUN = RunId("v3/redeemed")
REVISION = WorkflowRevisionHash("a1" * 32)
NODE = "verify"


@dataclass(frozen=True)
class OneRevisionRegistry:
    """A registry carrying exactly the revision under test, and nothing else."""

    revision: PublishedRevision

    def publish_revision(self, revision: PublishedRevision) -> PublishRevisionResult:
        raise AssertionError("resolution never publishes")

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        if kind is self.revision.kind and revision_hash == self.revision.revision_hash:
            return PublishedRevisionFound(self.revision)
        return PublishedRevisionMissing()


def resolution_of(document: bytes) -> ResolvedReference | ReferenceRefusal:
    """What the seam a run start uses answers for these exact published bytes."""
    revision = PublishedRevision(RevisionKind.TOOL, document)
    declared = DeclaredReference(
        ReferenceSite("tools", NODE, None),
        RevisionKind.TOOL,
        VersionedReference(ref="verify", revision=revision.revision_hash.value),
    )
    return resolve_declared_reference(declared, OneRevisionRegistry(revision))


def receipt(**changes: object) -> ToolRedemptionReceipt:
    """One redemption, exactly as an attempt of the node above would leave it."""
    fields: dict[str, object] = {
        "node_execution_id": NodeExecutionId.for_node(RUN, REVISION, NODE),
        "run_id": RUN,
        "workflow_revision_hash": REVISION,
        "node_id": NODE,
        "attempt_id": AgentAttemptId("b2" * 32),
        "tool_revision_hash": PublishedRevisionHash("c3" * 32),
        "capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION,
        "command": ("/bin/true",),
        "exit_code": 0,
        "standard_output_hash": Sha256Hash.of(b"all green"),
    }
    return ToolRedemptionReceipt(**(fields | changes))  # type: ignore[arg-type]


def test_the_one_published_grant_this_runtime_redeems_is_read_and_resolves() -> None:
    assert read_tool_grant_document(THE_ONE_GRANT) == ToolGrantAccepted(
        ToolGrantCapability.RUN_PROJECT_VERIFICATION
    )
    assert isinstance(resolution_of(THE_ONE_GRANT), ResolvedReference)


NOT_A_GRANT_THIS_RUNTIME_REDEEMS: tuple[tuple[str, bytes, ToolGrantRefusal], ...] = (
    (
        "a capability nothing performs",
        b'{"capability": "delete-the-repository"}',
        ToolGrantRefusal.UNKNOWN_CAPABILITY,
    ),
    (
        "a grant naming no capability",
        b"{}",
        ToolGrantRefusal.MISSING_CAPABILITY,
    ),
    (
        "a capability that is not a name",
        b'{"capability": 7}',
        ToolGrantRefusal.MISSING_CAPABILITY,
    ),
    (
        "a grant carrying a field nobody reads",
        b'{"capability": "run-project-verification", "network": "all"}',
        ToolGrantRefusal.UNKNOWN_FIELD,
    ),
    ("prose", b"you may run the tests", ToolGrantRefusal.NOT_A_GRANT_OBJECT),
    (
        "a grant that is not an object",
        b'["run-project-verification"]',
        ToolGrantRefusal.NOT_A_GRANT_OBJECT,
    ),
    ("bytes that are not text", b"\xff\xfe", ToolGrantRefusal.DOCUMENT_NOT_UTF8),
)


@pytest.mark.proves("a-tool-grant-this-runtime-cannot-redeem-is-refused-by-name")
@pytest.mark.parametrize(
    ("label", "document", "expected"),
    NOT_A_GRANT_THIS_RUNTIME_REDEEMS,
    ids=[label for label, _, _ in NOT_A_GRANT_THIS_RUNTIME_REDEEMS],
)
def test_published_bytes_that_are_no_grant_refuse_where_they_were_pinned(
    label: str, document: bytes, expected: ToolGrantRefusal
) -> None:
    del label
    verdict = read_tool_grant_document(document)

    assert isinstance(verdict, ToolGrantRefused)
    assert verdict.reason is expected

    refusal = resolution_of(document)

    assert isinstance(refusal, ReferenceRefusal)
    assert refusal.reason is ReferenceRefusalReason.UNREDEEMABLE_TOOL_GRANT
    assert expected.value in str(refusal)
    assert NODE in str(refusal)


@pytest.mark.proves("a-tool-grant-this-runtime-cannot-redeem-is-refused-by-name")
def test_a_grant_document_larger_than_its_bound_is_refused_before_it_is_read() -> None:
    padded = json.dumps({"capability": "x" * MAXIMUM_TOOL_GRANT_DOCUMENT_BYTES}).encode(
        "utf-8"
    )

    verdict = read_tool_grant_document(padded)

    assert isinstance(verdict, ToolGrantRefused)
    assert verdict.reason is ToolGrantRefusal.DOCUMENT_TOO_LARGE


def test_an_unpinnable_grant_reference_is_refused_as_an_unpublished_revision() -> None:
    declared = DeclaredReference(
        ReferenceSite("tools", NODE, None),
        RevisionKind.TOOL,
        VersionedReference(ref="verify", revision="f0" * 32),
    )

    refusal = resolve_declared_reference(
        declared, OneRevisionRegistry(PublishedRevision(RevisionKind.TOOL, b"{}"))
    )

    assert isinstance(refusal, ReferenceRefusal)
    assert refusal.reason is ReferenceRefusalReason.UNPUBLISHED_REVISION


ONE_FIELD_MUTATIONS: tuple[tuple[str, dict[str, object]], ...] = (
    ("the command", {"command": ("/bin/false",)}),
    ("one argument of the command", {"command": ("/bin/true", "--quiet")}),
    ("the exit code", {"exit_code": 1}),
    ("the standard output", {"standard_output_hash": Sha256Hash.of(b"all red")}),
    ("the grant it redeemed", {"tool_revision_hash": PublishedRevisionHash("d4" * 32)}),
    ("the attempt that redeemed it", {"attempt_id": AgentAttemptId("e5" * 32)}),
)


@pytest.mark.proves("a-granted-node-gets-its-project-verification-run-and-proven")
@pytest.mark.parametrize(
    ("label", "change"),
    ONE_FIELD_MUTATIONS,
    ids=[label for label, _ in ONE_FIELD_MUTATIONS],
)
def test_one_changed_fact_of_a_redemption_is_a_different_receipt(
    label: str, change: dict[str, object]
) -> None:
    del label
    assert receipt().receipt_hash != receipt(**change).receipt_hash


def test_a_redemption_receipt_refuses_an_identity_its_node_binding_denies() -> None:
    with pytest.raises(ValueError, match="differs from its node binding"):
        receipt(node_id="somebody-else")


def test_a_redemption_receipt_refuses_a_command_that_is_not_one() -> None:
    with pytest.raises(ValueError, match="nonempty throughout"):
        receipt(command=())
    with pytest.raises(ValueError, match="nonempty throughout"):
        receipt(command=("/bin/true", ""))


def test_a_declared_grant_carries_the_revision_and_the_capability_it_read() -> None:
    grant = DeclaredToolGrant(
        PublishedRevisionHash("c3" * 32), ToolGrantCapability.RUN_PROJECT_VERIFICATION
    )

    assert replace(grant).revision_hash.value == "c3" * 32
    with pytest.raises(TypeError, match="closed vocabulary"):
        DeclaredToolGrant(PublishedRevisionHash("c3" * 32), "run-project-verification")  # type: ignore[arg-type]
