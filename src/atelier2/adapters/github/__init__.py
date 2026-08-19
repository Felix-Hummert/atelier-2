"""The GitHub platform adapter: one EffectAdapterFactory, platform-blind above.

This slice's destination is a recorded fake platform. Live GitHub and githubkit
are not composed here.
"""

from atelier2.adapters.github.effects import GitHubEffectAdapterFactory

__all__ = ("GitHubEffectAdapterFactory",)
