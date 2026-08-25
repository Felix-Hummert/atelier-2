"""Observing the connected repository's open issues as tracker item references.

The thin observation surface of #652: list the open issues of the repository
the project-source connection record names, and answer them in the reference
grammar this package owns -- `gh:<n>`, the same spelling the queue's admission
door already receives (ADR 0010: what a tracker reference means is the
connected platform adapter's contract). Nothing else of an issue crosses this
boundary; titles, bodies, and labels stay GitHub's (REQ-QUEUE-14).

Provider output is external input: every payload shape this module reads is
validated, and a shape it refuses becomes a typed `TrackerPayloadMalformed`
rather than an unvalidated answer on its way to a durable write. The token
reaches GitHub by credential-directory reference exactly as the live `open-pr`
adapter's does (ADR 0009 §6) and lives nowhere durable afterward.

A durable cursor, conditional reads, and a poll loop are named deferrals of
#652 slice 1; every observation reads the full open list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import githubkit
import githubkit.exception
import httpx

from atelier2.adapters.github.live_effects import (
    GitHubCredentialUnresolvable,
    GitHubRepository,
    GitHubTokenCredential,
)
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.ports.issue_observation import (
    ObserveOpenTrackerItemsResult,
    OpenTrackerItemsObserved,
    TrackerPayloadMalformed,
    TrackerSourceUnavailable,
)

GITHUB_TRACKER_REFERENCE_PREFIX = "gh:"

# GitHub's own maximum page size; fewer round trips per observation, and the
# last short page is what ends the walk.
_ISSUES_PAGE_SIZE = 100


def github_tracker_reference(issue_number: int) -> TrackerItemReference:
    """The `gh:<n>` spelling this adapter owns for one GitHub issue."""

    return TrackerItemReference(f"{GITHUB_TRACKER_REFERENCE_PREFIX}{issue_number}")


@dataclass(frozen=True)
class LiveGitHubIssueSource:
    """The connected repository's open issues, observed through `githubkit`.

    `transport` is a test seam only, exactly as on
    `LiveGitHubEffectAdapterFactory`: production composition leaves it unset
    and reaches the real network.
    """

    repository: GitHubRepository
    token_credential: GitHubTokenCredential
    transport: httpx.BaseTransport | None = None

    def open_items(self) -> ObserveOpenTrackerItemsResult:
        try:
            token = self.token_credential.resolve()
        except GitHubCredentialUnresolvable as error:
            return TrackerSourceUnavailable(str(error))
        client: githubkit.GitHub[githubkit.TokenAuthStrategy] = githubkit.GitHub(
            token,
            transport=self.transport,
            # A cached listing could answer with a state from before the
            # operator's newest issue; the conditional-read design that would
            # make caching honest is a named deferral of slice 1.
            http_cache=False,
        )
        references: list[TrackerItemReference] = []
        page = 1
        while True:
            try:
                response = client.rest.issues.list_for_repo(
                    self.repository.owner,
                    self.repository.name,
                    state="open",
                    per_page=_ISSUES_PAGE_SIZE,
                    page=page,
                )
            except githubkit.exception.RequestFailed as error:
                return TrackerSourceUnavailable(
                    "GitHub answered "
                    f"{error.response.status_code} listing open issues of "
                    f"{self.repository.owner}/{self.repository.name}"
                )
            except githubkit.exception.RequestError as error:
                # githubkit wraps every transport and timeout failure in this
                # type before httpx's own can surface.
                return TrackerSourceUnavailable(
                    "GitHub could not be reached listing open issues of "
                    f"{self.repository.owner}/{self.repository.name}: {error}"
                )
            payload = response.raw_response.json()
            malformed = self._collect_issue_references(payload, references)
            if malformed is not None:
                return malformed
            if len(payload) < _ISSUES_PAGE_SIZE:
                return OpenTrackerItemsObserved(tuple(references))
            page += 1

    def _collect_issue_references(
        self, payload: Any, references: list[TrackerItemReference]
    ) -> TrackerPayloadMalformed | None:
        if not isinstance(payload, list):
            return TrackerPayloadMalformed(
                "the open-issue listing was not a list of issues"
            )
        for entry in payload:
            if not isinstance(entry, dict):
                return TrackerPayloadMalformed(
                    "the open-issue listing carried an entry that is not an object"
                )
            # GitHub's issue listing carries pull requests too, marked by this
            # key; a pull request is not a work item this queue observes.
            if "pull_request" in entry:
                continue
            number = entry.get("number")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                return TrackerPayloadMalformed(
                    "an open issue carried no positive integer number"
                )
            references.append(github_tracker_reference(number))
        return None
