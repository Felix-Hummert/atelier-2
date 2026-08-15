from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atelier2.contracts.hashing import canonical_json
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    AgentProcessInvocation,
)

MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES = 262_144
MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES = 4_096
CONTROL_FRAME_TIMEOUT_SECONDS = 1.0
PROVIDER_ENVIRONMENT_CHANNEL = "ATELIER2_AGENT_ENVIRONMENT_B64"


def encode_control_frame(payload: dict[str, object]) -> bytes:
    return canonical_json(payload)


def _base64_characters(byte_count: int) -> int:
    return 4 * ((byte_count + 2) // 3)


MAXIMUM_AGENT_FRAMELESS_WAIT_RESPONSE_BYTES = max(
    len(encode_control_frame({"type": arm}))
    for arm in (
        "OUTPUT_LIMIT_EXCEEDED",
        "SUPERVISION_FAILED",
        "STOPPED",
        "RECOVERY_HANDOFF",
    )
)


def maximum_agent_wait_response_bytes(standard_output_frame_bytes: int) -> int:
    """The exact wait-response bound for one invocation's declared frame."""

    empty_completion = encode_control_frame(
        {
            "return_code": -(2**31),
            "standard_error": "",
            "standard_output": "",
            "type": "COMPLETED",
        }
    )
    return max(
        MAXIMUM_AGENT_FRAMELESS_WAIT_RESPONSE_BYTES,
        len(empty_completion)
        + _base64_characters(standard_output_frame_bytes)
        + _base64_characters(MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES),
    )


def encode_wait_response(
    payload: dict[str, object], standard_output_frame_bytes: int | None
) -> bytes:
    """Encode one wait response, refusing any that outgrows its exact bound."""

    encoded = encode_control_frame(payload)
    bound = (
        MAXIMUM_AGENT_FRAMELESS_WAIT_RESPONSE_BYTES
        if standard_output_frame_bytes is None
        else maximum_agent_wait_response_bytes(standard_output_frame_bytes)
    )
    if len(encoded) > bound:
        raise RuntimeError("watchdog wait response exceeds its exact bound")
    return encoded


def encode_provider_environment(environment: Mapping[str, str]) -> str:
    """Carry a provider environment across the exec-guard boundary."""

    return base64.b64encode(canonical_json(sorted(environment.items()))).decode("ascii")


def decode_provider_environment(encoded: str) -> dict[str, str]:
    return {
        str(name): str(value)
        for name, value in json.loads(
            base64.b64decode(encoded, validate=True).decode("ascii")
        )
    }


def cgroup_populated(cgroup: Path) -> bool:
    events = (cgroup / "cgroup.events").read_text(encoding="ascii").splitlines()
    return "populated 1" in events


@dataclass(frozen=True)
class ProviderLaunch:
    """One exact provider invocation, as the launch frame declared it."""

    arguments: tuple[str, ...]
    working_directory: str
    environment: dict[str, str]
    standard_input: bytes
    standard_output_frame_bytes: int


def launch_request(invocation: AgentProcessInvocation) -> dict[str, object]:
    """The launch frame one supervisor sends and one watchdog decodes."""

    return {
        "arguments": invocation.arguments,
        "environment": invocation.environment,
        "operation": "LAUNCH",
        "standard_input": base64.b64encode(invocation.standard_input).decode("ascii"),
        "standard_output_frame_bytes": invocation.standard_output_frame_bytes,
        "working_directory": str(invocation.working_directory),
    }


def decode_launch_request(request: dict[str, Any]) -> ProviderLaunch:
    if set(request) != {
        "arguments",
        "environment",
        "operation",
        "standard_input",
        "standard_output_frame_bytes",
        "working_directory",
    }:
        raise ValueError("launch request has unexpected fields")
    arguments_value = request["arguments"]
    if (
        type(arguments_value) is not list
        or not arguments_value
        or any(type(value) is not str or not value for value in arguments_value)
    ):
        raise ValueError("launch arguments are malformed")
    working_directory_value = request["working_directory"]
    if (
        type(working_directory_value) is not str
        or not Path(working_directory_value).is_absolute()
    ):
        raise ValueError("launch working directory is malformed")
    environment_value = request["environment"]
    if type(environment_value) is not list:
        raise ValueError("launch environment is malformed")
    environment_pairs: list[tuple[str, str]] = []
    for pair in environment_value:
        if (
            type(pair) is not list
            or len(pair) != 2
            or type(pair[0]) is not str
            or not pair[0]
            or type(pair[1]) is not str
        ):
            raise ValueError("launch environment is malformed")
        environment_pairs.append((pair[0], pair[1]))
    environment = dict(environment_pairs)
    if len(environment) != len(environment_pairs):
        raise ValueError("launch environment names are duplicated")
    standard_input_value = request["standard_input"]
    if type(standard_input_value) is not str:
        raise ValueError("launch standard input is malformed")
    standard_input = base64.b64decode(standard_input_value, validate=True)
    if len(standard_input) > MAXIMUM_AGENT_PROCESS_INPUT_BYTES:
        raise ValueError("launch standard input exceeds its exact bound")
    standard_output_frame_bytes = request["standard_output_frame_bytes"]
    if type(standard_output_frame_bytes) is not int or standard_output_frame_bytes < 1:
        raise ValueError("launch standard output frame is malformed")
    return ProviderLaunch(
        tuple(arguments_value),
        working_directory_value,
        environment,
        standard_input,
        standard_output_frame_bytes,
    )
