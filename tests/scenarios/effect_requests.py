"""Canonical effect requests shared by crash and integration scenarios."""

from __future__ import annotations

from atelier2.contracts.effect_requests import (
    OpenPullRequest,
    head_branch_for_unbound_request,
)


def open_pull_request_request_for_output(output: bytes) -> bytes:
    return OpenPullRequest(
        output.decode("utf-8"), head_branch_for_unbound_request(output)
    ).canonical_bytes()
