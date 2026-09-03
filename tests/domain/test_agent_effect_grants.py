"""A node may bind at most one exec-shaped and at most one effect-shaped grant.

`agent_effect_grants.py` is what a node's `tools` pins resolve to once a run
holds it: `read_pinned_exec_tool_grant` for the grant carried in the durable
binding, `read_pinned_effect_tool_grant` for the one an Action or the agent's
own completion later redeems. Two pins of one shape name a redemption nothing
here could tell apart, so the module refuses them once their published bytes
say which shape each pin actually is (#1101).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from atelier2.adapters.dbos.agent_effect_grants import (
    read_pinned_effect_tool_grant,
    read_pinned_exec_tool_grant,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import published_revisions
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import RunBindingConflict
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.contracts.workflows_v3 import AgentNodeV3, VersionedReference

PUSH_OPERATION_REVISION = "a1" * 32


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "grant-shape-test"),
        LoopbackEffectAdapterFactory(
            tmp_path / "loopback.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def _published_grant(
    capability: ToolGrantCapability, *, operation_revision: str | None = None
) -> PublishedRevision:
    body: dict[str, object] = {"capability": capability.value}
    if operation_revision is not None:
        body["operation"] = {
            "ref": "push-atelier-commit",
            "revision": operation_revision,
        }
    document = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return PublishedRevision(RevisionKind.TOOL, document)


def _publish(runtime: DbosRuntime, *revisions: PublishedRevision) -> None:
    with runtime.engine.begin() as connection:
        for revision in revisions:
            connection.execute(
                published_revisions.insert().values(
                    kind=revision.kind.value,
                    revision_hash=revision.revision_hash.value,
                    document=revision.document,
                )
            )


def _node(*tools: PublishedRevision) -> AgentNodeV3:
    return AgentNodeV3(
        type="agent",
        id="implement",
        role="builder",
        mode="headless",
        instruction="Do the one thing this chain is for.",
        tools=tuple(
            VersionedReference(ref=f"grant-{index}", revision=grant.revision_hash.value)
            for index, grant in enumerate(tools)
        ),
    )


@pytest.mark.proves("a-node-binds-one-exec-shaped-and-one-effect-shaped-grant-together")
def test_a_node_binds_one_exec_shaped_and_one_effect_shaped_grant_together(
    runtime: DbosRuntime,
) -> None:
    verification = _published_grant(ToolGrantCapability.RUN_PROJECT_VERIFICATION)
    push = _published_grant(
        ToolGrantCapability.PUSH_ATELIER_COMMIT,
        operation_revision=PUSH_OPERATION_REVISION,
    )
    _publish(runtime, verification, push)
    node = _node(verification, push)

    with runtime.engine.connect() as connection:
        exec_grant = read_pinned_exec_tool_grant(connection, node)
        effect_grant = read_pinned_effect_tool_grant(connection, node)

    assert exec_grant is not None
    assert exec_grant.capability is ToolGrantCapability.RUN_PROJECT_VERIFICATION
    assert effect_grant is not None
    assert effect_grant.capability is ToolGrantCapability.PUSH_ATELIER_COMMIT


def test_two_exec_shaped_grants_on_one_node_are_refused(runtime: DbosRuntime) -> None:
    verification = _published_grant(ToolGrantCapability.RUN_PROJECT_VERIFICATION)
    _publish(runtime, verification)
    node = _node(verification, verification)

    with (
        runtime.engine.connect() as connection,
        pytest.raises(RunBindingConflict, match="exec-shaped"),
    ):
        read_pinned_exec_tool_grant(connection, node)


def test_two_effect_shaped_grants_on_one_node_are_refused(runtime: DbosRuntime) -> None:
    push = _published_grant(
        ToolGrantCapability.PUSH_ATELIER_COMMIT,
        operation_revision=PUSH_OPERATION_REVISION,
    )
    open_pr = _published_grant(ToolGrantCapability.OPEN_PR)
    _publish(runtime, push, open_pr)
    node = _node(push, open_pr)

    with (
        runtime.engine.connect() as connection,
        pytest.raises(RunBindingConflict, match="effect-shaped"),
    ):
        read_pinned_effect_tool_grant(connection, node)
