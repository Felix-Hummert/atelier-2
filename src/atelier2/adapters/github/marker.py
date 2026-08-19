"""The content marker an Atelier-authored pull request carries.

ADR 0010 owns the marker's syntax and its key so two operations cannot drift
into two dialects. The request hash is what a re-attempt after a crash uses to
find the first pull request instead of creating its twin.
"""

from __future__ import annotations

EFFECT_REQUEST_MARKER_KEY = "Atelier-Effect-Request"


def marker_line(request_hash: str) -> str:
    """The exact body line that identifies one prepared effect."""
    return f"{EFFECT_REQUEST_MARKER_KEY}: {request_hash}"


def body_carries_request_hash(body: str, request_hash: str) -> bool:
    """Whether this pull-request body is the object this request authored."""
    return marker_line(request_hash) in body.splitlines()
