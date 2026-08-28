"""GitHub source parsing and validation through an injected HTTP transport."""

from __future__ import annotations

from pathlib import Path

import httpx

from atelier2.adapters.github.composition import GITHUB_SOURCE_KIND
from atelier2.adapters.github.live_effects import GITHUB_TOKEN_CREDENTIAL_ENTRY
from atelier2.adapters.github.project_connections import GitHubProjectSourceConnector
from atelier2.contracts.host_configuration import SourceAddress
from atelier2.ports.project_connections import (
    ParsedProjectSourceAddress,
    ProjectSourceAddressInvalid,
    ProjectSourceAuthenticationRefused,
    ProjectSourceValidationUnavailable,
    ValidatedProjectSource,
)


def _credential_directory(tmp_path: Path, token: str = "provider-token") -> Path:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / GITHUB_TOKEN_CREDENTIAL_ENTRY).write_text(token, encoding="utf-8")
    return credentials


def test_address_parser_owns_github_forms_and_public_projection() -> None:
    connector = GitHubProjectSourceConnector()

    assert connector.parse_address("github.com/acme/studio") == (
        ParsedProjectSourceAddress(GITHUB_SOURCE_KIND, "acme/studio")
    )
    assert connector.parse_address("https://github.com/acme/studio.git") == (
        connector.parse_address("github.com/acme/studio")
    )
    assert isinstance(
        connector.parse_address("gitlab.com/acme/studio"), ProjectSourceAddressInvalid
    )
    assert connector.public_address(SourceAddress("acme/studio@main")) == "acme/studio"
    assert connector.parse_stored_address(SourceAddress("acme/studio@main")) == (
        ParsedProjectSourceAddress(GITHUB_SOURCE_KIND, "acme/studio")
    )


def test_validation_reads_the_credential_reference_and_keeps_branch_opaque(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"default_branch": "trunk"})

    connector = GitHubProjectSourceConnector(httpx.MockTransport(answer))
    parsed = connector.parse_address("github.com/acme/studio")
    assert isinstance(parsed, ParsedProjectSourceAddress)

    result = connector.validate(parsed, _credential_directory(tmp_path))

    assert result == ValidatedProjectSource(
        parsed.source_kind,
        SourceAddress("acme/studio@trunk"),
        "acme/studio",
    )
    assert [request.url.path for request in requests] == ["/repos/acme/studio"]


def test_provider_auth_reason_is_bounded_and_credential_redacted(
    tmp_path: Path,
) -> None:
    provider_reason = "Bearer provider-token " + ("x" * 400)

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": provider_reason}, request=request)

    connector = GitHubProjectSourceConnector(httpx.MockTransport(refuse))
    parsed = connector.parse_address("github.com/acme/studio")
    assert isinstance(parsed, ParsedProjectSourceAddress)

    result = connector.validate(parsed, _credential_directory(tmp_path))

    assert isinstance(result, ProjectSourceAuthenticationRefused)
    assert len(result.reason) <= 256
    assert "provider-token" not in result.reason


def test_provider_auth_reason_cannot_echo_the_submitted_token(tmp_path: Path) -> None:
    token = "opaque-value-123"

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": f"credential rejected: {token}"},
            request=request,
        )

    connector = GitHubProjectSourceConnector(httpx.MockTransport(refuse))
    parsed = connector.parse_address("github.com/acme/studio")
    assert isinstance(parsed, ParsedProjectSourceAddress)

    result = connector.validate(parsed, _credential_directory(tmp_path, token))

    assert isinstance(result, ProjectSourceAuthenticationRefused)
    assert result.reason == "credential rejected: [REDACTED]"


def test_malformed_provider_json_is_validation_unavailable(tmp_path: Path) -> None:
    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    connector = GitHubProjectSourceConnector(httpx.MockTransport(malformed))
    parsed = connector.parse_address("github.com/acme/studio")
    assert isinstance(parsed, ParsedProjectSourceAddress)

    result = connector.validate(parsed, _credential_directory(tmp_path))

    assert result == ProjectSourceValidationUnavailable(
        "GitHub returned malformed JSON while validating the source"
    )


def test_network_failure_is_separate_from_authentication_refusal(
    tmp_path: Path,
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable", request=request)

    connector = GitHubProjectSourceConnector(httpx.MockTransport(unavailable))
    parsed = connector.parse_address("github.com/acme/studio")
    assert isinstance(parsed, ParsedProjectSourceAddress)

    result = connector.validate(parsed, _credential_directory(tmp_path))

    assert result == ProjectSourceValidationUnavailable(
        "GitHub could not be reached while validating the source"
    )
