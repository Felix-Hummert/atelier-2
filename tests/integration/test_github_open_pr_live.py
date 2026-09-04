"""The live-GitHub `open-pr` adapter: readback-then-create against `githubkit`.

No test here reaches the real network. Every request is answered by an
injected `httpx.MockTransport` standing in for a small in-memory GitHub, so
these are integration tests of this adapter's contract with `githubkit`, not
of GitHub itself. The real live run against an actual repository is an
operator-gated step this suite does not perform.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from atelier2.adapters.github.composition import (
    GitHubConnectionUncomposable,
    live_github_effect_adapter_factory,
)
from atelier2.adapters.github.live_effects import (
    MAXIMUM_PULL_REQUEST_LISTING_PAGES,
    PULL_REQUESTS_PER_LISTING_PAGE,
    GitHubEffectRefused,
    LiveGitHubEffectAdapterFactory,
)
from atelier2.contracts.effect_markers import body_carries_request_hash, marker_line
from atelier2.contracts.effect_requests import (
    HeadBranch,
    OpenPullRequest,
    ReviewedDocumentationPullRequest,
    ReviewedDocumentReplacement,
)
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
    EffectUnknownOutcome,
    LogicalEffectKey,
    PerformedEffect,
    ReadbackPhase,
)
from atelier2.contracts.host_configuration import (
    ConnectionActor,
    ProjectId,
    ProjectSourceConnectionLifecycle,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
    SourceReference,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.contracts.secret_redaction import REDACTION_MARKER

ADAPTER_REVISION = AdapterRevision("github-open-pr-live-v1")
DESTINATION = EffectDestination("github")
LOGICAL_KEY = LogicalEffectKey("run-1/open-pr")
OWNER = "atelier2-operator"
REPO = "atelier2-target"
BASE_BRANCH = "main"
BASE_SHA = "b" * 40
AGENT_OUTPUT = b"the predecessor agent's answer, published as the pull request body"
HEAD_BRANCH = HeadBranch("atelier2/work-item/" + "a" * 64)
CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"


@dataclass
class _FakeGitHubServer:
    """The smallest stand-in for real GitHub these tests need.

    State lives in memory for one test only, and answers exactly the four
    calls the live adapter makes: the base branch's commit, listing pull
    requests by head branch, creating a branch ref, and creating a pull
    request. It enforces GitHub's own head+base uniqueness constraint on pull
    requests -- a second create for the same head answers `422`, the same
    status a duplicate branch ref answers with -- because that is exactly the
    race the concurrent-execute test below exercises.
    """

    owner: str
    repo: str
    base_branch: str
    base_sha: str
    pull_requests: list[dict[str, Any]] = field(default_factory=list)
    branches: set[str] = field(default_factory=set)
    http_calls: int = 0
    branch_ref_attempts: int = 0
    stale_pull_request_searches: int = 0
    """How many of the next searches answer as if the branch were still empty."""

    pull_request_searches: int = 0

    pull_request_search_answer: httpx.Response | None = None
    _next_number: int = 1

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.http_calls += 1
        path = request.url.path
        prefix = f"/repos/{self.owner}/{self.repo}"
        if request.method == "GET" and path == f"{prefix}/branches/{self.base_branch}":
            return httpx.Response(
                200, json={"name": self.base_branch, "commit": {"sha": self.base_sha}}
            )
        if request.method == "GET" and path == f"{prefix}/pulls":
            self.pull_request_searches += 1
            if self.pull_request_search_answer is not None:
                return self.pull_request_search_answer
            if self.stale_pull_request_searches > 0:
                self.stale_pull_request_searches -= 1
                return httpx.Response(200, json=[])
            head = request.url.params.get("head")
            matches = [
                pr
                for pr in self.pull_requests
                if head is None or pr["head"]["label"] == head
            ]
            return httpx.Response(200, json=self._page(request, matches))
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
            head_label = f"{self.owner}:{payload['head']}"
            if any(
                pr["head"]["label"] == head_label
                and pr["base"]["ref"] == payload["base"]
                for pr in self.pull_requests
            ):
                return httpx.Response(
                    422,
                    json={
                        "message": f"A pull request already exists for {payload['head']}."
                    },
                )
            number = self._next_number
            self._next_number += 1
            pull_request = {
                "number": number,
                "body": payload.get("body", ""),
                "title": payload["title"],
                "draft": payload.get("draft", False),
                "head": {"label": head_label},
                "base": {"ref": payload["base"]},
            }
            self.pull_requests.append(pull_request)
            return httpx.Response(201, json=pull_request)
        return httpx.Response(
            404, json={"message": f"unhandled {request.method} {path}"}
        )

    def _page(
        self, request: httpx.Request, matches: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """The slice of the listing GitHub answers for the requested page."""

        per_page = int(request.url.params.get("per_page", len(matches) + 1))
        page = int(request.url.params.get("page", 1))
        start = (page - 1) * per_page
        return matches[start : start + per_page]


@pytest.fixture
def server() -> _FakeGitHubServer:
    return _FakeGitHubServer(
        OWNER, REPO, BASE_BRANCH, BASE_SHA, branches={HEAD_BRANCH.value}
    )


def connection_revision(
    credential_directory: Path,
    source_kind: str = "github",
    source_address: str = f"{OWNER}/{REPO}",
    source_ref: str | None = BASE_BRANCH,
) -> ProjectSourceConnectionRevision:
    return ProjectSourceConnectionRevision(
        ProjectId("studio"),
        ProjectSourceId("11111111-1111-1111-1111-111111111111"),
        1,
        SourceKind(source_kind),
        SourceAddress(source_address),
        credential_directory,
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("felix"),
        ProjectSourceConnectionLifecycle.CONNECTED,
        None,
        None if source_ref is None else SourceReference(source_ref),
    )


@pytest.fixture
def factory(
    tmp_path: Path, server: _FakeGitHubServer
) -> LiveGitHubEffectAdapterFactory:
    """The factory exactly as serve composes it: from the connection record.

    The record's opaque address is decoded by the adapter's own composition
    entry, so every behavior below runs against the record-composed path; the
    transport seam is attached afterward because a record never carries one.
    """

    credential_directory = tmp_path / "github-credential"
    credential_directory.mkdir()
    (credential_directory / "token").write_text(CANARY_TOKEN, encoding="utf-8")
    composed = live_github_effect_adapter_factory(
        connection_revision(credential_directory),
        ADAPTER_REVISION,
        DESTINATION,
    )
    return replace(composed, transport=httpx.MockTransport(server.handle))


def test_the_record_composed_factory_names_the_connected_repository(
    factory: LiveGitHubEffectAdapterFactory,
) -> None:
    assert factory.binding.operational_identity == AdapterOperationalIdentity(
        f"{OWNER}/{REPO}"
    )
    assert factory.proves_absence is True


def test_a_foreign_source_kind_does_not_compose_the_github_factory(
    tmp_path: Path,
) -> None:
    with pytest.raises(GitHubConnectionUncomposable, match="source kind"):
        live_github_effect_adapter_factory(
            connection_revision(tmp_path, source_kind="gitlab"),
            ADAPTER_REVISION,
            DESTINATION,
        )


@pytest.mark.parametrize(
    "address",
    [
        "acme/studio",
        "acme@main",
        "/studio@main",
        "acme/@main",
        "acme/studio@",
        "acme/studio/extra@main",
    ],
)
def test_an_address_outside_the_owner_name_base_branch_grammar_is_refused(
    tmp_path: Path, address: str
) -> None:
    with pytest.raises(GitHubConnectionUncomposable, match="owner/name"):
        live_github_effect_adapter_factory(
            connection_revision(tmp_path, source_address=address, source_ref=None),
            ADAPTER_REVISION,
            DESTINATION,
        )


def test_a_base_branch_may_itself_carry_slashes_and_at_signs(
    tmp_path: Path,
) -> None:
    # Git allows both in branch names while the repository identity does not,
    # so the adapter-owned ref detail carries the branch verbatim.
    composed = live_github_effect_adapter_factory(
        connection_revision(
            tmp_path,
            source_address="acme/studio",
            source_ref="release/v1@rc",
        ),
        ADAPTER_REVISION,
        DESTINATION,
    )

    assert composed.repository.base_branch == "release/v1@rc"


def test_a_v45_address_with_an_embedded_branch_is_durable_corruption(
    tmp_path: Path,
) -> None:
    with pytest.raises(GitHubConnectionUncomposable, match="owner/name"):
        live_github_effect_adapter_factory(
            connection_revision(
                tmp_path,
                source_address=f"{OWNER}/{REPO}@release/v1@rc",
                source_ref=None,
            ),
            ADAPTER_REVISION,
            DESTINATION,
        )


def published(outcome: PerformedEffect | EffectUnknownOutcome) -> PerformedEffect:
    """What an execute performed; an unknown outcome is this test's failure."""

    assert isinstance(outcome, PerformedEffect), outcome
    return outcome


def effect_intent(payload: bytes = AGENT_OUTPUT, *, typed: bool = True) -> EffectIntent:
    request_payload = (
        OpenPullRequest(payload.decode("utf-8"), HEAD_BRANCH).canonical_bytes()
        if typed
        else payload
    )
    return EffectIntent(
        EffectBinding(
            logical_key=LOGICAL_KEY,
            run_id=RunId("run-1"),
            workflow_revision_hash=WorkflowRevision(b"workflow-v1").revision_hash,
            adapter_revision=ADAPTER_REVISION,
            destination=DESTINATION,
            adapter_operational_identity=AdapterOperationalIdentity(f"{OWNER}/{REPO}"),
        ),
        CanonicalRequest(request_payload),
    )


@dataclass
class _RecordingDocumentationPublisher:
    requests: list[ReviewedDocumentationPullRequest] = field(default_factory=list)

    def publish(
        self, intent: EffectIntent, request: ReviewedDocumentationPullRequest
    ) -> None:
        self.requests.append(request)

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class _DocumentationPublisherFactory:
    publisher: _RecordingDocumentationPublisher

    def open(self) -> _RecordingDocumentationPublisher:
        return self.publisher


def reviewed_documentation_intent() -> EffectIntent:
    request = ReviewedDocumentationPullRequest(
        BASE_SHA,
        "c" * 64,
        "d" * 64,
        (
            ReviewedDocumentReplacement(
                "docs/PRODUCT.md", "e" * 64, b"exact replacement\n"
            ),
        ),
        "Reviewed documentation",
        "The independently approved replacement.",
        HEAD_BRANCH,
        draft=True,
    )
    original = effect_intent()
    return EffectIntent(original.binding, CanonicalRequest(request.canonical_bytes()))


def test_a_reviewed_release_pushes_exact_content_then_uses_its_typed_pr_fields(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    publisher = _RecordingDocumentationPublisher()
    reviewed_factory = replace(
        factory,
        documentation_publisher_factory=_DocumentationPublisherFactory(publisher),
    )
    intent = reviewed_documentation_intent()
    request = ReviewedDocumentationPullRequest.from_canonical_bytes(
        intent.request.payload
    )
    adapter = reviewed_factory.open()
    try:
        adapter.execute(intent)
    finally:
        adapter.close()

    assert publisher.requests == [request]
    assert len(server.pull_requests) == 1
    pull_request = server.pull_requests[0]
    assert request.base_revision == server.base_sha
    assert pull_request["base"] == {"ref": BASE_BRANCH}
    assert pull_request["title"] == request.title
    assert request.draft is True
    assert pull_request["draft"] is True
    assert request.body in str(pull_request["body"])


def test_a_reviewed_release_refuses_when_its_typed_base_is_not_the_bound_base(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    publisher = _RecordingDocumentationPublisher()
    reviewed_factory = replace(
        factory,
        documentation_publisher_factory=_DocumentationPublisherFactory(publisher),
    )
    original = reviewed_documentation_intent()
    request = ReviewedDocumentationPullRequest.from_canonical_bytes(
        original.request.payload
    )
    mismatched = replace(request, base_revision="f" * 40)
    intent = EffectIntent(
        original.binding, CanonicalRequest(mismatched.canonical_bytes())
    )
    adapter = reviewed_factory.open()
    try:
        with pytest.raises(GitHubEffectRefused, match="base differs"):
            adapter.execute(intent)
    finally:
        adapter.close()

    assert publisher.requests == []
    assert server.pull_requests == []


def test_execute_uses_the_push_created_branch_and_opens_one_marked_pull_request(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    intent = effect_intent()
    adapter = factory.open()
    try:
        performed = published(adapter.execute(intent))
    finally:
        adapter.close()

    assert len(server.pull_requests) == 1
    assert server.branches == {HEAD_BRANCH.value}
    assert server.branch_ref_attempts == 0
    pull_request = server.pull_requests[0]
    body = str(pull_request["body"])
    assert body_carries_request_hash(body, intent.request.request_hash.value)
    assert marker_line(intent.request.request_hash.value) in body
    result = json.loads(performed.result.payload.decode("utf-8"))
    assert result["pr_number"] == pull_request["number"]
    assert result["branch"] == HEAD_BRANCH.value
    assert performed.effect_id.value == str(pull_request["number"])
    assert CANARY_TOKEN not in body


def test_a_second_execute_finds_the_same_pull_request_and_does_not_create_a_twin(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    intent = effect_intent()
    adapter = factory.open()
    try:
        first = published(adapter.execute(intent))
        second = published(adapter.execute(intent))
        read = adapter.readback(intent, ReadbackPhase.AFTER_SEND)
    finally:
        adapter.close()

    assert first.effect_id == second.effect_id
    assert first.result == second.result
    assert len(server.pull_requests) == 1
    assert len(server.branches) == 1
    assert server.branch_ref_attempts == 0
    assert isinstance(read, EffectReceipt)
    assert read.effect_id == first.effect_id
    assert read.confirmation_source is ConfirmationSource.ADAPTER_READBACK


def test_execute_converges_on_a_concurrently_created_pull_request_instead_of_raising(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    """A concurrent execute can win both the branch-ref and the pull-request
    race between this attempt's own search and its own create call. GitHub's
    head+base uniqueness constraint refuses the twin create with 422, and this attempt must
    converge on the concurrent winner's pull request rather than raising or
    creating a twin.
    """
    intent = effect_intent()
    adapter = factory.open()
    try:
        winner = published(adapter.execute(intent))

        # This attempt's own search misses the pull request the "concurrent"
        # execute above already created, so it proceeds to create and hits the
        # pull request race GitHub's own uniqueness constraint refuses.
        server.stale_pull_request_searches = 1
        loser = published(adapter.execute(intent))
    finally:
        adapter.close()

    assert loser.effect_id == winner.effect_id
    assert loser.result == winner.result
    assert len(server.pull_requests) == 1
    assert len(server.branches) == 1
    assert server.branch_ref_attempts == 0


def test_a_branch_carrying_no_pull_request_is_an_absence_that_licenses_the_create(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    intent = effect_intent()
    adapter = factory.open()
    try:
        read = adapter.readback(intent, ReadbackPhase.BEFORE_SEND)
        performed = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(read, EffectAbsence)
    assert read.intent_reference == intent.reference
    assert isinstance(performed, PerformedEffect)
    assert len(server.pull_requests) == 1


@pytest.mark.parametrize(
    ("answer", "failure_code", "detail"),
    [
        pytest.param(
            httpx.Response(500, json={"message": "Server Error"}),
            500,
            "Server Error",
            id="refused-status",
        ),
        pytest.param(
            httpx.Response(200, json={"message": "not a listing"}),
            200,
            "not a listing",
            id="unreadable-listing",
        ),
        pytest.param(
            httpx.Response(201, json=[]),
            201,
            "[]",
            id="empty-listing-under-another-status",
        ),
    ],
)
def test_a_listing_that_did_not_resolve_creates_nothing_and_keeps_what_github_said(
    factory: LiveGitHubEffectAdapterFactory,
    server: _FakeGitHubServer,
    answer: httpx.Response,
    failure_code: int,
    detail: str,
) -> None:
    server.pull_request_search_answer = answer
    intent = effect_intent()
    adapter = factory.open()
    try:
        read = adapter.readback(intent, ReadbackPhase.BEFORE_SEND)
        performed = adapter.execute(intent)
    finally:
        adapter.close()

    assert server.pull_requests == []
    for outcome in (read, performed):
        assert isinstance(outcome, EffectUnknownOutcome)
        assert outcome.reason is not None
        assert outcome.reason.failure_code == failure_code
        assert detail in outcome.reason.detail
        assert outcome.reason.duration_milliseconds >= 0


def _decoy_pull_request(number: int, base: str) -> dict[str, Any]:
    """Another pull request standing on the same head branch, not this request's."""

    return {
        "number": number,
        "body": f"an earlier attempt onto {base}\n",
        "title": "decoy",
        "draft": False,
        "head": {"label": f"{OWNER}:{HEAD_BRANCH.value}"},
        "base": {"ref": base},
    }


def test_the_marker_decides_which_of_a_branch_s_pull_requests_is_this_request_s(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    """Several pull requests may stand on one head, over more than one page."""

    intent = effect_intent()
    adapter = factory.open()
    try:
        performed = published(adapter.execute(intent))
        server.pull_requests = [
            _decoy_pull_request(number, f"release/{number}")
            for number in range(1000, 1000 + PULL_REQUESTS_PER_LISTING_PAGE)
        ] + server.pull_requests
        searches_before_the_marker_moved = server.pull_request_searches
        read = adapter.readback(intent, ReadbackPhase.BEFORE_SEND)
    finally:
        adapter.close()

    assert isinstance(read, EffectReceipt)
    assert read.effect_id == performed.effect_id
    assert server.pull_request_searches - searches_before_the_marker_moved == 2


def test_a_listing_that_never_ends_is_unknown_rather_than_a_loop_nobody_sees(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    server.pull_requests = [
        _decoy_pull_request(number, f"release/{number}")
        for number in range(
            PULL_REQUESTS_PER_LISTING_PAGE * MAXIMUM_PULL_REQUEST_LISTING_PAGES + 1
        )
    ]
    intent = effect_intent()
    adapter = factory.open()
    try:
        read = adapter.readback(intent, ReadbackPhase.BEFORE_SEND)
    finally:
        adapter.close()

    assert isinstance(read, EffectUnknownOutcome)
    assert read.reason is not None
    assert "did not end within" in read.reason.detail
    assert server.pull_request_searches == MAXIMUM_PULL_REQUEST_LISTING_PAGES


def test_a_branch_whose_pull_requests_are_all_foreign_licenses_no_create(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    server.pull_requests = [_decoy_pull_request(1000, "release/1000")]
    intent = effect_intent()
    adapter = factory.open()
    try:
        read = adapter.readback(intent, ReadbackPhase.BEFORE_SEND)
        performed = adapter.execute(intent)
    finally:
        adapter.close()

    assert len(server.pull_requests) == 1
    for outcome in (read, performed):
        assert isinstance(outcome, EffectUnknownOutcome)
        assert outcome.reason is not None
        assert "none of them this request's" in outcome.reason.detail


def test_an_empty_listing_after_a_create_attempt_is_unknown_and_opens_no_twin(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    """The listing that has not caught up cannot license a second create."""

    intent = effect_intent()
    adapter = factory.open()
    try:
        published(adapter.execute(intent))
        server.stale_pull_request_searches = 1
        read = adapter.readback(intent, ReadbackPhase.AFTER_SEND)
    finally:
        adapter.close()

    assert isinstance(read, EffectUnknownOutcome)
    assert read.reason is not None
    assert read.reason.failure_code == 200
    assert "a create was already attempted" in read.reason.detail
    assert len(server.pull_requests) == 1


def test_a_listing_still_stale_after_the_uniqueness_refusal_reports_unknown(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    """A create GitHub refused, whose winner stays unreadable, is unknown.

    The request was sent, so its outcome belongs to the operator's
    reconciliation rather than to an exception thrown over a sent request.
    """

    intent = effect_intent()
    adapter = factory.open()
    try:
        winner = published(adapter.execute(intent))
        server.stale_pull_request_searches = 2
        outcome = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(outcome, EffectUnknownOutcome)
    assert outcome.reason is not None
    assert outcome.reason.failure_code == 422
    assert len(server.pull_requests) == 1
    assert server.pull_requests[0]["number"] == int(winner.effect_id.value)


def test_a_credential_github_echoed_never_reaches_the_kept_reason(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    echoed = "ghp_" + "s" * 36
    server.pull_request_search_answer = httpx.Response(
        401, json={"message": f"Bad credentials: {echoed}"}
    )
    intent = effect_intent()
    adapter = factory.open()
    try:
        read = adapter.readback(intent, ReadbackPhase.BEFORE_SEND)
    finally:
        adapter.close()

    assert isinstance(read, EffectUnknownOutcome)
    assert read.reason is not None
    assert echoed not in read.reason.detail
    assert REDACTION_MARKER in read.reason.detail
    assert server.pull_requests == []


@pytest.mark.parametrize("operation", ["readback", "execute"])
def test_an_untyped_open_pr_payload_fails_loud_before_any_github_call(
    factory: LiveGitHubEffectAdapterFactory,
    server: _FakeGitHubServer,
    operation: str,
    malformed_open_pr_payload: bytes,
) -> None:
    intent = effect_intent(malformed_open_pr_payload, typed=False)
    adapter = factory.open()
    reaching_the_destination = {
        "readback": lambda: adapter.readback(intent, ReadbackPhase.BEFORE_SEND),
        "execute": lambda: adapter.execute(intent),
    }
    try:
        with pytest.raises(GitHubEffectRefused, match="canonical open-pr request"):
            reaching_the_destination[operation]()
    finally:
        adapter.close()

    assert server.pull_requests == []
    assert server.http_calls == 0


def test_no_token_appears_in_any_adapter_output(
    factory: LiveGitHubEffectAdapterFactory, server: _FakeGitHubServer
) -> None:
    intent = effect_intent()
    adapter = factory.open()
    try:
        performed = published(adapter.execute(intent))
        read = adapter.readback(intent, ReadbackPhase.AFTER_SEND)
    finally:
        adapter.close()

    assert CANARY_TOKEN not in repr(factory)
    assert CANARY_TOKEN not in repr(performed)
    assert CANARY_TOKEN not in repr(read)
    assert CANARY_TOKEN.encode() not in performed.result.payload
    for pull_request in server.pull_requests:
        assert CANARY_TOKEN not in str(pull_request["body"])
