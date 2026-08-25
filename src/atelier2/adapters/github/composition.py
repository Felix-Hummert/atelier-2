"""Composing the live GitHub surfaces from a project-source connection record.

A connection revision carries its source address as an opaque value (ADR 0010
decision 1), and this module is that address's one decode owner for the
`github` source kind: `owner/name@base-branch`, in GitHub's own words for a
repository plus the branch pull requests target. Serving hands the whole
revision over and receives a composed surface -- the `open-pr` effect factory
or the open-issue observation source; no repository fact travels back above
this package.

The revision's credential directory stays a reference here exactly as it is in
the record (ADR 0009 §6): the token it names is read when the composed surface
reaches GitHub, never during composition.
"""

from __future__ import annotations

from typing import assert_never

from atelier2.adapters.github.live_effects import (
    GitHubRepository,
    GitHubTokenCredential,
    LiveGitHubEffectAdapterFactory,
)
from atelier2.adapters.github.observation import LiveGitHubIssueSource
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import (
    ProjectSourceConnectionRevision,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
)

GITHUB_SOURCE_KIND = SourceKind("github")


class GitHubConnectionUncomposable(ValueError):
    """The connection record does not compose this package's live surfaces."""


def live_github_effect_adapter_factory(
    connection: ProjectSourceConnectionRevision,
    adapter_revision: AdapterRevision,
    destination: EffectDestination,
) -> LiveGitHubEffectAdapterFactory:
    return LiveGitHubEffectAdapterFactory(
        adapter_revision,
        destination,
        _connected_repository(connection),
        _token_credential(connection),
    )


def live_github_issue_source(
    connection: ProjectSourceConnectionRevision,
) -> LiveGitHubIssueSource:
    return LiveGitHubIssueSource(
        _connected_repository(connection),
        _token_credential(connection),
    )


def _connected_repository(
    connection: ProjectSourceConnectionRevision,
) -> GitHubRepository:
    if connection.source_kind != GITHUB_SOURCE_KIND:
        raise GitHubConnectionUncomposable(
            f"source kind {connection.source_kind.value!r} has no live "
            f"composition; only {GITHUB_SOURCE_KIND.value!r} composes here"
        )
    return _repository(connection.source_address)


def _token_credential(
    connection: ProjectSourceConnectionRevision,
) -> GitHubTokenCredential:
    match connection.auth_method:
        case SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN:
            return GitHubTokenCredential(connection.credential_directory)
        case _ as unreachable:
            assert_never(unreachable)


def _repository(address: SourceAddress) -> GitHubRepository:
    repository_part, at, base_branch = address.value.partition("@")
    owner, slash, name = repository_part.partition("/")
    if not at or not slash or not owner or not name or not base_branch or "/" in name:
        raise GitHubConnectionUncomposable(
            "a github source address reads owner/name@base-branch, "
            f"not {address.value!r}; reconnect the project with that address"
        )
    return GitHubRepository(owner, name, base_branch)
