"""Typed selection of the one runner-side executor a manifest names, and the
toolchain attestation that measures what this image installed for it.

Selection is keyed at the exact `(provider_id, executor_revision)` pair the
attested manifest carries -- never at a bare string a caller could mistype
-- so an unknown combination refuses before any child ever starts, instead of
falling through to whatever executor happened to be composed by default.

Which CLI a manifest's revision pins is a separate, Core-reachable question
this module does not answer itself: `atelier2.adapters.runner_cli_pins` owns
that register, because an image can carry a provider's pinned CLI, and prove
it before READY, some slices before a runner-side executor for it is
authored. A manifest naming such a revision still refuses -- at selection
rather than at attestation -- and never starts a provider.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from atelier2.adapters.free_runner_executor import FreeRunnerCandidateExecutor
from atelier2.adapters.runner_cli_pins import runner_provider_toolchain
from atelier2.contracts.runner_manifests import (
    MeasuredProviderCliVersion,
    RunnerManifestV1,
)
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


def attest_runner_provider_toolchain(
    manifest: RunnerManifestV1,
) -> MeasuredProviderCliVersion:
    """Measure this image's toolchain for the manifest, before any provider start."""
    return runner_provider_toolchain(manifest).attest(
        Path(manifest.provider_credential_directory)
    )
