"""The GitHub platform adapter: one EffectAdapterFactory, platform-blind above.

Two `EffectAdapterFactory` implementations share the one `open-pr` contract:
`GitHubEffectAdapterFactory` records against a fake platform, and
`LiveGitHubEffectAdapterFactory` publishes and reads back through `githubkit`
against real GitHub (ADR 0010). The live one is composed on serve from the
served project's source-connection record, whose opaque address this package
alone decodes (`atelier2.adapters.github.composition`); the fake stays a test
double. Live GitHub cannot prove absence, so an unmatched readback waits for
operator reconciliation before an agent or Action effect can execute.
"""

from atelier2.adapters.github.composition import (
    GITHUB_SOURCE_KIND,
    GitHubConnectionUncomposable,
    live_github_effect_adapter_factory,
    live_github_issue_source,
)
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.adapters.github.live_effects import (
    GitHubCredentialUnresolvable,
    GitHubRepository,
    GitHubTokenCredential,
    GitHubUnexpectedResponse,
    LiveGitHubEffectAdapterFactory,
)
from atelier2.adapters.github.observation import LiveGitHubIssueSource

__all__ = (
    "GITHUB_SOURCE_KIND",
    "GitHubConnectionUncomposable",
    "GitHubCredentialUnresolvable",
    "GitHubEffectAdapterFactory",
    "GitHubRepository",
    "GitHubTokenCredential",
    "GitHubUnexpectedResponse",
    "LiveGitHubEffectAdapterFactory",
    "LiveGitHubIssueSource",
    "live_github_effect_adapter_factory",
    "live_github_issue_source",
)
