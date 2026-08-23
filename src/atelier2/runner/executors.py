"""Typed selection of the one runner-side executor a manifest names, and of the
provider toolchain that executor needs on this image.

Both questions are keyed at the exact `(provider_id, executor_revision)` pair
the attested manifest carries -- never at a bare string a caller could mistype
-- so an unknown combination refuses before any child ever starts, instead of
falling through to whatever executor or executable happened to be composed by
default.

The two registries are separate because the questions are: an image can carry
a provider's pinned CLI, and prove it before READY, some slices before a
runner-side executor for it is authored. A manifest naming such a revision
still refuses -- at selection rather than at attestation -- and never starts a
provider.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from atelier2.adapters.claude_subscription import (
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    CONFORMANT_CLAUDE_VERSIONS,
    CREDENTIAL_RECORD_ENTRY,
    MANAGED_POLICY_ROOTS,
    ClaudeExecutableUnsupported,
    ClaudeManagedPolicyPresent,
    ClaudeSubscriptionSettings,
    attest_no_managed_policy,
    verify_claude_capability,
)
from atelier2.adapters.free_runner_executor import FreeRunnerCandidateExecutor
from atelier2.contracts.runner_manifests import (
    ABSENT_PROVIDER_CLI,
    MeasuredProviderCli,
    MeasuredProviderCliVersion,
    NoProviderCliPin,
    ProviderCliPin,
    RunnerExecutorCliPin,
    RunnerManifestV1,
)
from atelier2.ports.agent_executions import AgentExecutorV2


class RunnerExecutorUnavailable(ValueError):
    """No runner-side executor answers this manifest's exact provider and revision."""


class RunnerToolchainUnpinned(ValueError):
    """This image pins no provider toolchain for the manifest's exact revision."""


class RunnerToolchainRefused(ValueError):
    """This image's pinned toolchain cannot serve the manifest's executor."""


class RunnerProviderToolchain(Protocol):
    """What one executor revision needs installed, and the proof it is there."""

    @property
    def cli_pin(self) -> RunnerExecutorCliPin:
        """The measurement Core admits in this revision's READY attestation."""
        ...

    def attest(self, credential_directory: Path) -> MeasuredProviderCliVersion:
        """Measure this image's toolchain, or refuse before any provider start."""
        ...


class _AbsentProviderToolchain:
    """A runner-owned executor that starts no provider CLI at all.

    Its attestation is not a skipped check: it states, in the same typed
    vocabulary a measured CLI uses, that there was nothing to measure, so a
    READY carrying a version for it is refused rather than welcomed.
    """

    @property
    def cli_pin(self) -> RunnerExecutorCliPin:
        return NoProviderCliPin()

    def attest(self, credential_directory: Path) -> MeasuredProviderCliVersion:
        del credential_directory
        return ABSENT_PROVIDER_CLI


# The pin is the conformance set the adapter owns, never a second copy of it:
# this names which executable carries it on a Runner image.
_CLAUDE_CLI_PIN = ProviderCliPin("claude", CONFORMANT_CLAUDE_VERSIONS)


class _ClaudeSubscriptionToolchain:
    """The Claude Code this image installed, proven before a provider may start.

    Every check here is the serving deployment's own composition check, reused
    rather than restated: `ClaudeSubscriptionSettings` proves the executable is
    runnable, the credential directory exists and bubblewrap resolves on the
    search path; `verify_claude_capability` measures `--version` against the
    reviewed conformance set; `attest_no_managed_policy` refuses a host or an
    account where administrator policy can still act. A Runner is the same
    trust boundary as that deployment, so it may not admit anything that one
    refuses.
    """

    @property
    def cli_pin(self) -> RunnerExecutorCliPin:
        return _CLAUDE_CLI_PIN

    def attest(self, credential_directory: Path) -> MeasuredProviderCliVersion:
        search_path = os.environ.get("PATH", "")
        resolved = shutil.which(_CLAUDE_CLI_PIN.executable_name, path=search_path)
        if resolved is None:
            raise RunnerToolchainRefused("runner-provider-cli-absent")
        try:
            settings = ClaudeSubscriptionSettings(
                Path(resolved), credential_directory, search_path
            )
            measured = MeasuredProviderCli(
                verify_claude_capability(settings.executable)
            )
            # An offered credential directory with no credential record at all
            # is its own refusal, named before the policy attestation runs.
            # Without this split, an unbilled Runner that simply holds no
            # credential would be reported as a host where administrator
            # policy can act, which is a different and much louder claim.
            record = settings.credential_directory / CREDENTIAL_RECORD_ENTRY
            if not record.is_file():
                raise RunnerToolchainRefused("runner-provider-credential-absent")
            attest_no_managed_policy(
                settings.credential_directory, MANAGED_POLICY_ROOTS
            )
        except RunnerToolchainRefused:
            # Already named above; it is a ValueError too, so it must pass
            # through before the broad clause below can rename it.
            raise
        except ClaudeExecutableUnsupported as error:
            raise RunnerToolchainRefused("runner-provider-cli-drift") from error
        except ClaudeManagedPolicyPresent as error:
            raise RunnerToolchainRefused("runner-provider-policy-present") from error
        except OSError as error:
            # Measured, not assumed: `Path.exists()` ignores only ENOENT,
            # ENOTDIR, EBADF and ELOOP, so a policy surface this Runner may not
            # stat raises EACCES straight out of `attest_no_managed_policy`
            # (observed live: PermissionError on a root-owned credential
            # mount). A surface that refuses to answer is not an absent one.
            raise RunnerToolchainRefused(
                "runner-provider-toolchain-unusable"
            ) from error
        except ValueError as error:
            raise RunnerToolchainRefused(
                "runner-provider-toolchain-unusable"
            ) from error
        return measured


_RUNNER_EXECUTORS: dict[tuple[str, str], Callable[[], AgentExecutorV2]] = {
    ("fake-free", "fake-free/v1"): FreeRunnerCandidateExecutor,
}

_RUNNER_TOOLCHAINS: dict[tuple[str, str], RunnerProviderToolchain] = {
    ("fake-free", "fake-free/v1"): _AbsentProviderToolchain(),
    (
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
    ): _ClaudeSubscriptionToolchain(),
}


def select_runner_executor(manifest: RunnerManifestV1) -> AgentExecutorV2:
    """The one executor this attested manifest names, refused before any start."""
    factory = _RUNNER_EXECUTORS.get((manifest.provider_id, manifest.executor_revision))
    if factory is None:
        raise RunnerExecutorUnavailable("runner-executor-unavailable")
    return factory()


def runner_provider_toolchain(manifest: RunnerManifestV1) -> RunnerProviderToolchain:
    """The toolchain this image pins for the manifest's exact executor revision."""
    toolchain = _RUNNER_TOOLCHAINS.get(
        (manifest.provider_id, manifest.executor_revision)
    )
    if toolchain is None:
        raise RunnerToolchainUnpinned("runner-toolchain-unpinned")
    return toolchain


def runner_executor_cli_pin(manifest: RunnerManifestV1) -> RunnerExecutorCliPin:
    """Which measured provider CLI a READY for this manifest may report."""
    return runner_provider_toolchain(manifest).cli_pin


def attest_runner_provider_toolchain(
    manifest: RunnerManifestV1,
) -> MeasuredProviderCliVersion:
    """Measure this image's toolchain for the manifest, before any provider start."""
    return runner_provider_toolchain(manifest).attest(
        Path(manifest.provider_credential_directory)
    )
