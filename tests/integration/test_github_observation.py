"""The live-GitHub issue observation source, driven through `githubkit`.

Same harness idea as `test_github_open_pr_live`: no test reaches the real
network; an injected `httpx.MockTransport` stands in for GitHub's issue
endpoints, so these are integration tests of this source's contract with
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
    ProjectSourceId,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
)
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.ports.issue_observation import (
    ObserveWorkItemRevisionResult,
    OpenTrackerItemsObserved,
    TrackerItemUnknown,
    TrackerPayloadMalformed,
    TrackerSourceUnavailable,
    WorkItemRevisionObserved,
)

OWNER = "atelier2-operator"
REPO = "atelier2-target"
CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"
READ_AT = RecordedAt("2026-08-26T09:15:00Z")


@dataclass
class _FakeGitHubIssues:
    """The two endpoints this source calls: the paged open listing, and one issue."""

    owner: str
    repo: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    status: int = 200
    override_payload: Any = None
    override_content: bytes | None = None
    raise_transport_error: bool = False
    entity_tag: str | None = 'W/"3f1a9c"'
    requested_paths: list[str] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.raise_transport_error:
            raise httpx.ConnectError("the network is down", request=request)
        path = request.url.path
        self.requested_paths.append(path)
        collection = f"/repos/{self.owner}/{self.repo}/issues"
        if request.method == "GET" and path == collection:
            return self._listing(request)
        if request.method == "GET" and path.startswith(f"{collection}/"):
            return self._one_issue(path.removeprefix(f"{collection}/"))
        return httpx.Response(
            404, json={"message": f"unhandled {request.method} {path}"}
        )

    def _listing(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "refused"})
        if self.override_payload is not None:
            return httpx.Response(200, json=self.override_payload)
        per_page = int(request.url.params.get("per_page", "30"))
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * per_page
        return httpx.Response(200, json=self.issues[start : start + per_page])

    def _one_issue(self, number: str) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "refused"})
        headers = {} if self.entity_tag is None else {"ETag": self.entity_tag}
        if self.override_content is not None:
            # Raw bytes, because a payload this test needs -- an escaped lone
            # surrogate -- is one no JSON encoder here would produce.
            return httpx.Response(
                200,
                content=self.override_content,
                headers={**headers, "Content-Type": "application/json"},
            )
        if self.override_payload is not None:
            return httpx.Response(200, json=self.override_payload, headers=headers)
        for issue in self.issues:
            if str(issue["number"]) == number:
                return httpx.Response(200, json=issue, headers=headers)
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def github() -> _FakeGitHubIssues:
    return _FakeGitHubIssues(OWNER, REPO)


def connection_revision(
    credential_directory: Path,
    source_kind: str = "github",
) -> ProjectSourceConnectionRevision:
    return ProjectSourceConnectionRevision(
        ProjectId("studio"),
        ProjectSourceId("11111111-1111-1111-1111-111111111111"),
        1,
        SourceKind(source_kind),
        SourceAddress(f"{OWNER}/{REPO}@main"),
        credential_directory,
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("felix"),
    )


@pytest.fixture
def source(tmp_path: Path, github: _FakeGitHubIssues) -> LiveGitHubIssueSource:
    """The source exactly as serve composes it: from the connection record."""

    credential_directory = tmp_path / "github-credential"
    credential_directory.mkdir()
    (credential_directory / "token").write_text(CANARY_TOKEN, encoding="utf-8")
    composed = live_github_issue_source(connection_revision(credential_directory))
    return replace(
        composed,
        transport=httpx.MockTransport(github.handle),
        clock=lambda: READ_AT,
    )


def test_open_issues_become_gh_prefixed_tracker_references(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.issues = [{"number": 79}, {"number": 652}]

    observed = source.open_items()

    assert observed == OpenTrackerItemsObserved(
        (TrackerItemReference("gh:79"), TrackerItemReference("gh:652"))
    )


def test_pull_requests_in_the_issue_listing_are_not_work_items(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.issues = [
        {"number": 1},
        {"number": 2, "pull_request": {"url": "https://api.github.com/x"}},
        {"number": 3},
    ]

    observed = source.open_items()

    assert observed == OpenTrackerItemsObserved(
        (TrackerItemReference("gh:1"), TrackerItemReference("gh:3"))
    )


def test_observation_walks_every_page_of_a_large_listing(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.issues = [{"number": number} for number in range(1, 251)]

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
    github: _FakeGitHubIssues,
    payload: Any,
) -> None:
    github.override_payload = payload

    observed = source.open_items()

    assert isinstance(observed, TrackerPayloadMalformed)


def test_a_refusing_platform_answer_is_unavailable_naming_the_status(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.status = 403

    observed = source.open_items()

    assert isinstance(observed, TrackerSourceUnavailable)
    assert "403" in observed.detail


def test_an_unreachable_platform_is_unavailable_not_an_exception(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.raise_transport_error = True

    observed = source.open_items()

    assert isinstance(observed, TrackerSourceUnavailable)


def test_a_missing_credential_is_unavailable_and_leaks_no_token(
    tmp_path: Path, github: _FakeGitHubIssues
) -> None:
    empty_directory = tmp_path / "github-credential"
    empty_directory.mkdir()
    composed = live_github_issue_source(connection_revision(empty_directory))
    source = replace(composed, transport=httpx.MockTransport(github.handle))

    observed = source.open_items()

    assert isinstance(observed, TrackerSourceUnavailable)
    assert "platform-credential-unresolvable" in observed.detail


def test_no_token_appears_in_any_observation_output(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.issues = [{"number": 79}]

    observed = source.open_items()

    assert CANARY_TOKEN not in repr(source)
    assert CANARY_TOKEN not in repr(observed)


def test_a_foreign_source_kind_does_not_compose_the_issue_source(
    tmp_path: Path,
) -> None:
    with pytest.raises(GitHubConnectionUncomposable, match="source kind"):
        live_github_issue_source(connection_revision(tmp_path, source_kind="gitlab"))


def observed_revision(
    answer: ObserveWorkItemRevisionResult,
) -> ObservedWorkItemRevision:
    assert isinstance(answer, WorkItemRevisionObserved), answer
    return answer.revision


def test_a_snapshot_carries_the_served_body_bytes_untouched(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    """The bytes GitHub served, exactly: no normalization, nothing appended.

    A body with a carriage return, a non-ASCII character and no trailing
    newline is the shape a normalizing read would quietly change, which is what
    ADR 0010 §5's canonical rule forbids.
    """

    body = "Erste Zeile\r\n\r\nZweite Zeile — ohne Schlusszeilenumbruch"
    github.issues = [{"number": 712, "body": body}]

    revision = observed_revision(source.snapshot(TrackerItemReference("gh:712")))

    assert revision.body == body.encode("utf-8")


def test_a_snapshot_names_its_item_its_kind_and_the_reads_provenance(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.issues = [{"number": 712, "body": "text"}]
    github.entity_tag = 'W/"c0ffee"'

    revision = observed_revision(source.snapshot(TrackerItemReference("gh:712")))

    assert revision.item == TrackerItemReference("gh:712")
    assert revision.kind is WorkItemKind.ISSUE
    assert revision.change_marker == WorkItemChangeMarker('W/"c0ffee"')
    assert revision.observed_at == READ_AT


def test_a_pull_request_is_read_as_a_change_request(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    """The one platform mapping this slice proves: GitHub PR reads as neutral kind.

    The listing refuses pull requests because the queue observes work items;
    reading one by name is a different question, and the answer carries no
    GitHub word into the core.
    """

    github.issues = [
        {
            "number": 761,
            "body": "the change",
            "pull_request": {"url": "https://api.github.com/x"},
        }
    ]

    revision = observed_revision(source.snapshot(TrackerItemReference("gh:761")))

    assert revision.kind is WorkItemKind.CHANGE_REQUEST
    assert "pull" not in repr(revision)


def test_an_item_without_a_body_is_read_as_empty_bytes(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.issues = [{"number": 712, "body": None}]

    revision = observed_revision(source.snapshot(TrackerItemReference("gh:712")))

    assert revision.body == b""


def test_an_item_the_tracker_does_not_hold_is_unknown(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.issues = [{"number": 712, "body": "text"}]

    answer = source.snapshot(TrackerItemReference("gh:99999"))

    assert answer == TrackerItemUnknown(TrackerItemReference("gh:99999"))


@pytest.mark.parametrize("reference", ["gl:!12", "gh:", "gh:zero", "gh:0", "712"])
def test_a_reference_this_source_cannot_address_is_unknown_without_a_request(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues, reference: str
) -> None:
    answer = source.snapshot(TrackerItemReference(reference))

    assert answer == TrackerItemUnknown(TrackerItemReference(reference))
    assert github.requested_paths == []


@pytest.mark.parametrize(
    "payload",
    [
        "not an object",
        {"number": 712},
        {"number": 712, "body": 3},
        {"body": "no number at all"},
    ],
)
def test_a_snapshot_payload_shape_this_source_refuses_is_typed_malformed(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues, payload: Any
) -> None:
    github.override_payload = payload

    answer = source.snapshot(TrackerItemReference("gh:712"))

    assert isinstance(answer, TrackerPayloadMalformed)


def test_an_answer_about_another_item_is_refused_rather_than_pinned(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    """Otherwise one item's bytes would be pinned under another item's name."""

    github.override_payload = {"number": 713, "body": "the other item's text"}

    answer = source.snapshot(TrackerItemReference("gh:712"))

    assert isinstance(answer, TrackerPayloadMalformed)


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (b'{"number": 712, "body": "unclosed', "truncated JSON"),
        (b"\xff\xfe not even text", "bytes that are not UTF-8"),
        (b"", "an empty answer"),
    ],
    ids=["truncated-json", "undecodable-bytes", "empty-answer"],
)
def test_an_answer_that_does_not_decode_is_refused_rather_than_raised(
    source: LiveGitHubIssueSource,
    github: _FakeGitHubIssues,
    content: bytes,
    why: str,
) -> None:
    """A typed port promises a typed refusal, including for the encoding itself."""

    github.override_content = content

    answer = source.snapshot(TrackerItemReference("gh:712"))

    assert isinstance(answer, TrackerPayloadMalformed), why


@pytest.mark.parametrize(
    "number",
    [True, 712.0, "712", None],
    ids=["boolean-that-equals-one", "integral-float", "text", "absent"],
)
def test_an_item_number_that_is_not_an_integer_is_refused(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues, number: Any
) -> None:
    """`True == 1` and `1.0 == 1` in Python; neither is an item GitHub served."""

    github.override_payload = {"number": number, "body": "text"}

    answer = source.snapshot(TrackerItemReference("gh:1"))

    assert isinstance(answer, TrackerPayloadMalformed)


def test_a_body_that_is_not_encodable_as_utf8_is_refused_by_name(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    """An escaped lone surrogate decodes as text and encodes as nothing.

    JSON admits `\\ud800` unpaired, so a provider answer can carry text no
    UTF-8 encoder accepts. This source promises a typed refusal for a payload
    it will not read, and that promise has to hold here rather than raise.
    """

    github.override_content = b'{"number": 712, "body": "lone \\ud800 surrogate"}'

    answer = source.snapshot(TrackerItemReference("gh:712"))

    assert isinstance(answer, TrackerPayloadMalformed)


def test_a_read_without_a_change_marker_is_refused_rather_than_invented(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.issues = [{"number": 712, "body": "text"}]
    github.entity_tag = None

    answer = source.snapshot(TrackerItemReference("gh:712"))

    assert isinstance(answer, TrackerPayloadMalformed)


def test_a_refusing_platform_answer_to_a_snapshot_names_its_status(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.status = 403

    answer = source.snapshot(TrackerItemReference("gh:712"))

    assert isinstance(answer, TrackerSourceUnavailable)
    assert "403" in answer.detail


def test_an_unreachable_platform_leaves_a_snapshot_unavailable(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.raise_transport_error = True

    answer = source.snapshot(TrackerItemReference("gh:712"))

    assert isinstance(answer, TrackerSourceUnavailable)


def test_a_missing_credential_leaves_a_snapshot_unavailable_and_leaks_no_token(
    tmp_path: Path, github: _FakeGitHubIssues
) -> None:
    empty_directory = tmp_path / "github-credential"
    empty_directory.mkdir()
    composed = live_github_issue_source(connection_revision(empty_directory))
    source = replace(composed, transport=httpx.MockTransport(github.handle))

    answer = source.snapshot(TrackerItemReference("gh:712"))

    assert isinstance(answer, TrackerSourceUnavailable)
    assert "platform-credential-unresolvable" in answer.detail


def test_no_token_appears_in_a_snapshot_answer(
    source: LiveGitHubIssueSource, github: _FakeGitHubIssues
) -> None:
    github.issues = [{"number": 712, "body": "text"}]

    answer = source.snapshot(TrackerItemReference("gh:712"))

    assert CANARY_TOKEN not in repr(answer)
