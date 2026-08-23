"""The live-GitHub `open-pr` adapter: readback-then-create against `githubkit`.

No test here reaches the real network. Every request is answered by an
injected `httpx.MockTransport` standing in for a small in-memory GitHub, so
these are integration tests of this adapter's contract with `githubkit`, not
of GitHub itself. The real live run against an actual repository is an
operator-gated step this suite does not perform.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from atelier2.adapters.github.live_effects import (
    GitHubRepository,
    GitHubTokenCredential,
    LiveGitHubEffectAdapterFactory,
)
from atelier2.adapters.github.marker import body_carries_request_hash, marker_line
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectReceipt,
    EffectUnknownOutcome,
    LogicalEffectKey,
)
from atelier2.contracts.runs import RunId, WorkflowRevision

ADAPTER_REVISION = AdapterRevision("github-open-pr-live-v1")
DESTINATION = EffectDestination("github")
LOGICAL_KEY = LogicalEffectKey("run-1/open-pr")
OWNER = "atelier2-operator"
REPO = "atelier2-target"
BASE_BRANCH = "main"
BASE_SHA = "b" * 40
AGENT_OUTPUT = b"the predecessor agent's answer, published as the pull request body"
CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"


@dataclass
class _FakeGitHubServer:
    """The smallest stand-in for real GitHub these tests need.

    State lives in memory for one test only, and answers exactly the three
    calls the live adapter makes: the base branch's commit, listing pull
    requests by head branch, creating a branch ref, and creating a pull
    request.
    """

    owner: str
    repo: str
    base_branch: str
    base_sha: str
    pull_requests: list[dict[str, Any]] = field(default_factory=list)
    branches: set[str] = field(default_factory=set)
    branch_ref_attempts: int = 0
    _next_number: int = 1

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        prefix = f"/repos/{self.owner}/{self.repo}"
        if request.method == "GET" and path == f"{prefix}/branches/{self.base_branch}":
            return httpx.Response(
                200, json={"name": self.base_branch, "commit": {"sha": self.base_sha}}
            )
        if request.method == "GET" and path == f"{prefix}/pulls":
            head = request.url.params.get("head")
            matches = [
                pr
                for pr in self.pull_requests
                if head is None or pr["head"]["label"] == head
            ]
            return httpx.Response(200, json=matches)
        if request.method == "POST" and path == f"{prefix}/git/refs":
            self.branch_ref_attempts += 1
            payload = json.loads(request.content)
            branch = str(payload["ref"]).removeprefix("refs/heads/")
            if branch in self.branches:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.branches.add(branch)
            return httpx.Response(
                201,
                json={
                    "ref": payload["ref"],
                    "node_id": "REF_1",
                    "url": f"https://api.github.com{prefix}/git/refs/heads/{branch}",
                    "object": {
                        "sha": payload["sha"],
                        "type": "commit",
                        "url": "https://api.github.com/x",
                    },
                },
            )
        if request.method == "POST" and path == f"{prefix}/pulls":
            payload = json.loads(request.content)
            number = self._next_number
            self._next_number += 1
            pull_request = {
                "number": number,
                "body": payload.get("body", ""),
                "head": {"label": f"{self.owner}:{payload['head']}"},
            }
            self.pull_requests.append(pull_request)
            return httpx.Response(201, json=pull_request)
        return httpx.Response(
            404, json={"message": f"unhandled {request.method} {path}"}
        )


@pytest.fixture
def server() -> _FakeGitHubServer:
    return _FakeGitHubServer(OWNER, REPO, BASE_BRANCH, BASE_SHA)


@pytest.fixture
def factory(
    tmp_path: Path, server: _FakeGitHubServer
) -> LiveGitHubEffectAdapterFactory:
    credential_directory = tmp_path / "github-credential"
    credential_directory.mkdir()
    (credential_directory / "token").write_text(CANARY_TOKEN, encoding="utf-8")
    return LiveGitHubEffectAdapterFactory(
        ADAPTER_REVISION,
        DESTINATION,
        GitHubRepository(OWNER, REPO, BASE_BRANCH),
        GitHubTokenCredential(credential_directory),
        transport=httpx.MockTransport(server.handle),
    )


def effect_intent(payload: bytes = AGENT_OUTPUT) -> EffectIntent:
    return EffectIntent(
        EffectBinding(
            logical_key=LOGICAL_KEY,
            run_id=RunId("run-1"),
            workflow_revision_hash=WorkflowRevision(b"workflow-v1").revision_hash,
            adapter_revision=ADAPTER_REVISION,
            destination=DESTINATION,
            adapter_operational_identity=AdapterOperationalIdentity(f"{OWNER}/{REPO}"),
        ),
        CanonicalRequest(payload),
    )


def test_execute_creates_one_branch_and_one_pull_request_carrying_the_marker(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    intent = effect_intent()
    adapter = factory.open()
    try:
        performed = adapter.execute(intent)
    finally:
        adapter.close()

    assert len(server.pull_requests) == 1
    assert len(server.branches) == 1
    pull_request = server.pull_requests[0]
    body = str(pull_request["body"])
    assert body_carries_request_hash(body, intent.request.request_hash.value)
    assert marker_line(intent.request.request_hash.value) in body
    result = json.loads(performed.result.payload.decode("utf-8"))
    assert result["pr_number"] == pull_request["number"]
    assert performed.effect_id.value == str(pull_request["number"])
    assert CANARY_TOKEN not in body


def test_a_second_execute_finds_the_same_pull_request_and_does_not_create_a_twin(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    intent = effect_intent()
    adapter = factory.open()
    try:
        first = adapter.execute(intent)
        second = adapter.execute(intent)
        read = adapter.readback(intent)
    finally:
        adapter.close()

    assert first.effect_id == second.effect_id
    assert first.result == second.result
    assert len(server.pull_requests) == 1
    assert len(server.branches) == 1
    assert isinstance(read, EffectReceipt)
    assert read.effect_id == first.effect_id
    assert read.confirmation_source is ConfirmationSource.ADAPTER_READBACK


def test_readback_before_any_execute_is_unknown_never_an_authoritative_absence(
    factory: LiveGitHubEffectAdapterFactory,
) -> None:
    intent = effect_intent()
    adapter = factory.open()
    try:
        read = adapter.readback(intent)
    finally:
        adapter.close()

    assert isinstance(read, EffectUnknownOutcome)
    assert read.intent_reference == intent.reference


def test_no_token_appears_in_any_adapter_output(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    intent = effect_intent()
    adapter = factory.open()
    try:
        performed = adapter.execute(intent)
        read = adapter.readback(intent)
    finally:
        adapter.close()

    assert CANARY_TOKEN not in repr(factory)
    assert CANARY_TOKEN not in repr(performed)
    assert CANARY_TOKEN not in repr(read)
    assert CANARY_TOKEN.encode() not in performed.result.payload
    for pull_request in server.pull_requests:
        assert CANARY_TOKEN not in str(pull_request["body"])
