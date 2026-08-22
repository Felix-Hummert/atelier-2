"""The one Serve-free owner of the free-runner auth-reference derivation.

Both Core's encode side (the disposable `#301` witness process today; a real
Core composition later) and the Runner's resolve side (`runner.session`) call
this module directly, so the reference either end computes for the same
`AuthProfileRevision` can never drift into two owners. Nothing here imports
`atelier2.serve` or any Core-only adapter: Serve composes providers, it never
derives their identity, and this module stays reachable from both sides of
the wire it is named for.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.agents import AuthMode, AuthProfileRevision

_MAXIMUM_AUTH_REFERENCE_BYTES = 128


@dataclass(frozen=True)
class AuthReference:
    """A provider's own non-secret pointer to a resolved authorization.

    Never the credential value and never a host path -- both sides of the
    wire check this exact typed form and refuse anything else. Each provider
    owns its own derivation into this shape; `free_runner_auth_reference`
    below is the fake-free candidate's.
    """

    value: str

    def __post_init__(self) -> None:
        encoded = self.value.encode("ascii")
        if not 1 <= len(encoded) <= _MAXIMUM_AUTH_REFERENCE_BYTES:
            raise ValueError(
                f"auth reference must be 1..{_MAXIMUM_AUTH_REFERENCE_BYTES} ASCII bytes"
            )


@dataclass(frozen=True)
class FreeRunnerAuthorization:
    """The fake-free executor receives no credential material."""


def free_runner_auth_reference(profile: AuthProfileRevision) -> AuthReference:
    """The one deterministic, secret-free reference for a fake-free profile."""
    return AuthReference(
        f"urn:atelier2:fake-free-auth:v1:{profile.revision_hash.value}"
    )


def resolve_free_runner_authorization(
    profile: AuthProfileRevision, reference: AuthReference
) -> FreeRunnerAuthorization:
    """Confirm `reference` is exactly this profile's own derived reference."""
    if (
        profile.provider_id.value != "fake-free"
        or profile.auth_mode is not AuthMode.API_KEY
        or reference != free_runner_auth_reference(profile)
    ):
        raise ValueError("auth-profile-unresolvable")
    return FreeRunnerAuthorization()
