"""The GitHub platform adapter: one EffectAdapterFactory, platform-blind above.

Two `EffectAdapterFactory` implementations share the one `open-pr` contract:
`GitHubEffectAdapterFactory` records against a fake platform, and
`LiveGitHubEffectAdapterFactory` publishes and reads back through `githubkit`
against real GitHub (ADR 0010). Neither is composed on serve yet -- that
remains an operator-gated step.
"""

from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.adapters.github.live_effects import (
    GitHubCredentialUnresolvable,
    GitHubRepository,
    GitHubTokenCredential,
    GitHubUnexpectedResponse,
    LiveGitHubEffectAdapterFactory,
)

__all__ = (
    "GitHubCredentialUnresolvable",
    "GitHubEffectAdapterFactory",
    "GitHubRepository",
    "GitHubTokenCredential",
    "GitHubUnexpectedResponse",
    "LiveGitHubEffectAdapterFactory",
)
