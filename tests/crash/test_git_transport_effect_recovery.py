"""Recovery after the remote accepted a push but the sender did not return."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from atelier2.adapters.git_transport.effects import (
    GitCommandResult,
    SubprocessGitCommandRunner,
)
from atelier2.contracts.effects import EffectReceipt
from tests.integration.test_git_transport_push import _intent, _repositories


class CrashAfterAcceptedPush(SubprocessGitCommandRunner):
    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        result = super().run(
            arguments,
            working_directory=working_directory,
            environment=environment,
            standard_input=standard_input,
        )
        if "push" in arguments and result.returncode == 0:
            raise RuntimeError("injected crash after accepted push")
        return result


def test_restart_reads_the_exact_commit_without_pushing_a_twin(tmp_path: Path) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    from tests.integration.test_git_transport_push import _factory

    factory = _factory(store, remote, CrashAfterAcceptedPush())
    intent, request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            adapter.execute(intent)
    finally:
        adapter.close()

    recovered = _factory(store, remote).open()
    try:
        receipt = recovered.readback(intent)
    finally:
        recovered.close()
    assert isinstance(receipt, EffectReceipt)
    assert receipt.effect_id.value == request.expected_commit_oid(
        intent.request.request_hash.value
    )
