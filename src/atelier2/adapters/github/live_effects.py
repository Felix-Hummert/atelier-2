"""Publication and readback for the `open-pr` adapter operation against live GitHub.

Same contract as the fake platform's `atelier2.adapters.github.effects`:
`readback` then `execute`, the request hash carried as a marker inside the
pull request's own body, and idempotency by that marker rather than by any
identifier GitHub assigns. What differs is the destination and one honest
consequence of it (ADR 0010 §5): a fake platform can list every pull request
it ever created, so a missing marker there is an authoritative absence. Live
GitHub offers no such inventory -- a pull request search is eventually
consistent -- so an unmatched search here can only report `EffectUnknownOutcome`,
never `EffectAbsence`. Reporting an absence this adapter cannot prove would be
exactly the `platform-absence-unprovable` case ADR 0010 §5 refuses.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import githubkit
import githubkit.exception
import httpx
from githubkit_schemas.latest.types import ReposOwnerRepoPullsPostBodyType

from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_markers import body_carries_request_hash, marker_line
from atelier2.contracts.effect_requests import OpenPullRequest
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
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


def _body_for(request: CanonicalRequest) -> str:
    try:
        body = OpenPullRequest.from_canonical_bytes(request.payload).body
    except (TypeError, ValueError):
        try:
            body = request.payload.decode("utf-8")
        except UnicodeDecodeError:
            body = request.payload.hex()
    return f"{body}\n\n{marker_line(request.request_hash.value)}\n"


def _branch_for(request: CanonicalRequest) -> str:
    try:
        return OpenPullRequest.from_canonical_bytes(request.payload).head_branch.value
    except (TypeError, ValueError):
        return f"atelier2-open-pr-{request.request_hash.value[:12]}"


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
        # A live-GitHub pull request search is eventually consistent, so a
        # not-found readback is `EffectUnknownOutcome`, never an authoritative
        # absence (ADR 0010 §5). The shared effect path durably moves that
        # outcome to reconciliation before an agent run can advance.
        return False

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
        return LiveGitHubEffectAdapter(client, self.repository, self.binding)


class LiveGitHubEffectAdapter:
    def __init__(
        self,
        client: githubkit.GitHub[githubkit.TokenAuthStrategy],
        repository: GitHubRepository,
        binding: EffectAdapterBinding,
    ) -> None:
        self._client = client
        self._repository = repository
        self._binding = binding
        self._closed = False

    def readback(self, intent: EffectIntent) -> EffectReadback:
        self._authorize_binding(intent)
        found = self._find_recorded_pull_request(intent)
        if found is None:
            return EffectUnknownOutcome(intent.reference)
        return self._receipt(intent, found)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        self._authorize_binding(intent)
        found = self._find_recorded_pull_request(intent)
        record = found if found is not None else self._create_pull_request(intent)
        return self._performed(record)

    def close(self) -> None:
        self._closed = True

    def _authorize_binding(self, intent: EffectIntent) -> None:
        self._require_open()
        if intent.binding.adapter_binding != self._binding:
            raise EffectIntentMismatch(
                "effect intent does not belong to this adapter binding"
            )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("github live effect adapter is closed")

    def _find_recorded_pull_request(
        self, intent: EffectIntent
    ) -> _RecordedPullRequest | None:
        branch = _branch_for(intent.request)
        response = self._client.rest.pulls.list(
            self._repository.owner,
            self._repository.name,
            head=f"{self._repository.owner}:{branch}",
            state="all",
        )
        matches = response.raw_response.json()
        if not isinstance(matches, list) or not matches:
            return None
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

    def _create_pull_request(self, intent: EffectIntent) -> _RecordedPullRequest:
        branch = _branch_for(intent.request)
        body = _body_for(intent.request)
        create_body: ReposOwnerRepoPullsPostBodyType = {
            "title": _title_for(body),
            "head": branch,
            "base": self._repository.base_branch,
            "body": body,
        }
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
            found = self._find_recorded_pull_request(intent)
            if found is None:
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
