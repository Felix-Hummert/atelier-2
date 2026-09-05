"""Redeeming an agent node's own `open-pr` grant through the shared adapter.

`#431` Phase 2 lets an agent node open a pull request as a declared grant rather
than through a separate Action node. The redemption drives the very
`EffectAdapter` the Action already drives, so what an operator reads back is one
effect however it was authorized: a readback runs before create, so a redemption
retried after the pull request already exists finds it rather than opening a
twin, and an `UNKNOWN` readback -- which only live GitHub can report and this
fake never does -- confirms nothing and is refused loud.

The prepared intent these tests redeem is the same one an Action prepares,
reused here through the V3 effect-line scenario, because what
`redeem_agent_effect` loads is a PREPARED effect intent by its logical key --
how that key was derived is the intent-preparation's concern, proved end to end
in the integration slice.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos import agent_effect_grants
from atelier2.adapters.dbos.advancer import (
    redeem_agent_effect,
)
from atelier2.adapters.dbos.agent_effect_grants import open_pr_capability_for
from atelier2.adapters.dbos.effect_store import receipt_from_record
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import effect_intents, effect_receipts, runs
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.contracts.effect_markers import body_carries_request_hash
from atelier2.contracts.effects import (
    AdapterRevision,
    ConfirmationSource,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    EffectReceipt,
    EffectUnknownOutcome,
    PerformedEffect,
    ReadbackPhase,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    ToolGrantAccepted,
    ToolGrantCapability,
    ToolGrantCapabilityNotRedeemed,
    read_tool_grant_document,
    redeems_as_platform_effect,
)
from atelier2.contracts.workflows_v3 import VersionedReference
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.durable_state import canonical_runtime_settings
from tests.scenarios.runs import (
    complete_v3_agent_node,
    prepare_graph_action,
    publish_pinned_revisions,
    start_published_v3_run,
)
from tests.scenarios.runtime import recording_exact_runtime
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    OPEN_PR_OPERATION,
    V3_EFFECT_LINE_AGENT_JOB,
    V3_EFFECT_LINE_AGENT_NODE_ID,
    V3_EFFECT_LINE_DOCUMENT,
)

REVISION = WorkflowRevision(V3_EFFECT_LINE_DOCUMENT)
RUN_ID = RunId("run-open-pr")
PROVIDER_OUTPUT = b'"draft-17"'


@pytest.fixture
def prepared(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, GitHubEffectAdapterFactory, EffectIntent]]:
    """A run with one PREPARED pull-request intent, bound to the fake platform."""
    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    runtime = recording_exact_runtime(
        canonical_runtime_settings(
            tmp_path, "executor-A", agent_scratch_root(tmp_path)
        ),
        github,
        PROVIDER_OUTPUT,
    )
    runtime.initialize_storage()
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    start_published_v3_run(
        runtime.engine,
        runtime.settings,
        RUN_ID,
        REVISION,
        runtime.agent_executor_registry,
    )
    complete_v3_agent_node(
        runtime,
        RUN_ID,
        V3_EFFECT_LINE_AGENT_NODE_ID,
        V3_EFFECT_LINE_AGENT_JOB,
        PROVIDER_OUTPUT,
    )
    intent = prepare_graph_action(
        runtime.engine, RUN_ID, REVISION.revision_hash, github.binding
    ).intent
    try:
        yield runtime, github, intent
    finally:
        runtime.close()


@dataclass
class _ScriptedAdapter:
    """An adapter whose readback this test dictates and whose execute it forbids."""

    readback_result: EffectReadback
    execute_calls: int = field(default=0, init=False)

    def readback(self, intent: EffectIntent, phase: ReadbackPhase) -> EffectReadback:
        del intent
        return self.readback_result

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        del intent
        self.execute_calls += 1
        raise AssertionError("an unknown readback must not license an execute")

    def close(self) -> None:
        return None


def _receipts(runtime: DbosRuntime) -> list[EffectReceipt]:
    with runtime.engine.connect() as connection:
        return [
            receipt_from_record(record)
            for record in connection.execute(sa.select(effect_receipts)).mappings()
        ]


def test_the_external_effect_capabilities_share_the_effect_redemption_shape() -> None:
    assert redeems_as_platform_effect(ToolGrantCapability.OPEN_PR) is True
    assert redeems_as_platform_effect(ToolGrantCapability.PUSH_ATELIER_COMMIT) is True
    assert (
        redeems_as_platform_effect(ToolGrantCapability.RUN_PROJECT_VERIFICATION)
        is False
    )


def _grant(capability: ToolGrantCapability) -> DeclaredToolGrant:
    operation = (
        VersionedReference(ref="push", revision="a1" * 32)
        if capability is ToolGrantCapability.PUSH_ATELIER_COMMIT
        else None
    )
    return DeclaredToolGrant(
        PublishedRevisionHash(Sha256Hash.of(b"any-tool-revision").value),
        capability,
        operation,
    )


def test_an_open_pr_grant_is_the_capability_this_preparation_opens() -> None:
    """An `open-pr` grant is the one effect-shaped grant this preparation performs."""
    assert (
        open_pr_capability_for(_grant(ToolGrantCapability.OPEN_PR))
        is ToolGrantCapability.OPEN_PR
    )


@pytest.mark.parametrize(
    "grant",
    [
        pytest.param(None, id="no-grant"),
        pytest.param(
            _grant(ToolGrantCapability.RUN_PROJECT_VERIFICATION), id="exec-shaped"
        ),
    ],
)
def test_a_missing_or_exec_shaped_grant_prepares_no_open_pr_effect(
    grant: DeclaredToolGrant | None,
) -> None:
    """Only an effect-shaped grant prepares a pull request; the rest prepare nothing."""
    assert open_pr_capability_for(grant) is None


def test_the_push_grant_is_handled_by_its_sibling_preparation() -> None:
    assert (
        open_pr_capability_for(_grant(ToolGrantCapability.PUSH_ATELIER_COMMIT)) is None
    )


def test_an_effect_shaped_capability_that_is_not_open_pr_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future effect-shaped member the open-pr preparation cannot perform fails loud.

    Only `OPEN_PR` is effect-shaped today, so a stand-in effect member is forced
    by classifying `RUN_PROJECT_VERIFICATION` as effect-shaped for this test: the
    preparation must refuse an effect-shaped capability it does not perform by
    name rather than drop it as an unprepared intent."""
    monkeypatch.setattr(
        agent_effect_grants, "redeems_as_platform_effect", lambda _capability: True
    )
    unredeemed = ToolGrantCapability.RUN_PROJECT_VERIFICATION

    with pytest.raises(ToolGrantCapabilityNotRedeemed) as refused:
        open_pr_capability_for(_grant(unredeemed))

    assert refused.value.capability is unredeemed


def test_a_published_open_pr_document_is_a_grant_this_runtime_redeems() -> None:
    """The capability that authorizes the effect is admitted where it is declared."""
    document = json.dumps({"capability": ToolGrantCapability.OPEN_PR.value}).encode(
        "utf-8"
    )

    assert read_tool_grant_document(document) == ToolGrantAccepted(
        ToolGrantCapability.OPEN_PR
    )


def test_a_granted_agent_opens_one_pull_request_through_the_shared_adapter(
    prepared: tuple[DbosRuntime, GitHubEffectAdapterFactory, EffectIntent],
) -> None:
    """Redeeming the prepared intent opens exactly one pull request, with a receipt."""
    runtime, github, intent = prepared

    with canonical_write_transaction(runtime.engine) as connection:
        redeem_agent_effect(
            connection,
            github.open(),
            intent.binding.logical_key.value,
            intent.binding.workflow_revision_hash.value,
        )

    recorded = github.recorded_pull_requests()
    assert len(recorded) == 1
    assert body_carries_request_hash(
        recorded[0].body, intent.request.request_hash.value
    )
    receipts = _receipts(runtime)
    assert len(receipts) == 1
    assert receipts[0].intent == intent
    assert receipts[0].confirmation_source is ConfirmationSource.ADAPTER_EXECUTION


def test_a_redemption_after_the_pull_request_exists_finds_it_rather_than_a_twin(
    prepared: tuple[DbosRuntime, GitHubEffectAdapterFactory, EffectIntent],
) -> None:
    """The derived logical key readback-matches the first pull request, opening no
    second one -- idempotency by readback-then-create, not by a durable guess."""
    runtime, github, intent = prepared

    with canonical_write_transaction(runtime.engine) as connection:
        redeem_agent_effect(
            connection,
            github.open(),
            intent.binding.logical_key.value,
            intent.binding.workflow_revision_hash.value,
        )
    first = _receipts(runtime)[0]

    adapter = github.open()
    try:
        readback = adapter.readback(intent, ReadbackPhase.AFTER_SEND)
    finally:
        adapter.close()

    assert isinstance(readback, EffectReceipt)
    assert readback.confirmation_source is ConfirmationSource.ADAPTER_READBACK
    assert readback.effect_id == first.effect_id
    assert len(github.recorded_pull_requests()) == 1


def test_an_unknown_readback_waits_for_reconciliation_and_writes_no_receipt(
    prepared: tuple[DbosRuntime, GitHubEffectAdapterFactory, EffectIntent],
) -> None:
    """No source can decide, so nothing is opened and nothing is confirmed."""
    runtime, _github, intent = prepared
    adapter = _ScriptedAdapter(EffectUnknownOutcome(intent.reference))

    with canonical_write_transaction(runtime.engine) as connection:
        state = redeem_agent_effect(
            connection,
            adapter,
            intent.binding.logical_key.value,
            intent.binding.workflow_revision_hash.value,
        )

    assert state == "WAITING_RECONCILIATION"
    assert adapter.execute_calls == 0
    assert _receipts(runtime) == []
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(effect_intents.c.state).where(
                    effect_intents.c.logical_key == intent.binding.logical_key.value
                )
            )
            == "WAITING_RECONCILIATION"
        )
        assert (
            connection.scalar(
                sa.select(runs.c.state).where(runs.c.run_id == RUN_ID.value)
            )
            == "WAITING_RECONCILIATION"
        )
