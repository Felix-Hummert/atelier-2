"""Which credential shapes never reach durable evidence, and what stands there."""

from __future__ import annotations

import pytest

from atelier2.contracts.secret_redaction import REDACTION_MARKER, redact_credentials


def assembled(*parts: str) -> str:
    """One credential-shaped value, assembled here instead of spelled out.

    This repository's own history is secret-scanned, so a test that needs the
    shape of a credential builds it from parts rather than writing a
    credential-shaped literal into the history that scan reads.
    """

    return "".join(parts)


def armoured_key() -> str:
    header = assembled("-----BEGIN ", "RSA PRIVATE KEY", "-----")
    footer = assembled("-----END ", "RSA PRIVATE KEY", "-----")
    return f"{header}\nMIIEowIBAAKCAQEAxfake\n{footer}"


@pytest.mark.parametrize(
    ("secret", "surroundings"),
    [
        pytest.param(armoured_key(), "{secret}", id="armoured private key"),
        pytest.param(
            assembled("AKIA", "7QF3NOTAREALKEY0"),
            "aws_access_key_id={secret}\n",
            id="aws access key id",
        ),
        pytest.param(
            assembled("sk-ant", "-", "notarealkeyvalue0123456789"),
            "export ANTHROPIC_KEY={secret}",
            id="issuer-prefixed token",
        ),
        pytest.param(
            assembled(
                "eyJ", "hbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiI0MiJ9", ".", "bm90LXJlYWw"
            ),
            "session={secret} (expired)",
            id="json web token",
        ),
    ],
)
def test_a_recognised_credential_is_replaced_where_it_stood(
    secret: str, surroundings: str
) -> None:
    redacted = redact_credentials(surroundings.format(secret=secret))

    assert secret not in redacted.text
    assert REDACTION_MARKER in redacted.text
    assert redacted.redacted


@pytest.mark.parametrize(
    ("carrier", "expected"),
    [
        pytest.param(
            "Authorization: Bearer {secret}",
            f"Authorization: Bearer {REDACTION_MARKER}",
            id="the header keeps its name",
        ),
        pytest.param(
            "api_key = {secret}",
            f"api_key = {REDACTION_MARKER}",
            id="the field keeps its name",
        ),
    ],
)
def test_the_reader_still_sees_which_credential_was_taken_out(
    carrier: str, expected: str
) -> None:
    secret = assembled("notarealcredential", "0123456789")

    assert redact_credentials(carrier.format(secret=secret)).text == expected


@pytest.mark.parametrize(
    "prose",
    [
        pytest.param("The password: yes answer is not a credential.", id="short value"),
        pytest.param("Read the token from the operator's own keyring.", id="no value"),
        pytest.param("git commit -m 'begin private key rotation'", id="prose about it"),
    ],
)
def test_text_carrying_no_credential_is_kept_exactly_and_says_so(prose: str) -> None:
    redacted = redact_credentials(prose)

    assert redacted.text == prose
    assert not redacted.redacted
