"""The live-GitHub issue observation source, driven through `githubkit`.

Same harness idea as `test_github_open_pr_live`: no test reaches the real
network; an injected `httpx.MockTransport` stands in for GitHub's issue
listing, so these are integration tests of this source's contract with
`githubkit`, not of GitHub itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from atelier2.adapters.github.composition import (
    GitHubConnectionUncomposable,
    live_github_issue_source,
)
from atelier2.adapters.github.observation import LiveGitHubIssueSource
from atelier2.contracts.host_configuration import (
    ConnectionActor,
    ProjectId,
    ProjectSourceConnectionRevision,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
)
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.ports.issue_observation import (
    OpenTrackerItemsObserved,
    TrackerPayloadMalformed,
    TrackerSourceUnavailable,
)

OWNER = "atelier2-operator"
REPO = "atelier2-target"
CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"


@dataclass
class _FakeGitHubIssueListing:
    """The one endpoint this source calls: the paged open-issue listing."""

    owner: str
    repo: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    status: int = 200
    override_payload: Any = None
    raise_transport_error: bool = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.raise_transport_error:
            raise httpx.ConnectError("the network is down", request=request)
        path = request.url.path
        if (
            request.method == "GET"
            and path == f"/repos/{self.owner}/{self.repo}/issues"
        ):
            if self.status != 200:
                return httpx.Response(self.status, json={"message": "refused"})
            if self.override_payload is not None:
                return httpx.Response(200, json=self.override_payload)
            per_page = int(request.url.params.get("per_page", "30"))
            page = int(request.url.params.get("page", "1"))
            start = (page - 1) * per_page
            return httpx.Response(200, json=self.issues[start : start + per_page])
        return httpx.Response(
            404, json={"message": f"unhandled {request.method} {path}"}
        )


@pytest.fixture
def listing() -> _FakeGitHubIssueListing:
    return _FakeGitHubIssueListing(OWNER, REPO)


def connection_revision(
    credential_directory: Path,
    source_kind: str = "github",
) -> ProjectSourceConnectionRevision:
    return ProjectSourceConnectionRevision(
        ProjectId("studio"),
        1,
        SourceKind(source_kind),
        SourceAddress(f"{OWNER}/{REPO}@main"),
        credential_directory,
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("felix"),
    )


@pytest.fixture
def source(tmp_path: Path, listing: _FakeGitHubIssueListing) -> LiveGitHubIssueSource:
    """The source exactly as serve composes it: from the connection record."""

    credential_directory = tmp_path / "github-credential"
    credential_directory.mkdir()
    (credential_directory / "token").write_text(CANARY_TOKEN, encoding="utf-8")
    composed = live_github_issue_source(connection_revision(credential_directory))
    return replace(composed, transport=httpx.MockTransport(listing.handle))


def test_open_issues_become_gh_prefixed_tracker_references(
    source: LiveGitHubIssueSource, listing: _FakeGitHubIssueListing
) -> None:
    listing.issues = [{"number": 79}, {"number": 652}]

    observed = source.open_items()

    assert observed == OpenTrackerItemsObserved(
        (TrackerItemReference("gh:79"), TrackerItemReference("gh:652"))
    )


def test_pull_requests_in_the_issue_listing_are_not_work_items(
    source: LiveGitHubIssueSource, listing: _FakeGitHubIssueListing
) -> None:
    listing.issues = [
        {"number": 1},
        {"number": 2, "pull_request": {"url": "https://api.github.com/x"}},
        {"number": 3},
    ]

    observed = source.open_items()

    assert observed == OpenTrackerItemsObserved(
        (TrackerItemReference("gh:1"), TrackerItemReference("gh:3"))
    )


def test_observation_walks_every_page_of_a_large_listing(
    source: LiveGitHubIssueSource, listing: _FakeGitHubIssueListing
) -> None:
    listing.issues = [{"number": number} for number in range(1, 251)]

    observed = source.open_items()

    assert isinstance(observed, OpenTrackerItemsObserved)
    assert len(observed.references) == 250
    assert observed.references[0] == TrackerItemReference("gh:1")
    assert observed.references[-1] == TrackerItemReference("gh:250")


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "not a list"},
        ["not an object"],
        [{"title": "no number"}],
        [{"number": "79"}],
        [{"number": 0}],
        [{"number": True}],
    ],
)
def test_a_payload_shape_this_source_refuses_is_a_typed_malformed_answer(
    source: LiveGitHubIssueSource,
    listing: _FakeGitHubIssueListing,
    payload: Any,
) -> None:
    listing.override_payload = payload

    observed = source.open_items()

    assert isinstance(observed, TrackerPayloadMalformed)


def test_a_refusing_platform_answer_is_unavailable_naming_the_status(
    source: LiveGitHubIssueSource, listing: _FakeGitHubIssueListing
) -> None:
    listing.status = 403

    observed = source.open_items()

    assert isinstance(observed, TrackerSourceUnavailable)
    assert "403" in observed.detail


def test_an_unreachable_platform_is_unavailable_not_an_exception(
    source: LiveGitHubIssueSource, listing: _FakeGitHubIssueListing
) -> None:
    listing.raise_transport_error = True

    observed = source.open_items()

    assert isinstance(observed, TrackerSourceUnavailable)


def test_a_missing_credential_is_unavailable_and_leaks_no_token(
    tmp_path: Path, listing: _FakeGitHubIssueListing
) -> None:
    empty_directory = tmp_path / "github-credential"
    empty_directory.mkdir()
    composed = live_github_issue_source(connection_revision(empty_directory))
    source = replace(composed, transport=httpx.MockTransport(listing.handle))

    observed = source.open_items()

    assert isinstance(observed, TrackerSourceUnavailable)
    assert "platform-credential-unresolvable" in observed.detail


def test_no_token_appears_in_any_observation_output(
    source: LiveGitHubIssueSource, listing: _FakeGitHubIssueListing
) -> None:
    listing.issues = [{"number": 79}]

    observed = source.open_items()

    assert CANARY_TOKEN not in repr(source)
    assert CANARY_TOKEN not in repr(observed)


def test_a_foreign_source_kind_does_not_compose_the_issue_source(
    tmp_path: Path,
) -> None:
    with pytest.raises(GitHubConnectionUncomposable, match="source kind"):
        live_github_issue_source(connection_revision(tmp_path, source_kind="gitlab"))
