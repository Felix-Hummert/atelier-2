"""Composition helpers for the git transport effect adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from atelier2.adapters.git_transport.effects import (
    GitRemote,
    GitTransportEffectAdapterFactory,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.ports.effects import HeadBranchPullRequests


@dataclass(frozen=True, slots=True)
class GitTransportConfiguration:
    remote_identity: str
    remote_url: str
    credential_file: Path | None = None


def compose_git_transport_effect_adapter(
    candidate_store: Path,
    configuration: GitTransportConfiguration,
    head_branch_pull_requests: HeadBranchPullRequests,
) -> GitTransportEffectAdapterFactory:
    return GitTransportEffectAdapterFactory(
        candidate_store,
        GitRemote(
            configuration.remote_identity,
            configuration.remote_url,
            configuration.credential_file,
        ),
        AdapterRevision("git-transport-push-v1"),
        EffectDestination("git"),
        head_branch_pull_requests,
    )
