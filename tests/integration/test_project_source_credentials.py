"""Filesystem ownership and publication for managed source credentials."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import atelier2.adapters.project_source_credentials as credential_deposits
from atelier2.adapters.github.live_effects import GITHUB_TOKEN_CREDENTIAL_ENTRY
from atelier2.adapters.project_source_credentials import (
    FilesystemCredentialDeposit,
    FilesystemProjectSourceCredentialStore,
)
from atelier2.contracts.host_configuration import ProjectSourceId
from atelier2.ports.project_connections import CredentialDepositUnavailable

SOURCE = ProjectSourceId("11111111-1111-4111-8111-111111111111")


def _permissions(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_token_is_restrictively_staged_beside_its_atomic_destination(
    tmp_path: Path,
) -> None:
    managed_root = tmp_path / "managed"
    store = FilesystemProjectSourceCredentialStore(
        managed_root, deposit_name=lambda: "deposit1"
    )

    staged = store.stage(SOURCE, "managed-token")

    assert isinstance(staged, FilesystemCredentialDeposit)
    staged_directory = staged.credential_directory
    token_path = staged_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY
    assert staged_directory.parent == managed_root.resolve()
    assert _permissions(managed_root) == 0o700
    assert _permissions(staged_directory) == 0o700
    assert _permissions(token_path) == 0o600
    assert token_path.read_text(encoding="utf-8") == "managed-token"

    published_directory = staged.publish()

    assert published_directory.parent == managed_root.resolve()
    assert published_directory.name == f"{SOURCE.value}-deposit1"
    assert not staged_directory.exists()
    assert (published_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY).read_text(
        encoding="utf-8"
    ) == "managed-token"


def test_discard_removes_only_the_managed_deposit_and_never_a_cli_sibling(
    tmp_path: Path,
) -> None:
    cli_owned = tmp_path / "cli-credentials"
    cli_owned.mkdir()
    cli_token = cli_owned / GITHUB_TOKEN_CREDENTIAL_ENTRY
    cli_token.write_text("cli-owned-token", encoding="utf-8")
    store = FilesystemProjectSourceCredentialStore(
        tmp_path / "managed", deposit_name=lambda: "deposit2"
    )
    staged = store.stage(SOURCE, "managed-token")
    assert isinstance(staged, FilesystemCredentialDeposit)

    staged.discard()

    assert not staged.credential_directory.exists()
    assert cli_token.read_text(encoding="utf-8") == "cli-owned-token"

    published = store.stage(SOURCE, "managed-token")
    assert isinstance(published, FilesystemCredentialDeposit)
    published_directory = published.publish()
    published.discard()

    assert not published_directory.exists()
    assert cli_token.read_text(encoding="utf-8") == "cli-owned-token"


def test_invalid_or_empty_deposits_are_refused_without_writing(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed"
    invalid_name = FilesystemProjectSourceCredentialStore(
        managed_root, deposit_name=lambda: "../outside"
    )

    assert isinstance(invalid_name.stage(SOURCE, "token"), CredentialDepositUnavailable)
    assert tuple(managed_root.iterdir()) == ()

    empty = FilesystemProjectSourceCredentialStore(
        tmp_path / "empty-managed", deposit_name=lambda: "deposit"
    )
    assert isinstance(empty.stage(SOURCE, ""), CredentialDepositUnavailable)
    assert not empty.managed_root.exists()


def test_publish_and_removal_fsync_the_managed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "managed"
    store = FilesystemProjectSourceCredentialStore(
        managed_root, deposit_name=lambda: "durable"
    )
    staged = store.stage(SOURCE, "managed-token")
    assert isinstance(staged, FilesystemCredentialDeposit)
    fsynced: list[Path] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        fsynced.append(Path(os.readlink(f"/proc/self/fd/{descriptor}")).resolve())
        real_fsync(descriptor)

    monkeypatch.setattr(credential_deposits.os, "fsync", record_fsync)

    staged.publish()
    staged.discard()

    assert fsynced == [managed_root.resolve(), managed_root.resolve()]


def test_discard_surfaces_removal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemProjectSourceCredentialStore(
        tmp_path / "managed", deposit_name=lambda: "held"
    )
    staged = store.stage(SOURCE, "managed-token")
    assert isinstance(staged, FilesystemCredentialDeposit)

    def refuse_removal(_path: Path) -> None:
        raise OSError("simulated removal failure")

    monkeypatch.setattr(credential_deposits.shutil, "rmtree", refuse_removal)

    with pytest.raises(OSError, match="simulated removal failure"):
        staged.discard()

    assert staged.credential_directory.exists()
