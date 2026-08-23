"""Typed selection of the one runner-side executor a manifest names.

Keyed at the exact `(provider_id, executor_revision)` pair the attested
manifest carries -- never at a bare string a caller could mistype -- so an
unknown combination refuses before any child ever starts, instead of falling
through to whatever executor happened to be composed by default.
"""

from __future__ import annotations

from collections.abc import Callable

from atelier2.adapters.free_runner_executor import FreeRunnerCandidateExecutor
from atelier2.contracts.runner_manifests import RunnerManifestV1
from atelier2.ports.agent_executions import AgentExecutorV2


class RunnerExecutorUnavailable(ValueError):
    """No runner-side executor answers this manifest's exact provider and revision."""


_RUNNER_EXECUTORS: dict[tuple[str, str], Callable[[], AgentExecutorV2]] = {
    ("fake-free", "fake-free/v1"): FreeRunnerCandidateExecutor,
}


def select_runner_executor(manifest: RunnerManifestV1) -> AgentExecutorV2:
    """The one executor this attested manifest names, refused before any start."""
    factory = _RUNNER_EXECUTORS.get((manifest.provider_id, manifest.executor_revision))
    if factory is None:
        raise RunnerExecutorUnavailable("runner-executor-unavailable")
    return factory()
