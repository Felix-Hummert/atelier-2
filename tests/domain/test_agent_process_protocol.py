from __future__ import annotations

import base64
from pathlib import Path

import pytest

from atelier2.adapters.agent_process_protocol import (
    MAXIMUM_AGENT_FRAMELESS_WAIT_RESPONSE_BYTES,
    cgroup_populated,
    decode_provider_environment,
    encode_control_frame,
    encode_provider_environment,
    encode_wait_response,
    maximum_agent_wait_response_bytes,
)
from atelier2.contracts.hashing import canonical_json
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
)


def test_canonical_json_sorts_its_keys_and_escapes_everything_beyond_ascii() -> None:
    payload = {"b": "Grüße", "a": [1, {"d": 2, "c": 3}]}

    assert canonical_json(payload) == (
        b'{"a":[1,{"c":3,"d":2}],"b":"Gr\\u00fc\\u00dfe"}'
    )


def test_a_control_frame_is_written_in_that_one_canonical_spelling() -> None:
    frame: dict[str, object] = {
        "type": "CANCELLED",
        "disposition": "REAPED_AFTER_TERM",
    }

    assert encode_control_frame(frame) == (
        b'{"disposition":"REAPED_AFTER_TERM","type":"CANCELLED"}'
    )


def test_the_wait_response_bound_is_exactly_the_declared_frame_at_its_worst() -> None:
    declared_frame_bytes = 8_192
    worst_case = encode_control_frame(
        {
            "return_code": -(2**31),
            "standard_error": base64.b64encode(
                b"e" * MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
            ).decode("ascii"),
            "standard_output": base64.b64encode(b"o" * declared_frame_bytes).decode(
                "ascii"
            ),
            "type": "COMPLETED",
        }
    )

    assert maximum_agent_wait_response_bytes(declared_frame_bytes) == len(worst_case)


def test_a_wait_response_at_its_exact_bound_is_encoded() -> None:
    declared_frame_bytes = 8_192
    completion = {
        "return_code": -(2**31),
        "standard_error": base64.b64encode(
            b"e" * MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
        ).decode("ascii"),
        "standard_output": base64.b64encode(b"o" * declared_frame_bytes).decode(
            "ascii"
        ),
        "type": "COMPLETED",
    }

    encoded = encode_wait_response(completion, declared_frame_bytes)

    assert len(encoded) == maximum_agent_wait_response_bytes(declared_frame_bytes)


@pytest.mark.parametrize(
    "declared_frame_bytes", (None, 8_192), ids=("frameless", "declared-frame")
)
def test_a_wait_response_beyond_its_bound_is_refused(
    declared_frame_bytes: int | None,
) -> None:
    bound = (
        MAXIMUM_AGENT_FRAMELESS_WAIT_RESPONSE_BYTES
        if declared_frame_bytes is None
        else maximum_agent_wait_response_bytes(declared_frame_bytes)
    )

    with pytest.raises(RuntimeError, match="wait response exceeds"):
        encode_wait_response({"detail": "x" * bound}, declared_frame_bytes)


@pytest.mark.parametrize(
    "environment",
    (
        {},
        {"PATH": "/usr/bin", "HOME": "/root"},
        {"GRÜSSE": "Ärger", "TAB": "\t"},
    ),
    ids=("empty", "ascii", "beyond-ascii"),
)
def test_a_provider_environment_survives_the_exec_guard_boundary(
    environment: dict[str, str],
) -> None:
    assert (
        decode_provider_environment(encode_provider_environment(environment))
        == environment
    )


def test_a_provider_environment_is_carried_in_one_canonical_order() -> None:
    ascending = encode_provider_environment({"A": "1", "B": "2"})
    descending = encode_provider_environment({"B": "2", "A": "1"})

    assert ascending == descending


@pytest.mark.parametrize(
    ("events", "populated"),
    (
        ("populated 0\n", False),
        ("populated 1\n", True),
        ("populated 0\nfrozen 0\n", False),
        ("frozen 0\npopulated 1\n", True),
    ),
)
def test_a_cgroup_reports_whether_anything_still_runs_in_it(
    tmp_path: Path, events: str, populated: bool
) -> None:
    (tmp_path / "cgroup.events").write_text(events, encoding="ascii")

    assert cgroup_populated(tmp_path) is populated
