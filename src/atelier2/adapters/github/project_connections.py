"""GitHub-owned parsing and validation for project source connections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import githubkit
import githubkit.exception
import httpx

from atelier2.adapters.github.composition import (
    GITHUB_SOURCE_KIND,
    github_public_source_address,
)
from atelier2.adapters.github.live_effects import (
    GitHubCredentialUnresolvable,
    GitHubTokenCredential,
)
from atelier2.contracts.host_configuration import SourceAddress, SourceReference
from atelier2.contracts.secret_redaction import redact_credentials
from atelier2.ports.project_connections import (
    ParsedProjectSourceAddress,
    ParseProjectSourceAddressResult,
    ProjectSourceAddressInvalid,
    ProjectSourceAuthenticationRefused,
    ProjectSourceCredentialUnresolvable,
    ProjectSourceValidationUnavailable,
    ValidatedProjectSource,
    ValidateProjectSourceResult,
)

_GITHUB_ADDRESS = re.compile(
    r"(?:https://)?github\.com/([^/\s]+)/([^/@\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_MAXIMUM_PROVIDER_REASON_CHARACTERS = 256
_BEARER_CREDENTIAL = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


def _provider_reason(
    error: githubkit.exception.RequestFailed, submitted_token: str
) -> str:
    reason = "GitHub refused the token for this repository"
    try:
        payload: Any = error.response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        reason = payload["message"]
    without_submitted_token = reason.replace(submitted_token, "[REDACTED]")
    without_bearer_credential = _BEARER_CREDENTIAL.sub(
        "Bearer [REDACTED]", without_submitted_token
    )
    bounded = " ".join(redact_credentials(without_bearer_credential).text.split())
    return bounded[:_MAXIMUM_PROVIDER_REASON_CHARACTERS] or "GitHub refused the token"


@dataclass(frozen=True)
class GitHubProjectSourceConnector:
    transport: httpx.BaseTransport | None = None

    def parse_address(self, address: str) -> ParseProjectSourceAddressResult:
        if type(address) is not str:
            return ProjectSourceAddressInvalid("the source address must be text")
        matched = _GITHUB_ADDRESS.fullmatch(address.strip())
        if matched is None:
            return ProjectSourceAddressInvalid(
                "a GitHub source address must read github.com/owner/name"
            )
        return ParsedProjectSourceAddress(
            GITHUB_SOURCE_KIND, f"{matched[1]}/{matched[2]}"
        )

    def validate(
        self,
        parsed: ParsedProjectSourceAddress,
        credential_directory: Path,
    ) -> ValidateProjectSourceResult:
        if parsed.source_kind != GITHUB_SOURCE_KIND:
            return ProjectSourceAddressInvalid("this connector accepts only GitHub")
        owner, separator, name = parsed.public_address.partition("/")
        if not separator or not owner or not name or "/" in name:
            return ProjectSourceAddressInvalid(
                "a GitHub source address must read github.com/owner/name"
            )
        try:
            token = GitHubTokenCredential(credential_directory).resolve()
        except GitHubCredentialUnresolvable:
            return ProjectSourceCredentialUnresolvable()
        client: githubkit.GitHub[githubkit.TokenAuthStrategy] = githubkit.GitHub(
            token,
            transport=self.transport,
            http_cache=False,
        )
        try:
            response = client.rest.repos.get(owner, name)
        except githubkit.exception.RequestFailed as error:
            if error.response.status_code in {
                httpx.codes.UNAUTHORIZED,
                httpx.codes.FORBIDDEN,
                httpx.codes.NOT_FOUND,
                httpx.codes.UNPROCESSABLE_ENTITY,
            }:
                return ProjectSourceAuthenticationRefused(
                    _provider_reason(error, token)
                )
            return ProjectSourceValidationUnavailable(
                f"GitHub answered {error.response.status_code} validating the source"
            )
        except githubkit.exception.RequestError:
            return ProjectSourceValidationUnavailable(
                "GitHub could not be reached while validating the source"
            )
        try:
            payload = response.raw_response.json()
        except ValueError:
            return ProjectSourceValidationUnavailable(
                "GitHub returned malformed JSON while validating the source"
            )
        if not isinstance(payload, dict):
            return ProjectSourceValidationUnavailable(
                "GitHub returned no repository object while validating the source"
            )
        default_branch = payload.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            return ProjectSourceValidationUnavailable(
                "GitHub returned no default branch while validating the source"
            )
        return ValidatedProjectSource(
            GITHUB_SOURCE_KIND,
            SourceAddress(parsed.public_address),
            SourceReference(default_branch),
            parsed.public_address,
        )

    def parse_stored_address(
        self, source_address: SourceAddress
    ) -> ParseProjectSourceAddressResult:
        try:
            public_address = self.public_address(source_address)
        except ValueError:
            return ProjectSourceAddressInvalid("the stored GitHub source is malformed")
        return ParsedProjectSourceAddress(GITHUB_SOURCE_KIND, public_address)

    def public_address(self, source_address: SourceAddress) -> str:
        return github_public_source_address(source_address)
