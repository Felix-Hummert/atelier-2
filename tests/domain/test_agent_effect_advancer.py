"""Redeeming an agent node's own `open-pr` grant through the shared adapter.

`#431` Phase 2 lets an agent node open a pull request as a declared grant rather
than through a separate Action node. The redemption drives the very
`EffectAdapter` the Action already drives, so what an operator reads back is one
effect however it was authorized: a readback runs before create, so a redemption
retried after the pull request already exists finds it rather than opening a
twin, and an `UNKNOWN` readback -- which only live GitHub can report and this
fake never does -- confirms nothing and is refused loud.

The prepared intent these tests redeem is the same one an Action prepares,
reused here through the V1 action scenario, because what `redeem_agent_open_pr`
loads is a PREPARED effect intent by its logical key -- how that key was derived
is the intent-preparation's concern, proved end to end in the integration slice.
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
    _effect_shaped_capability_to_open_pr,
    redeem_agent_open_pr,
)
from atelier2.adapters.dbos.effect_store import receipt_from_record
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents, effect_receipts, runs
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.adapters.github.marker import body_carries_request_hash
from atelier2.contracts.effects import (
    AdapterRevision,
    ConfirmationSource,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    EffectReceipt,
    EffectUnknownOutcome,
    PerformedEffect,
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
from tests.scenarios.agents import commit_configured_agent
from tests.scenarios.runs import prepare_graph_action, start_published_v1_run
from tests.scenarios.runtime import exact_output_runtime

WORKFLOW_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: waiting, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: waiting}
  - {id: agent, type: agent, job: job-17, output: draft-17, next: action}
"""
REVISION = WorkflowRevision(WORKFLOW_DOCUMENT)
RUN_ID = RunId("run-open-pr")


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
    runtime = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "executor-A"), github
    )
    runtime.initialize_storage()
    start_published_v1_run(runtime.engine, runtime.settings, RUN_ID, REVISION)
    with canonical_write_transaction(runtime.engine) as connection:
        commit_configured_agent(connection, RUN_ID, REVISION.revision_hash, "agent")
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

    def readback(self, intent: EffectIntent) -> EffectReadback:
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


def test_open_pr_is_the_only_effect_shaped_capability() -> None:
    """The one owner of the exec-versus-effect split answers for both members."""
    assert redeems_as_platform_effect(ToolGrantCapability.OPEN_PR) is True
    assert (
        redeems_as_platform_effect(ToolGrantCapability.RUN_PROJECT_VERIFICATION)
        is False
    )


def _grant(capability: ToolGrantCapability) -> DeclaredToolGrant:
    return DeclaredToolGrant(
        PublishedRevisionHash(Sha256Hash.of(b"any-tool-revision").value), capability
    )


def test_an_open_pr_grant_is_the_capability_this_preparation_opens() -> None:
    """An `open-pr` grant is the one effect-shaped grant this preparation performs."""
    assert (
        _effect_shaped_capability_to_open_pr(_grant(ToolGrantCapability.OPEN_PR))
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
    assert _effect_shaped_capability_to_open_pr(grant) is None


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
        _effect_shaped_capability_to_open_pr(_grant(unredeemed))

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
        redeem_agent_open_pr(
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
        redeem_agent_open_pr(
            connection,
            github.open(),
            intent.binding.logical_key.value,
            intent.binding.workflow_revision_hash.value,
        )
    first = _receipts(runtime)[0]

    adapter = github.open()
    try:
        readback = adapter.readback(intent)
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
        state = redeem_agent_open_pr(
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
