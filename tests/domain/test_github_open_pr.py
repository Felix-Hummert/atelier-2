"""The GitHub adapter's open-pr effect: readback-then-create, one PR, no twin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atelier2.adapters.github.effects import (
    GitHubEffectAdapterFactory,
    GitHubEffectRefused,
)
from atelier2.contracts.effect_markers import body_carries_request_hash, marker_line
from atelier2.contracts.effect_requests import HeadBranch, OpenPullRequest
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectAbsence,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectReceipt,
    LogicalEffectKey,
)
from atelier2.contracts.runs import RunId, WorkflowRevision

ADAPTER_REVISION = AdapterRevision("github-open-pr-v1")
DESTINATION = EffectDestination("platform")
LOGICAL_KEY = LogicalEffectKey("run-1/open-pr")
TREE = json.dumps({"files": {"hello.txt": "from the builder"}}).encode("utf-8")
HEAD_BRANCH = HeadBranch("atelier2/work-item/" + "a" * 64)
CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"


@pytest.fixture
def factory(tmp_path: Path) -> GitHubEffectAdapterFactory:
    return GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        ADAPTER_REVISION,
        DESTINATION,
    )


def effect_intent(factory: GitHubEffectAdapterFactory) -> EffectIntent:
    return EffectIntent(
        EffectBinding(
            logical_key=LOGICAL_KEY,
            run_id=RunId("run-1"),
            workflow_revision_hash=WorkflowRevision(b"workflow-v1").revision_hash,
            adapter_revision=ADAPTER_REVISION,
            destination=DESTINATION,
            adapter_operational_identity=AdapterOperationalIdentity(
                str(factory.database_path.resolve())
            ),
        ),
        CanonicalRequest(
            OpenPullRequest(TREE.decode("utf-8"), HEAD_BRANCH).canonical_bytes()
        ),
    )


def test_execute_records_one_pull_request_with_the_request_hash_in_its_body(
    factory: GitHubEffectAdapterFactory,
) -> None:
    intent = effect_intent(factory)
    adapter = factory.open()
    try:
        assert isinstance(adapter.readback(intent), EffectAbsence)
        performed = adapter.execute(intent)
    finally:
        adapter.close()

    recorded = factory.recorded_pull_requests()
    assert len(recorded) == 1
    pull_request = recorded[0]
    result = json.loads(performed.result.payload.decode("utf-8"))
    assert result == {
        "branch": pull_request.branch,
        "pr_number": pull_request.pr_number,
    }
    assert pull_request.pr_number == 1
    assert pull_request.branch == HEAD_BRANCH.value
    assert body_carries_request_hash(
        pull_request.body, intent.request.request_hash.value
    )
    assert marker_line(intent.request.request_hash.value) in pull_request.body
    assert CANARY_TOKEN not in pull_request.body
    assert CANARY_TOKEN.encode() not in performed.result.payload
    assert CANARY_TOKEN.encode() not in factory.database_path.read_bytes()


def test_a_second_execute_finds_the_same_pull_request_and_does_not_create_a_twin(
    factory: GitHubEffectAdapterFactory,
) -> None:
    intent = effect_intent(factory)
    adapter = factory.open()
    try:
        first = adapter.execute(intent)
        second = adapter.execute(intent)
        read = adapter.readback(intent)
    finally:
        adapter.close()

    assert first.effect_id == second.effect_id
    assert first.result == second.result
    assert isinstance(read, EffectReceipt)
    assert read.effect_id == first.effect_id
    assert read.confirmation_source is ConfirmationSource.ADAPTER_READBACK
    assert factory.recorded_pull_requests()[0].pr_number == 1
    assert len(factory.recorded_pull_requests()) == 1


@pytest.mark.parametrize("operation", ["readback", "execute"])
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not an open-pr request", id="utf8-body"),
        pytest.param(b"\xff", id="non-utf8-body"),
        pytest.param(b'{"body":"missing head"}', id="incomplete-object"),
    ],
)
def test_a_malformed_open_pr_payload_is_refused_before_the_recorded_adapter_writes(
    factory: GitHubEffectAdapterFactory,
    operation: str,
    payload: bytes,
) -> None:
    original = effect_intent(factory)
    intent = EffectIntent(original.binding, CanonicalRequest(payload))
    adapter = factory.open()
    try:
        with pytest.raises(GitHubEffectRefused, match="canonical open-pr request"):
            getattr(adapter, operation)(intent)
    finally:
        adapter.close()

    assert factory.recorded_pull_requests() == ()
