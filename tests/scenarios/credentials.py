"""Credential-shaped test values, assembled rather than spelled out.

This repository's own history is secret-scanned, so a test that needs the
shape of a credential builds it from parts rather than writing a
credential-shaped literal into the history that scan reads.
"""

from __future__ import annotations


def assembled(*parts: str) -> str:
    """One credential-shaped value, assembled here instead of spelled out."""

    return "".join(parts)


def armoured_key() -> str:
    header = assembled("-----BEGIN ", "RSA PRIVATE KEY", "-----")
    footer = assembled("-----END ", "RSA PRIVATE KEY", "-----")
    return f"{header}\nMIIEowIBAAKCAQEAxfake\n{footer}"
