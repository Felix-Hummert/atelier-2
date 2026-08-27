"""The pinned-grant decision shared by agent completion and effect redemption."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from atelier2.adapters.dbos.schema import published_revisions
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_bindings import RunBindingConflict
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    ToolGrantCapability,
    ToolGrantCapabilityNotRedeemed,
    ToolGrantRefused,
    read_tool_grant_document,
    redeems_as_platform_effect,
)
from atelier2.contracts.workflows_v3 import AgentNodeV3


def read_pinned_tool_grant(session: Any, node: AgentNodeV3) -> DeclaredToolGrant | None:
    if not node.tools:
        return None
    pinned = node.tools[0]
    document = session.scalar(
        sa.select(published_revisions.c.document).where(
            published_revisions.c.kind == RevisionKind.TOOL.value,
            published_revisions.c.revision_hash == pinned.revision,
        )
    )
    if document is None:
        raise RunBindingConflict("the pinned tool revision left the registry")
    grant = read_tool_grant_document(bytes(document))
    if isinstance(grant, ToolGrantRefused):
        raise RunBindingConflict(f"the pinned tool revision is no grant: {grant}")
    return DeclaredToolGrant(
        PublishedRevisionHash(pinned.revision), grant.capability, grant.operation
    )


def open_pr_capability_for(
    grant: DeclaredToolGrant | None,
) -> ToolGrantCapability | None:
    if grant is None or not redeems_as_platform_effect(grant.capability):
        return None
    if grant.capability is ToolGrantCapability.OPEN_PR:
        return grant.capability
    if grant.capability is ToolGrantCapability.PUSH_ATELIER_COMMIT:
        return None
    raise ToolGrantCapabilityNotRedeemed(grant.capability)


def push_atelier_commit_capability_for(
    grant: DeclaredToolGrant | None,
) -> ToolGrantCapability | None:
    if grant is None or not redeems_as_platform_effect(grant.capability):
        return None
    if grant.capability is ToolGrantCapability.OPEN_PR:
        return None
    if grant.capability is not ToolGrantCapability.PUSH_ATELIER_COMMIT:
        raise ToolGrantCapabilityNotRedeemed(grant.capability)
    if grant.operation is None:
        raise ToolGrantCapabilityNotRedeemed(grant.capability)
    return grant.capability


def agent_node_redeems_platform_effect(session: Any, node: AgentNodeV3) -> bool:
    grant = read_pinned_tool_grant(session, node)
    return grant is not None and redeems_as_platform_effect(grant.capability)
