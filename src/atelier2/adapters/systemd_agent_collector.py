from __future__ import annotations

import base64
import os
import selectors
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from atelier2.adapters.leased_directory import entered_leased_directory
from atelier2.adapters.systemd_generation_records import (
    DirectSystemdGenerationRecords,
    DirectSystemdInvocationId,
    DirectSystemdResult,
    DirectSystemdResultOutcome,
    DirectSystemdStarted,
    RecordPublicationBarriers,
    decode_canonical_systemd_base64,
    decode_canonical_systemd_json,
    encode_canonical_systemd_json,
    encode_direct_systemd_intent,
    encode_direct_systemd_started,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    AgentProcessCommand,
    AgentProcessInvocation,
)

MAXIMUM_DIRECT_SYSTEMD_LAUNCH_ENVELOPE_BYTES = 262_144
DIRECT_SYSTEMD_LAUNCH_CREDENTIAL_NAME = "atelier2-launch"
DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME = "launch-envelope"
_LAUNCH_ENVELOPE_VERSION = 1
_PROCESS_EXIT_OBSERVATION_SECONDS = 0.05
_CREDENTIAL_MODES = frozenset({0o400, 0o600})
_SOURCE_MODE = frozenset({0o600})


class DirectSystemdPopen(Protocol):
    def __call__(
        self,
        arguments: tuple[str, ...],
        **keywords: Any,
    ) -> subprocess.Popen[bytes]: ...


@dataclass(frozen=True)
class DirectSystemdCollectorBarriers:
    after_started_file_fsync: Callable[[], None] = lambda: None
    after_started_directory_fsync: Callable[[], None] = lambda: None
    after_envelope_directory_fsync: Callable[[], None] = lambda: None


_NO_COLLECTOR_BARRIERS = DirectSystemdCollectorBarriers()


def _popen(arguments: tuple[str, ...], **keywords: Any) -> subprocess.Popen[bytes]:
    return cast(subprocess.Popen[bytes], subprocess.Popen(arguments, **keywords))


@dataclass(frozen=True)
class DirectSystemdLaunch:
    """What a launcher needs: one command and the directory it is started in.

    The attempt's lease owns that directory. This record carries only the path,
    because a launcher never owns an attempt -- it starts a process where
    somebody else leased the ground, and the envelope's bytes are unchanged by
    the split that gave the lease its own type.
    """

    command: AgentProcessCommand
    working_directory: Path
    device: int
    inode: int


def encode_direct_systemd_launch_envelope(
    invocation: AgentProcessInvocation,
) -> bytes:
    command = invocation.command
    value = {
        "arguments": list(command.arguments),
        "environment": [list(entry) for entry in command.environment],
        "standard_input": base64.b64encode(command.standard_input).decode("ascii"),
        "standard_output_limit": command.standard_output_frame_bytes,
        "type": "LAUNCH",
        "version": _LAUNCH_ENVELOPE_VERSION,
        "working_directory": str(invocation.lease.working_directory),
        "working_directory_device": invocation.lease.device,
        "working_directory_inode": invocation.lease.inode,
    }
    payload = encode_canonical_systemd_json(value)
    if len(payload) > MAXIMUM_DIRECT_SYSTEMD_LAUNCH_ENVELOPE_BYTES:
        raise ValueError("direct systemd launch envelope exceeds its exact bound")
    return payload


def decode_direct_systemd_launch_envelope(payload: bytes) -> DirectSystemdLaunch:
    if len(payload) > MAXIMUM_DIRECT_SYSTEMD_LAUNCH_ENVELOPE_BYTES:
        raise ValueError("direct systemd launch envelope exceeds its exact bound")
    value = decode_canonical_systemd_json(payload)
    fields = {
        "arguments",
        "environment",
        "standard_input",
        "standard_output_limit",
        "type",
        "version",
        "working_directory",
        "working_directory_device",
        "working_directory_inode",
    }
    if (
        set(value) != fields
        or value["type"] != "LAUNCH"
        or value["version"] != _LAUNCH_ENVELOPE_VERSION
    ):
        raise ValueError("direct systemd launch envelope fields are malformed")
    raw_arguments = value["arguments"]
    if type(raw_arguments) is not list or any(
        type(argument) is not str for argument in raw_arguments
    ):
        raise ValueError("direct systemd launch arguments are malformed")
    raw_environment = value["environment"]
    if type(raw_environment) is not list or any(
        type(entry) is not list
        or len(entry) != 2
        or any(type(field) is not str for field in entry)
        for entry in raw_environment
    ):
        raise ValueError("direct systemd launch environment is malformed")
    standard_input = decode_canonical_systemd_base64(value, "standard_input")
    if len(standard_input) > MAXIMUM_AGENT_PROCESS_INPUT_BYTES:
        raise ValueError("direct systemd launch input exceeds its exact bound")
    working_directory = value["working_directory"]
    standard_output_limit = value["standard_output_limit"]
    device = value["working_directory_device"]
    inode = value["working_directory_inode"]
    if (
        type(working_directory) is not str
        or type(standard_output_limit) is not int
        or type(device) is not int
        or type(inode) is not int
        or device < 0
        or inode < 0
    ):
        raise ValueError("direct systemd launch envelope values are malformed")
    return DirectSystemdLaunch(
        AgentProcessCommand(
            tuple(raw_arguments),
            tuple((entry[0], entry[1]) for entry in raw_environment),
            standard_input,
            standard_output_frame_bytes=standard_output_limit,
        ),
        Path(working_directory),
        device,
        inode,
    )


def collect_direct_systemd_agent_process(
    records: DirectSystemdGenerationRecords,
    invocation_id: DirectSystemdInvocationId,
    *,
    launch_credential_path: Path,
    source_envelope_path: Path,
    barriers: DirectSystemdCollectorBarriers = _NO_COLLECTOR_BARRIERS,
    popen: DirectSystemdPopen = _popen,
) -> DirectSystemdResult:
    if os.path.lexists(records.started_path):
        raise FileExistsError("direct systemd STARTED already exists")
    intent = records.read_intent()
    envelope = read_direct_systemd_launch_envelope(
        launch_credential_path, allowed_modes=_CREDENTIAL_MODES
    )
    if Sha256Hash.of(envelope) != intent.launch_envelope_hash:
        raise ValueError("direct systemd launch envelope differs from INTENT")
    launch = decode_direct_systemd_launch_envelope(envelope)
    if launch.command.standard_output_frame_bytes != intent.standard_output_limit:
        raise ValueError(
            "direct systemd launch envelope output bound differs from INTENT"
        )
    started = DirectSystemdStarted(
        Sha256Hash.of(encode_direct_systemd_intent(intent)), invocation_id
    )
    records.publish_started(
        started,
        RecordPublicationBarriers(
            after_file_fsync=barriers.after_started_file_fsync,
            after_directory_fsync=barriers.after_started_directory_fsync,
        ),
    )
    if (
        read_direct_systemd_launch_envelope(
            source_envelope_path, allowed_modes=_SOURCE_MODE
        )
        != envelope
    ):
        raise ValueError("direct systemd source envelope differs from credential")
    source_envelope_path.unlink()
    fsync_direct_systemd_directory(source_envelope_path.parent)
    barriers.after_envelope_directory_fsync()

    started_hash = Sha256Hash.of(encode_direct_systemd_started(started))
    try:
        # A crash from durable STARTED onward deliberately sacrifices retry to
        # keep provider execution at most once.
        entered = entered_leased_directory(
            launch.working_directory, launch.device, launch.inode
        )
        with entered as (leased_cwd, leased_descriptor):
            process = popen(
                launch.command.arguments,
                cwd=leased_cwd,
                pass_fds=(leased_descriptor,),
                env=dict(launch.command.environment),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
    except (OSError, subprocess.SubprocessError, ValueError):
        result = DirectSystemdResult(
            started_hash,
            invocation_id,
            DirectSystemdResultOutcome.PROCESS_BOUNDARY_FAILED,
            None,
            b"",
            b"",
            False,
            False,
        )
    else:
        result = _collect_process(process, launch.command, started_hash, invocation_id)
    records.publish_result(result)
    return result


def _collect_process(
    process: subprocess.Popen[bytes],
    command: AgentProcessCommand,
    started_hash: Sha256Hash,
    invocation_id: DirectSystemdInvocationId,
) -> DirectSystemdResult:
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise RuntimeError("direct systemd provider pipes are absent")
    selector = selectors.DefaultSelector()
    streams = {
        "stdin": process.stdin,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }
    for name, stream in streams.items():
        os.set_blocking(stream.fileno(), False)
        events = selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ
        selector.register(stream, events, name)
    standard_input_offset = 0
    standard_output = bytearray()
    standard_error = bytearray()
    standard_output_overflow = False
    standard_error_overflow = False
    if not command.standard_input:
        _close_stream(selector, streams, "stdin")
    try:
        while "stdout" in streams or "stderr" in streams:
            # Overflow closes only its stream; it never ends this loop. While the
            # provider lives, 1B's unit lifetime is the loop bound. After provider
            # exit, this internal observation interval makes the drain branch live,
            # so a pipe-inheriting descendant cannot delay the durable result.
            events = selector.select(_PROCESS_EXIT_OBSERVATION_SECONDS)
            if not events and process.poll() is not None:
                for name in ("stdout", "stderr"):
                    overflow = _drain_available(
                        selector,
                        streams,
                        name,
                        standard_output if name == "stdout" else standard_error,
                        command.standard_output_frame_bytes
                        if name == "stdout"
                        else MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
                    )
                    if name == "stdout":
                        standard_output_overflow |= overflow
                    else:
                        standard_error_overflow |= overflow
                break
            for key, _mask in events:
                name = str(key.data)
                if name == "stdin":
                    try:
                        written = os.write(
                            key.fd,
                            command.standard_input[standard_input_offset:],
                        )
                    except BrokenPipeError:
                        _close_stream(selector, streams, name)
                    else:
                        standard_input_offset += written
                        if standard_input_offset == len(command.standard_input):
                            _close_stream(selector, streams, name)
                    continue
                target = standard_output if name == "stdout" else standard_error
                limit = (
                    command.standard_output_frame_bytes
                    if name == "stdout"
                    else MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
                )
                overflow = _read_bounded_stream(selector, streams, name, target, limit)
                if name == "stdout":
                    standard_output_overflow |= overflow
                else:
                    standard_error_overflow |= overflow
    finally:
        for name in tuple(streams):
            _close_stream(selector, streams, name)
        selector.close()

    overflow = standard_output_overflow or standard_error_overflow
    # RuntimeMaxSec with KillMode=control-group owns a live provider; after exit,
    # poll avoids inherited pipes while wait owns the ordinary exit code.
    return_code = process.poll()
    if not overflow:
        return_code = process.wait()
    return DirectSystemdResult(
        started_hash,
        invocation_id,
        DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED
        if overflow
        else DirectSystemdResultOutcome.COMPLETED,
        return_code,
        bytes(standard_output),
        bytes(standard_error),
        standard_output_overflow,
        standard_error_overflow,
    )


def _read_bounded_stream(
    selector: selectors.BaseSelector,
    streams: dict[str, Any],
    name: str,
    target: bytearray,
    limit: int,
) -> bool:
    stream = streams[name]
    chunk = os.read(stream.fileno(), limit + 1 - len(target))
    if not chunk:
        _close_stream(selector, streams, name)
        return False
    target.extend(chunk)
    if len(target) <= limit:
        return False
    del target[limit:]
    _close_stream(selector, streams, name)
    return True


def _drain_available(
    selector: selectors.BaseSelector,
    streams: dict[str, Any],
    name: str,
    target: bytearray,
    limit: int,
) -> bool:
    while name in streams:
        try:
            overflow = _read_bounded_stream(selector, streams, name, target, limit)
        except BlockingIOError:
            return False
        if overflow:
            return True
    return False


def _close_stream(
    selector: selectors.BaseSelector, streams: dict[str, Any], name: str
) -> None:
    stream = streams.pop(name, None)
    if stream is None:
        return
    try:
        selector.unregister(stream)
    except KeyError:
        pass
    stream.close()


def read_direct_systemd_launch_envelope(
    path: Path, *, allowed_modes: frozenset[int]
) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) not in allowed_modes
        ):
            raise ValueError("launch envelope is not a mode-0600 file or credential")
        payload = stream.read(MAXIMUM_DIRECT_SYSTEMD_LAUNCH_ENVELOPE_BYTES + 1)
    if len(payload) > MAXIMUM_DIRECT_SYSTEMD_LAUNCH_ENVELOPE_BYTES:
        raise ValueError("direct systemd launch envelope exceeds its exact bound")
    return payload


def main() -> None:
    generation_directory = Path.cwd()
    collect_direct_systemd_agent_process(
        DirectSystemdGenerationRecords(generation_directory),
        DirectSystemdInvocationId(os.environ["INVOCATION_ID"]),
        launch_credential_path=Path(os.environ["CREDENTIALS_DIRECTORY"])
        / DIRECT_SYSTEMD_LAUNCH_CREDENTIAL_NAME,
        source_envelope_path=generation_directory / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME,
    )


def fsync_direct_systemd_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
