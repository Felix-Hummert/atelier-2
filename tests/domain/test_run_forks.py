from __future__ import annotations

from dataclasses import replace

import pytest

from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    DeclaredContextPackageHash,
    NodeReceiptHash,
)
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.run_forks import (
    RunFork,
    RunForkCommandId,
    RunForkEffectFence,
    RunForkReusedNode,
    successor_run_id_for,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash


def test_run_fork_v1_has_published_identity_vectors() -> None:
    origin = RunId("origin/run-7")
    revision = WorkflowRevisionHash("11" * 32)
    command = RunForkCommandId.for_request(origin, "retry-key-9")
    successor = successor_run_id_for(command)
    reused = RunForkReusedNode(
        "prepare",
        1,
        origin,
        revision,
        NodeExecutionId.for_node(origin, revision, "prepare"),
        Sha256Hash("22" * 32),
        NodeReceiptHash("33" * 32),
        DeclaredContextPackageHash("44" * 32),
        Sha256Hash("55" * 32),
    )
    fence = RunForkEffectFence(
        "publish",
        1,
        LogicalEffectKey("effect/origin/publish"),
        origin,
        revision,
        Sha256Hash("66" * 32),
    )

    fork = RunFork(
        command,
        origin,
        Sha256Hash("77" * 32),
        successor,
        revision,
        RunConfigurationRevisionHash("88" * 32),
        "publish",
        (reused,),
        (fence,),
    )

    assert (
        command.value
        == "01af841274212e8815bef21df8c146a23314a6b56c84ce4587a1ee9a3fed587c"
    )
    assert (
        successor.value
        == "78da5b94ac7559b1e1663f1e3f019ab765ac070e189a1a1ab7c4721915d650a0"
    )
    assert (
        fork.fork_hash.value
        == "6db95e06036a33016d7ea1ad0fde723f90ac8d05bdfee11d17f359ad57122d28"
    )

    changed_command = RunForkCommandId.for_request(origin, "retry-key-10")
    changed_reused_node = replace(
        reused,
        node_id="prepare-again",
        source_node_execution_id=NodeExecutionId.for_node(
            origin, revision, "prepare-again"
        ),
    )
    changed_source_run = RunId("origin/run-8")
    changed_reused_source = replace(
        reused,
        source_run_id=changed_source_run,
        source_node_execution_id=NodeExecutionId.for_node(
            changed_source_run, revision, reused.node_id
        ),
    )
    changed_revision = WorkflowRevisionHash("12" * 32)
    changed_reused_revision = replace(
        reused,
        source_workflow_revision_hash=changed_revision,
        source_node_execution_id=NodeExecutionId.for_node(
            origin, changed_revision, reused.node_id
        ),
    )
    variants = (
        replace(
            fork,
            command_id=changed_command,
            successor_run_id=successor_run_id_for(changed_command),
        ),
        replace(fork, origin_run_id=RunId("other-origin")),
        replace(fork, origin_terminal_hash=Sha256Hash("78" * 32)),
        replace(fork, workflow_revision_hash=WorkflowRevisionHash("13" * 32)),
        replace(
            fork,
            run_configuration_revision_hash=RunConfigurationRevisionHash("89" * 32),
        ),
        replace(fork, restart_from_node_id="publish-again"),
        replace(fork, reused_nodes=(changed_reused_node,)),
        replace(
            fork,
            reused_nodes=(
                replace(
                    reused,
                    round_ordinal=2,
                    source_node_execution_id=NodeExecutionId.for_node(
                        origin, revision, reused.node_id, 2
                    ),
                ),
            ),
        ),
        replace(fork, reused_nodes=(changed_reused_source,)),
        replace(fork, reused_nodes=(changed_reused_revision,)),
        replace(
            fork,
            reused_nodes=(replace(reused, source_event_hash=Sha256Hash("23" * 32)),),
        ),
        replace(
            fork,
            reused_nodes=(
                replace(reused, source_receipt_hash=NodeReceiptHash("34" * 32)),
            ),
        ),
        replace(
            fork,
            reused_nodes=(
                replace(
                    reused,
                    source_declared_context_package_hash=DeclaredContextPackageHash(
                        "45" * 32
                    ),
                ),
            ),
        ),
        replace(
            fork,
            reused_nodes=(
                replace(reused, source_agent_receipt_hash=Sha256Hash("56" * 32)),
            ),
        ),
        replace(fork, effect_fences=(replace(fence, node_id="publish-again"),)),
        replace(fork, effect_fences=(replace(fence, round_ordinal=2),)),
        replace(
            fork,
            effect_fences=(
                replace(
                    fence, source_logical_key=LogicalEffectKey("effect/origin/other")
                ),
            ),
        ),
        replace(
            fork, effect_fences=(replace(fence, source_run_id=RunId("other-origin")),)
        ),
        replace(
            fork,
            effect_fences=(
                replace(
                    fence, source_workflow_revision_hash=WorkflowRevisionHash("14" * 32)
                ),
            ),
        ),
        replace(
            fork,
            effect_fences=(replace(fence, source_result_hash=Sha256Hash("67" * 32)),),
        ),
        replace(fork, reused_nodes=()),
        replace(fork, effect_fences=()),
    )

    assert len({variant.fork_hash for variant in variants}) == len(variants)
    assert all(variant.fork_hash != fork.fork_hash for variant in variants)


def test_run_fork_rejects_an_unowned_successor_and_duplicate_evidence() -> None:
    origin = RunId("origin")
    revision = WorkflowRevisionHash("11" * 32)
    command = RunForkCommandId.for_request(origin, "key")
    reused = RunForkReusedNode(
        "a",
        1,
        origin,
        revision,
        NodeExecutionId.for_node(origin, revision, "a"),
        Sha256Hash("22" * 32),
        NodeReceiptHash("33" * 32),
        DeclaredContextPackageHash("44" * 32),
    )

    with pytest.raises(ValueError, match="successor"):
        RunFork(
            command,
            origin,
            Sha256Hash("55" * 32),
            RunId("caller-picked"),
            revision,
            RunConfigurationRevisionHash("66" * 32),
            "b",
            (),
            (),
        )
    with pytest.raises(ValueError, match="reuses each"):
        RunFork(
            command,
            origin,
            Sha256Hash("55" * 32),
            successor_run_id_for(command),
            revision,
            RunConfigurationRevisionHash("66" * 32),
            "b",
            (reused, reused),
            (),
        )


def test_command_identity_requires_a_nonempty_retry_key() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        RunForkCommandId.for_request(RunId("origin"), "")
