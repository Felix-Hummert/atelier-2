"""Filesystem credential deposits owned only by the HTTP source doors."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from atelier2.adapters.github.live_effects import GITHUB_TOKEN_CREDENTIAL_ENTRY
from atelier2.contracts.host_configuration import ProjectSourceId
from atelier2.ports.project_connections import (
    CredentialDepositUnavailable,
    ManagedCredentialDeposit,
    StageCredentialResult,
)

MANAGED_PROJECT_SOURCE_CREDENTIALS_DIRECTORY = "source-credentials"


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass
class FilesystemCredentialDeposit(ManagedCredentialDeposit):
    _managed_root: Path
    _staged_directory: Path
    _published_directory: Path
    _published: bool = False

    @property
    def credential_directory(self) -> Path:
        return self._staged_directory

    def publish(self) -> Path:
        if self._published:
            return self._published_directory
        os.replace(self._staged_directory, self._published_directory)
        self._published = True
        _fsync_directory(self._managed_root)
        return self._published_directory

    def discard(self) -> None:
        target = (
            self._published_directory if self._published else self._staged_directory
        )
        try:
            target.relative_to(self._managed_root)
        except ValueError as error:
            raise RuntimeError("managed credential deposit escaped its root") from error
        shutil.rmtree(target)
        _fsync_directory(self._managed_root)


@dataclass(frozen=True)
class FilesystemProjectSourceCredentialStore:
    managed_root: Path
    deposit_name: Callable[[], str] = lambda: uuid4().hex

    def __post_init__(self) -> None:
        object.__setattr__(self, "managed_root", self.managed_root.resolve())

    def stage(self, source_id: ProjectSourceId, token: str) -> StageCredentialResult:
        if type(token) is not str or not token:
            return CredentialDepositUnavailable("the token is empty")
        try:
            self.managed_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.managed_root, 0o700)
            deposit_name = self.deposit_name()
            if not deposit_name or not deposit_name.isalnum():
                return CredentialDepositUnavailable(
                    "the credential deposit id is invalid"
                )
            staged = Path(
                tempfile.mkdtemp(
                    prefix=f".{source_id.value}-{deposit_name}-",
                    suffix=".tmp",
                    dir=self.managed_root,
                )
            )
            os.chmod(staged, 0o700)
            token_path = staged / GITHUB_TOKEN_CREDENTIAL_ENTRY
            descriptor = os.open(
                token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
                    token_file.write(token)
                    token_file.flush()
                    os.fsync(token_file.fileno())
            except BaseException:
                shutil.rmtree(staged)
                raise
            published = self.managed_root / f"{source_id.value}-{deposit_name}"
            return FilesystemCredentialDeposit(self.managed_root, staged, published)
        except OSError as error:
            return CredentialDepositUnavailable(str(error))
