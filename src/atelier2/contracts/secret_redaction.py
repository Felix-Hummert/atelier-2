"""Credential shapes taken out of provider bytes before anything durable keeps them.

**Why this is not the CI secret scan.** The `Secret scan` job owns a different
boundary: it reads the repository's own git history with a pinned `gitleaks`
binary, and the rules it matches live inside that Go executable -- the
repository's `.gitleaks.toml` carries reviewed allowlist entries and no patterns
at all. Nothing there can be called on a durable write path: an in-process
redactor cannot shell out to a binary that a serving host is not promised to
have, and a transcript that could only be kept when a Go tool happened to be
installed would be evidence that disappears for an unrelated reason.

So this is the owner for the other boundary -- bytes a provider just produced,
on their way into an artifact nobody can delete. The two owners answer different
questions about different material and neither can stand in for the other.

**Why the set is small and named.** Every shape below is one a reader can judge:
what it matches, and why bytes of that shape are a credential rather than
prose. It is deliberately not a corpus. A match is replaced, never dropped, and
the caller learns that something was replaced -- material this cannot recognise
is still kept, because a transcript that quietly held back what it could not
classify would be exactly the silence this repository is removing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REDACTION_MARKER = "[redacted]"
"""What stands where a credential stood, in every surface that reads it back."""

# Where a shape names the credential's own surroundings -- the header it travels
# in, the field it is assigned to -- only this group is replaced, so the reader
# still sees *which* secret was taken out.
_MATCHED_VALUE_GROUP = "value"
# What a shape may span. A credential is a token, not a document, and an
# unbounded span is what turns a linear scan of one large tool result into a
# quadratic one.
_MAXIMUM_SPAN = 8_192


@dataclass(frozen=True, slots=True)
class CredentialShape:
    """One recognisable way a credential appears in text a provider produced."""

    name: str
    pattern: re.Pattern[str]


CREDENTIAL_SHAPES = (
    CredentialShape(
        # The whole armoured block: its own header names it, and every byte
        # between the markers is key material.
        "private-key-block",
        re.compile(
            r"-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----"
            rf".{{0,{_MAXIMUM_SPAN}}}?"
            r"-----END [A-Z ]{0,32}PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    CredentialShape(
        # AWS's own published key-id form: a fixed prefix and a fixed width.
        "aws-access-key-id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    CredentialShape(
        # Issuer-prefixed tokens: the prefix is the issuer's own declaration
        # that what follows is a credential.
        "issued-token",
        re.compile(
            r"\b(?:sk-ant|sk|ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abopsr])"
            r"[-_][A-Za-z0-9_-]{16,}"
        ),
    ),
    CredentialShape(
        # A JSON Web Token: three base64url segments, the first of which is a
        # JSON header and therefore always begins `eyJ`.
        "json-web-token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    CredentialShape(
        # The credential in transit, named by the header carrying it.
        "authorization-header",
        re.compile(
            r"(?i:authorization)\s*:\s*(?i:bearer|basic|token)\s+"
            rf"(?P<{_MATCHED_VALUE_GROUP}>[A-Za-z0-9._~+/=-]{{8,}})"
        ),
    ),
    CredentialShape(
        # The credential at rest, named by the field it was assigned to. The
        # width floor keeps ordinary prose -- `password: yes` -- out of it.
        #
        # The name is read as the whole identifier rather than as the credential
        # word alone, because that is how a provider spells it. The measured
        # miss this was widened for is `AWS_SECRET_ACCESS_KEY=`, whose word sits
        # between two other segments and which the narrower shape walked past
        # while still calling itself a redactor. Only the value is replaced, so
        # a generously matched name costs nothing, and the deliberate trade is
        # that a name saying credential is believed even where its value turns
        # out to be a path: a visible replacement rather than a silent leak.
        "assigned-secret",
        re.compile(
            r"(?:[A-Za-z0-9]{1,32}[_-]){0,8}"
            # The plural is inside the case-insensitive group, not after it: an
            # `s` left outside matched only a lowercase one, so a field spelled
            # `CREDENTIALS` was read as prose.
            r"(?i:(?:api[_-]?key|secret|password|passwd|token|credential)s?)"
            r"(?:[_-][A-Za-z0-9]{1,32}){0,8}"
            r"\s*[:=]\s*[\"']?"
            rf"(?P<{_MATCHED_VALUE_GROUP}>[A-Za-z0-9._~+/=-]{{12,}})"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class RedactedText:
    """Text safe to keep, and whether keeping it safe changed anything."""

    text: str
    redacted: bool


def _replacement(match: re.Match[str]) -> str:
    if _MATCHED_VALUE_GROUP not in match.groupdict():
        return REDACTION_MARKER
    start, end = match.span(_MATCHED_VALUE_GROUP)
    matched = match.group(0)
    offset = match.start()
    return matched[: start - offset] + REDACTION_MARKER + matched[end - offset :]


def redact_credentials(text: str) -> RedactedText:
    """Replace every credential shape this owner recognises, and say whether it did."""

    redacted = text
    for shape in CREDENTIAL_SHAPES:
        redacted = shape.pattern.sub(_replacement, redacted)
    return RedactedText(redacted, redacted != text)
