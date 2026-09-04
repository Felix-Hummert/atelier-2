"""Publication and readback for the `open-pr` adapter operation against live GitHub.

Same contract as the fake platform's `atelier2.adapters.github.effects`:
`readback` then `execute`, the request hash carried as a marker inside the
pull request's own body, and idempotency by that marker rather than by any
identifier GitHub assigns.

What an unmatched read means here is decided by which read is used (#1210).
Listing pull requests by their exact head branch is not the eventually
consistent search index: it is a direct query about one branch, and a `200`
answering it with an empty list is GitHub's own statement that this branch
carries no pull request, in any state. That is an authoritative absence, and
reporting it as unknown was what made every live run wait for an operator
before it had sent anything at all. Only a read that failed -- a refused
status, a timeout, an answer that is not a list -- leaves the outcome unknown,
and then it carries what GitHub said. A false absence still cannot open a
twin: GitHub's own head+base uniqueness refuses the second create with `422`,
and this adapter converges on the winner.

The client is `githubkit` (ADR 0010 §7): typed request construction, retries,
TLS and pagination are its job, not this module's. This slice composes the
personal-access-token method only (ADR 0010 §2's low-friction path); the
GitHub App method stays unbuilt here.

The credential reaches this adapter by reference, never by value (ADR 0009 §6,
ADR 0010 §3), the same pattern `ClaudeSubscriptionSettings.credential_directory`
already uses: the durable settings hold a directory, and the token itself is
read from it once, at `open()`, and lives nowhere durable afterward -- not in
a lease, a receipt, an event, a log, or an API projection.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import githubkit
import githubkit.exception
import httpx
from githubkit_schemas.latest.types import ReposOwnerRepoPullsPostBodyType

from atelier2.adapters.github.effects import (
    GitHubEffectRefused,
    OpenPullRequestRequest,
    ReviewedDocumentationPublisher,
    ReviewedDocumentationPublisherFactory,
    open_pull_request,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_markers import body_carries_request_hash, marker_line
from atelier2.contracts.effect_requests import (
    ReviewedDocumentationPullRequest,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    ConfirmationSource,
    EffectAbsence,
    EffectAdapterBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentMismatch,
    EffectReadback,
    EffectReceipt,
    EffectResult,
    EffectUnknownOutcome,
    PerformedEffect,
    UnknownOutcomeReason,
)

GITHUB_TOKEN_CREDENTIAL_ENTRY = "token"

# GitHub's own head+base uniqueness constraint on pull requests: the second of
# the two create-time races. A concurrent execute can create both the branch
# and the pull request between this adapter's search and its own create calls,
# and this is the status that create then answers with -- the create-branch
# race's exact counterpart, and equally not a refusal.
_PULL_REQUEST_ALREADY_EXISTS_STATUS = 422

# GitHub does not document a hard title length limit; this is a defensive
# bound well under every limit third-party clients have observed, so a very
# long predecessor answer cannot make the create request itself malformed.
_MAXIMUM_PULL_REQUEST_TITLE_CHARACTERS = 256
_DEFAULT_PULL_REQUEST_TITLE = "Atelier open-pr"


class GitHubCredentialUnresolvable(RuntimeError):
    """The bound token credential reference does not resolve (`platform-credential-unresolvable`)."""


class GitHubUnexpectedResponse(RuntimeError):
    """A platform response did not carry the shape this operation reads."""


@dataclass(frozen=True)
class GitHubTokenCredential:
    """Where the adapter resolves the personal-access-token credential.

    Pattern: `ClaudeSubscriptionSettings.credential_directory`
    (`atelier2.adapters.claude_subscription`). The directory is a deployment
    value, resolved once when the adapter opens; the token it names is never
    copied into anything durable this adapter writes.
    """

    credential_directory: Path

    def resolve(self) -> str:
        token_path = self.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise GitHubCredentialUnresolvable(
                f"platform-credential-unresolvable: {token_path} did not "
                f"resolve a GitHub token: {error}"
            ) from error
        if not token:
            raise GitHubCredentialUnresolvable(
                f"platform-credential-unresolvable: {token_path} is empty"
            )
        return token


@dataclass(frozen=True)
class GitHubRepository:
    """The exact repository and base branch a connected project scopes to."""

    owner: str
    name: str
    base_branch: str

    def __post_init__(self) -> None:
        if not self.owner or not self.name or not self.base_branch:
            raise ValueError(
                "GitHubRepository requires a nonempty owner, name and base branch"
            )


@dataclass(frozen=True)
class _RecordedPullRequest:
    """One pull request this adapter found or created, as it names it."""

    branch: str
    pr_number: int
    body: str


@dataclass(frozen=True)
class _NoPullRequestOnBranch:
    """GitHub answered the head-branch listing, and it names no pull request."""


@dataclass(frozen=True)
class _PullRequestSearchFailed:
    """The listing itself did not resolve, so the branch's state is unknown."""

    reason: UnknownOutcomeReason


type _PullRequestSearch = (
    _RecordedPullRequest | _NoPullRequestOnBranch | _PullRequestSearchFailed
)


def _elapsed_milliseconds(started: float) -> int:
    return round((time.monotonic() - started) * 1_000)


def _refused_search(
    error: githubkit.exception.RequestError[Any], elapsed_milliseconds: int
) -> UnknownOutcomeReason:
    """Why a pull request listing did not resolve, in GitHub's own words.

    A refused request carries the status GitHub answered and the body it
    explained itself in; a timeout or a transport failure never reached a
    status at all, and then the client's own account of it is what there is.
    """

    if isinstance(error, githubkit.exception.RequestFailed):
        return UnknownOutcomeReason(
            error.response.status_code,
            elapsed_milliseconds,
            error.response.raw_response.text,
        )
    return UnknownOutcomeReason(None, elapsed_milliseconds, str(error.exc))


def _body_for(request: OpenPullRequestRequest, request_hash: str) -> str:
    return f"{request.body}\n\n{marker_line(request_hash)}\n"


def _title_for(body: str) -> str:
    for line in body.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate[:_MAXIMUM_PULL_REQUEST_TITLE_CHARACTERS]
    return _DEFAULT_PULL_REQUEST_TITLE


def _result_payload(branch: str, pr_number: int) -> bytes:
    return json.dumps(
        {"branch": branch, "pr_number": pr_number},
        separators=(",", ":"),
    ).encode("utf-8")


def _string_field(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GitHubUnexpectedResponse(
            f"{context} did not carry a {key!r} string field"
        )
    return value


def _integer_field(data: dict[str, Any], key: str, context: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise GitHubUnexpectedResponse(
            f"{context} did not carry an integer {key!r} field"
        )
    return value


@dataclass(frozen=True)
class LiveGitHubEffectAdapterFactory:
    """The host-composed factory for one live-GitHub `open-pr` adapter.

    `transport` is a test seam only (ADR 0010 §7's client is `githubkit`,
    which accepts an injectable `httpx` transport): production composition
    leaves it unset and reaches the real network. It is never a durable
    field a lease or receipt copies.
    """

    adapter_revision: AdapterRevision
    destination: EffectDestination
    repository: GitHubRepository
    token_credential: GitHubTokenCredential
    transport: httpx.BaseTransport | None = None
    documentation_publisher_factory: ReviewedDocumentationPublisherFactory | None = None

    @property
    def binding(self) -> EffectAdapterBinding:
        return EffectAdapterBinding(
            self.adapter_revision,
            self.destination,
            AdapterOperationalIdentity(
                f"{self.repository.owner}/{self.repository.name}"
            ),
            AdapterOperationName.OPEN_PR,
        )

    @property
    def proves_absence(self) -> bool:
        # Listing pull requests by their exact head branch is a direct query,
        # not the eventually consistent search index: a `200` with an empty
        # list is GitHub's own answer that this branch carries none (#1210).
        return True

    def open(self) -> LiveGitHubEffectAdapter:
        token = self.token_credential.resolve()
        client: githubkit.GitHub[githubkit.TokenAuthStrategy] = githubkit.GitHub(
            token,
            transport=self.transport,
            # A cached read could answer a retry's search from before an
            # earlier crashed attempt's create, which is exactly the twin
            # the readback-then-create rule exists to prevent.
            http_cache=False,
        )
        publisher = (
            None
            if self.documentation_publisher_factory is None
            else self.documentation_publisher_factory.open()
        )
        return LiveGitHubEffectAdapter(client, self.repository, self.binding, publisher)


class LiveGitHubEffectAdapter:
    def __init__(
        self,
        client: githubkit.GitHub[githubkit.TokenAuthStrategy],
        repository: GitHubRepository,
        binding: EffectAdapterBinding,
        documentation_publisher: ReviewedDocumentationPublisher | None,
    ) -> None:
        self._client = client
        self._repository = repository
        self._binding = binding
        self._documentation_publisher = documentation_publisher
        self._closed = False

    def readback(self, intent: EffectIntent) -> EffectReadback:
        request = self._authorized_request(intent)
        found = self._find_recorded_pull_request(intent, request)
        if isinstance(found, _PullRequestSearchFailed):
            return EffectUnknownOutcome(intent.reference, found.reason)
        if isinstance(found, _NoPullRequestOnBranch):
            return EffectAbsence(intent.reference)
        return self._receipt(intent, found)

    def execute(self, intent: EffectIntent) -> PerformedEffect | EffectUnknownOutcome:
        request = self._authorized_request(intent)
        found = self._find_recorded_pull_request(intent, request)
        if isinstance(found, _PullRequestSearchFailed):
            return EffectUnknownOutcome(intent.reference, found.reason)
        if isinstance(found, _RecordedPullRequest):
            return self._performed(found)
        if isinstance(request, ReviewedDocumentationPullRequest):
            self._verify_reviewed_base(request)
            if self._documentation_publisher is None:
                raise GitHubEffectRefused(
                    "reviewed documentation open-pr requires its push publisher"
                )
            self._documentation_publisher.publish(intent, request)
        record = self._create_pull_request(intent, request)
        return self._performed(record)

    def close(self) -> None:
        if self._documentation_publisher is not None:
            self._documentation_publisher.close()
        self._closed = True

    def _authorize_binding(self, intent: EffectIntent) -> None:
        self._require_open()
        if intent.binding.adapter_binding != self._binding:
            raise EffectIntentMismatch(
                "effect intent does not belong to this adapter binding"
            )

    def _authorized_request(self, intent: EffectIntent) -> OpenPullRequestRequest:
        self._authorize_binding(intent)
        return open_pull_request(intent.request)

    def _verify_reviewed_base(self, request: ReviewedDocumentationPullRequest) -> None:
        response = self._client.rest.repos.get_branch(
            self._repository.owner,
            self._repository.name,
            self._repository.base_branch,
        )
        branch = response.raw_response.json()
        if not isinstance(branch, dict):
            raise GitHubUnexpectedResponse(
                "base branch read did not return a branch object"
            )
        commit = branch.get("commit")
        if not isinstance(commit, dict):
            raise GitHubUnexpectedResponse(
                "base branch read did not return a commit object"
            )
        if _string_field(commit, "sha", "base branch commit") != request.base_revision:
            raise GitHubEffectRefused(
                "reviewed documentation base differs from the connected base branch"
            )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("github live effect adapter is closed")

    def _find_recorded_pull_request(
        self, intent: EffectIntent, request: OpenPullRequestRequest
    ) -> _PullRequestSearch:
        branch = request.head_branch.value
        started = time.monotonic()
        try:
            response = self._client.rest.pulls.list(
                self._repository.owner,
                self._repository.name,
                head=f"{self._repository.owner}:{branch}",
                state="all",
            )
        except githubkit.exception.RequestError as error:
            return _PullRequestSearchFailed(
                _refused_search(error, _elapsed_milliseconds(started))
            )
        raw_response = response.raw_response
        elapsed = _elapsed_milliseconds(started)
        try:
            matches: Any = raw_response.json()
        except ValueError:
            matches = None
        if not isinstance(matches, list):
            return _PullRequestSearchFailed(
                UnknownOutcomeReason(
                    raw_response.status_code, elapsed, raw_response.text
                )
            )
        if not matches:
            return _NoPullRequestOnBranch()
        pull_request = matches[0]
        if not isinstance(pull_request, dict):
            raise GitHubUnexpectedResponse(
                "pull request search did not return a pull request object"
            )
        number = _integer_field(pull_request, "number", "pull request search result")
        body = pull_request.get("body")
        body = body if isinstance(body, str) else ""
        self._verify_recorded_body(intent, body)
        return _RecordedPullRequest(branch, number, body)

    def _create_pull_request(
        self, intent: EffectIntent, request: OpenPullRequestRequest
    ) -> _RecordedPullRequest:
        branch = request.head_branch.value
        body = _body_for(request, intent.request.request_hash.value)
        create_body: ReposOwnerRepoPullsPostBodyType = {
            "title": (
                request.title
                if isinstance(request, ReviewedDocumentationPullRequest)
                else _title_for(body)
            ),
            "head": branch,
            "base": self._repository.base_branch,
            "body": body,
        }
        if isinstance(request, ReviewedDocumentationPullRequest):
            create_body["draft"] = request.draft
        try:
            response = self._client.rest.pulls.create(
                self._repository.owner,
                self._repository.name,
                data=create_body,
            )
        except githubkit.exception.RequestFailed as error:
            if error.response.status_code != _PULL_REQUEST_ALREADY_EXISTS_STATUS:
                raise
            # A concurrent execute created the pull request between this
            # attempt's search and this create; the same marker search
            # converges on its result rather than this attempt raising or
            # creating a twin GitHub's own constraint would have refused
            # anyway.
            found = self._find_recorded_pull_request(intent, request)
            if not isinstance(found, _RecordedPullRequest):
                raise
            return found
        created = response.raw_response.json()
        if not isinstance(created, dict):
            raise GitHubUnexpectedResponse(
                "pull request creation did not return a pull request object"
            )
        number = _integer_field(created, "number", "pull request creation result")
        return _RecordedPullRequest(branch, number, body)

    def _verify_recorded_body(self, intent: EffectIntent, body: str) -> None:
        request_hash = intent.request.request_hash.value
        if not body_carries_request_hash(body, request_hash):
            raise EffectIntentMismatch(
                "recorded pull request does not carry this request's marker"
            )

    def _performed(self, record: _RecordedPullRequest) -> PerformedEffect:
        return PerformedEffect(
            EffectId(str(record.pr_number)),
            EffectResult(_result_payload(record.branch, record.pr_number)),
        )

    def _receipt(
        self, intent: EffectIntent, record: _RecordedPullRequest
    ) -> EffectReceipt:
        return EffectReceipt(
            intent,
            EffectId(str(record.pr_number)),
            EffectResult(_result_payload(record.branch, record.pr_number)),
            ConfirmationSource.ADAPTER_READBACK,
        )
