"""Create-only publication of one deterministic Atelier candidate commit."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from atelier2.adapters.project_source import isolated_git_environment
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import (
    PushAtelierCommit,
    PushAtelierCommitReceipt,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    ConfirmationSource,
    EffectAdapterBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentMismatch,
    EffectReceipt,
    EffectResult,
    EffectUnknownOutcome,
    PerformedEffect,
)

_HOOK_FREE_ARGUMENTS = ("-c", f"core.hooksPath={os.devnull}")


class GitTransportRefused(RuntimeError):
    """The durable request does not authorize the proposed git mutation."""


@dataclass(frozen=True, slots=True)
class GitRemote:
    identity: str
    url: str
    credential_file: Path | None = None

    def __post_init__(self) -> None:
        if not self.identity or not self.url:
            raise ValueError("a git remote has a stable identity and URL")


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class GitCommandRunner(Protocol):
    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult: ...


class SubprocessGitCommandRunner:
    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=working_directory,
            env=environment,
            input=standard_input,
            capture_output=True,
            check=False,
        )
        return GitCommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )


@dataclass(frozen=True, slots=True)
class GitTransportEffectAdapterFactory:
    candidate_store: Path
    remote: GitRemote
    adapter_revision: AdapterRevision
    destination: EffectDestination
    command_runner: GitCommandRunner = field(default_factory=SubprocessGitCommandRunner)

    @property
    def binding(self) -> EffectAdapterBinding:
        return EffectAdapterBinding(
            self.adapter_revision,
            self.destination,
            AdapterOperationalIdentity(self.remote.identity),
            AdapterOperationName.PUSH_ATELIER_COMMIT,
        )

    @property
    def proves_absence(self) -> bool:
        return False

    def open(self) -> GitTransportEffectAdapter:
        store = self.candidate_store.resolve()
        credential_file = self.remote.credential_file
        if credential_file is not None:
            credential_file = credential_file.resolve()
            if not credential_file.is_file() or credential_file.stat().st_size == 0:
                raise GitTransportRefused(
                    f"git credential file is missing or empty: {credential_file}"
                )
        return GitTransportEffectAdapter(
            store,
            GitRemote(self.remote.identity, self.remote.url, credential_file),
            self.binding,
            self.command_runner,
        )


class GitTransportEffectAdapter:
    def __init__(
        self,
        candidate_store: Path,
        remote: GitRemote,
        binding: EffectAdapterBinding,
        command_runner: GitCommandRunner,
    ) -> None:
        self._candidate_store = candidate_store
        self._remote = remote
        self._binding = binding
        self._command_runner = command_runner
        self._closed = False

    def readback(self, intent: EffectIntent) -> EffectReceipt | EffectUnknownOutcome:
        request = self._authorized_request(intent)
        self._verify_candidate(request)
        expected = request.expected_commit_oid(intent.request.request_hash.value)
        standing = self._remote_ref(request.head_branch.full_ref)
        if standing != expected:
            return EffectUnknownOutcome(intent.reference)
        performed = self._performed(request, expected)
        return EffectReceipt(
            intent,
            performed.effect_id,
            performed.result,
            ConfirmationSource.ADAPTER_READBACK,
        )

    def execute(self, intent: EffectIntent) -> PerformedEffect | EffectUnknownOutcome:
        request = self._authorized_request(intent)
        self._verify_candidate(request)
        expected = request.expected_commit_oid(intent.request.request_hash.value)
        full_ref = request.head_branch.full_ref
        standing = self._remote_ref(full_ref)
        if standing == expected:
            return self._performed(request, expected)
        if standing is not None:
            return EffectUnknownOutcome(intent.reference)
        if not self._remote_carries(request.base_commit):
            raise GitTransportRefused(
                "the declared base commit is not advertised by the configured remote"
            )
        self._fetch_base(request.base_commit)
        written = self._store_commit(request, intent.request.request_hash.value)
        if written != expected:
            raise GitTransportRefused(
                "git did not preserve the deterministic commit object identity"
            )
        zero_oid = "0" * len(expected)
        self._remote_git(
            (
                "push",
                "--porcelain",
                "--no-verify",
                f"--force-with-lease={full_ref}:{zero_oid}",
                self._remote.url,
                f"{expected}:{full_ref}",
            ),
            in_store=True,
        )
        return self._readback_after_send(intent, request, expected)

    def close(self) -> None:
        self._closed = True

    def _authorized_request(self, intent: EffectIntent) -> PushAtelierCommit:
        if self._closed:
            raise RuntimeError("git transport effect adapter is closed")
        if intent.binding.adapter_binding != self._binding:
            raise EffectIntentMismatch(
                "effect intent does not belong to this adapter binding"
            )
        try:
            return PushAtelierCommit.from_canonical_bytes(intent.request.payload)
        except (TypeError, ValueError) as error:
            raise GitTransportRefused("push request is not canonical") from error

    def _verify_candidate(self, request: PushAtelierCommit) -> None:
        if not self._candidate_store.is_dir():
            raise GitTransportRefused(
                f"candidate store does not exist: {self._candidate_store}"
            )
        reference = f"refs/atelier/candidates/{request.attempt_id}"
        result = self._store_git(("for-each-ref", "--format=%(objectname)", reference))
        if result.returncode != 0:
            raise GitTransportRefused("the candidate store could not be read")
        standing = result.stdout.decode("ascii", errors="strict").strip()
        if standing != request.candidate_tree:
            raise GitTransportRefused(
                "the pinned attempt does not name the declared candidate tree"
            )

    def _remote_ref(self, full_ref: str) -> str | None:
        result = self._remote_git(("ls-remote", "--refs", self._remote.url, full_ref))
        if result.returncode != 0:
            return None
        lines = result.stdout.decode("ascii", errors="strict").splitlines()
        if not lines:
            return None
        if len(lines) != 1:
            return None
        oid, separator, name = lines[0].partition("\t")
        if separator != "\t" or name != full_ref:
            return None
        return oid

    def _remote_carries(self, oid: str) -> bool:
        result = self._remote_git(("ls-remote", "--refs", self._remote.url))
        if result.returncode != 0:
            return False
        return any(
            line.partition("\t")[0] == oid
            for line in result.stdout.decode("ascii", errors="strict").splitlines()
        )

    def _fetch_base(self, oid: str) -> None:
        result = self._remote_git(
            ("fetch", "--no-tags", "--no-write-fetch-head", self._remote.url, oid),
            in_store=True,
        )
        if result.returncode != 0:
            raise GitTransportRefused("the declared base commit could not be fetched")

    def _store_commit(self, request: PushAtelierCommit, request_hash: str) -> str:
        result = self._store_git(
            ("hash-object", "-t", "commit", "-w", "--stdin"),
            request.commit_bytes(request_hash),
        )
        if result.returncode != 0:
            raise GitTransportRefused("the deterministic commit could not be stored")
        return result.stdout.decode("ascii", errors="strict").strip()

    def _performed(
        self, request: PushAtelierCommit, commit_oid: str
    ) -> PerformedEffect:
        receipt = PushAtelierCommitReceipt(
            self._remote.identity,
            request.head_branch.full_ref,
            commit_oid,
            request.base_commit,
            request.candidate_tree,
            request.head_branch.value,
            request.author,
            request.committer,
        )
        return PerformedEffect(
            EffectId(commit_oid), EffectResult(receipt.result_bytes())
        )

    def _readback_after_send(
        self,
        intent: EffectIntent,
        request: PushAtelierCommit,
        expected: str,
    ) -> PerformedEffect | EffectUnknownOutcome:
        standing = self._remote_ref(request.head_branch.full_ref)
        if standing == expected:
            return self._performed(request, expected)
        return EffectUnknownOutcome(intent.reference)

    def _credential_arguments(self) -> tuple[str, ...]:
        credential_file = self._remote.credential_file
        if credential_file is None:
            return ()
        path = shlex.quote(str(credential_file))
        helper = (
            '!f() { test "$1" = get || exit 0; '
            "printf 'username=x-access-token\\npassword='; "
            f"/bin/cat {path}; printf '\\n'; }}; f"
        )
        return ("-c", f"credential.helper={helper}")

    def _remote_git(
        self, arguments: tuple[str, ...], *, in_store: bool = False
    ) -> GitCommandResult:
        prefix = (*_HOOK_FREE_ARGUMENTS, *self._credential_arguments())
        environment = isolated_git_environment()
        if in_store:
            environment["GIT_DIR"] = str(self._candidate_store)
        return self._command_runner.run(
            (*prefix, *arguments),
            working_directory=self._candidate_store.parent,
            environment=environment,
        )

    def _store_git(
        self, arguments: tuple[str, ...], standard_input: bytes | None = None
    ) -> GitCommandResult:
        return self._command_runner.run(
            (*_HOOK_FREE_ARGUMENTS, *arguments),
            working_directory=self._candidate_store.parent,
            environment=isolated_git_environment(GIT_DIR=str(self._candidate_store)),
            standard_input=standard_input,
        )
