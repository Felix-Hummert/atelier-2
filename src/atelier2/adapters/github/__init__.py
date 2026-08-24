"""The GitHub platform adapter: one EffectAdapterFactory, platform-blind above.

Two `EffectAdapterFactory` implementations share the one `open-pr` contract:
`GitHubEffectAdapterFactory` records against a fake platform, and
`LiveGitHubEffectAdapterFactory` publishes and reads back through `githubkit`
against real GitHub (ADR 0010). The live one is composed on serve only when the
operator names its credential directory and repository (`atelier2.host.serving`,
`#430`); the fake stays a test double. Because live GitHub cannot prove absence,
admission refuses an agent-authored `open-pr` grant against the live adapter
(`#430`/`#431`); only an Action node's `open-pr` reaches it.
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
