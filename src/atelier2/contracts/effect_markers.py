"""The shared marker carried by every content-bearing Atelier effect."""

from __future__ import annotations

EFFECT_REQUEST_MARKER_KEY = "Atelier-Effect-Request"


def marker_line(request_hash: str) -> str:
    return f"{EFFECT_REQUEST_MARKER_KEY}: {request_hash}"


def body_carries_request_hash(body: str, request_hash: str) -> bool:
    return marker_line(request_hash) in body.splitlines()


def commit_message(attempt_id: str, request_hash: str) -> str:
    return (
        f"Atelier-authored work for attempt {attempt_id}\n\n"
        f"{marker_line(request_hash)}\n"
    )
