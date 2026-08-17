"""The project manifest as the one owner of the project's verification command.

The atelier does not decide what verifies a project, and neither does the agent
whose node redeemed the grant. The project says it, in the manifest that already
travels with its source, so the statement is pinnable with the tree like every
other material. A manifest that says nothing refuses the redemption by name
rather than letting the atelier choose a command on the project's behalf.

Nothing here claims isolation. The command runs as this process's own user, with
this process's own environment, in the blank directory the attempt leases -- the
same honest limit the lease itself states. Enforcement at a boundary that cannot
be talked out of is #242's, and this adapter must not be read as having done it.
"""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from atelier2.adapters.bounded_processes import bounded_process_answer
from atelier2.contracts.agents import MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.tool_grants_v3 import MAXIMUM_VERIFICATION_COMMAND_BYTES
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.project_verification import (
    ProjectVerificationOutcome,
    ProjectVerificationUnavailable,
    ProjectVerificationUndeclared,
)

PROJECT_MANIFEST_NAME = "pyproject.toml"
DECLARED_VERIFICATION_PATH = ("tool", "atelier2", "verification")
"""Where in the manifest a project states its own verification."""

_COMMAND_KEY = "command"
_TIMEOUT_KEY = "timeout_seconds"


@dataclass(frozen=True, slots=True)
class DeclaredVerification:
    """Exactly what one project declared: the argv, and how long it may take."""

    command: tuple[str, ...]
    timeout_seconds: float


class LocalProjectVerificationRunner:
    """Runs the verification the manifest of one local project declares."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root

    @property
    def manifest_path(self) -> Path:
        return self._project_root / PROJECT_MANIFEST_NAME

    def preflight(self) -> None:
        self._declared()

    def run(self, lease: AgentAttemptWorkspaceLease) -> ProjectVerificationOutcome:
        declared = self._declared()
        try:
            process = subprocess.Popen(
                declared.command,
                cwd=lease.working_directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            raise ProjectVerificationUnavailable(
                f"the verification {self.manifest_path} declares could not be "
                f"started in {lease.working_directory}: {error}"
            ) from error
        try:
            exit_code, standard_output = bounded_process_answer(
                process,
                declared.timeout_seconds,
                MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
            )
        except OSError as error:
            raise ProjectVerificationUnavailable(
                f"the verification {self.manifest_path} declares did not answer "
                f"within its declared {declared.timeout_seconds} seconds: {error}"
            ) from error
        return ProjectVerificationOutcome(
            declared.command, exit_code, Sha256Hash.of(standard_output)
        )

    def _declared(self) -> DeclaredVerification:
        manifest = self.manifest_path
        try:
            document = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except OSError as error:
            raise ProjectVerificationUndeclared(
                f"no project manifest is readable at {manifest}, so this project "
                f"declares no verification to redeem: {error}"
            ) from error
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise ProjectVerificationUndeclared(
                f"{manifest} is not a readable manifest, so what it declares "
                f"cannot be redeemed: {error}"
            ) from error
        return _read_declaration(manifest, document)


def _read_declaration(
    manifest: Path, document: dict[str, object]
) -> DeclaredVerification:
    """The one verification this manifest declares, or the reason it declares none."""
    declared: object = document
    for key in DECLARED_VERIFICATION_PATH:
        if not isinstance(declared, dict) or key not in declared:
            raise ProjectVerificationUndeclared(
                f"{manifest} declares no [{'.'.join(DECLARED_VERIFICATION_PATH)}], "
                "so this project states no verification command of its own"
            )
        declared = declared[key]
    if not isinstance(declared, dict):
        raise ProjectVerificationUndeclared(
            f"{manifest} states its verification as "
            f"{type(declared).__name__}, and a verification is a table carrying "
            f"{_COMMAND_KEY} and {_TIMEOUT_KEY}"
        )
    command = declared.get(_COMMAND_KEY)
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(argument, str) and argument for argument in command)
    ):
        raise ProjectVerificationUndeclared(
            f"{manifest} states no nonempty {_COMMAND_KEY} of nonempty strings, "
            "so there is no exact command to run"
        )
    exact = tuple(str(argument) for argument in command)
    encoded = sum(len(argument.encode("utf-8")) for argument in exact)
    if encoded > MAXIMUM_VERIFICATION_COMMAND_BYTES:
        raise ProjectVerificationUndeclared(
            f"{manifest} states a {encoded}-byte command, and a redemption "
            f"receipt holds {MAXIMUM_VERIFICATION_COMMAND_BYTES} bytes of one"
        )
    timeout_seconds = declared.get(_TIMEOUT_KEY)
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise ProjectVerificationUndeclared(
            f"{manifest} states no positive {_TIMEOUT_KEY}, and a verification "
            "this runtime waits for without a declared deadline never ends"
        )
    return DeclaredVerification(exact, float(timeout_seconds))
