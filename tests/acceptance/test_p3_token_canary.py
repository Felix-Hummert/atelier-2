"""A platform token crosses only the credential helper's file boundary."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import initialize_schema
from atelier2.adapters.git_transport.effects import (
    GitCommandResult,
    GitRemote,
    GitTransportEffectAdapterFactory,
    SubprocessGitCommandRunner,
)
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectDestination,
    PerformedEffect,
)
from tests.integration.test_git_transport_push import _git, _intent, _repositories


class RecordingRunner(SubprocessGitCommandRunner):
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str]]] = []

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        self.calls.append((arguments, dict(environment)))
        return super().run(
            arguments,
            working_directory=working_directory,
            environment=environment,
            standard_input=standard_input,
        )


def test_token_canary_is_absent_from_durable_and_process_surfaces(
    tmp_path: Path, caplog: object
) -> None:
    canary = "p3-token-canary-never-copy"
    token_file = tmp_path / "token"
    token_file.write_text(canary, encoding="utf-8")
    store, remote, base, tree = _repositories(tmp_path)
    runner = RecordingRunner()
    factory = GitTransportEffectAdapterFactory(
        store,
        GitRemote("local-test", str(remote), token_file),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
        runner,
    )
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        performed = adapter.execute(intent)
    finally:
        adapter.close()
    assert isinstance(performed, PerformedEffect)

    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    engine.dispose()
    durable_bytes = database.read_bytes()
    commit_bytes = _git(
        remote, "cat-file", "commit", performed.effect_id.value
    ).encode()
    process_surfaces = repr(runner.calls).encode()
    log_bytes = repr(getattr(caplog, "records", ())).encode()
    assert canary.encode() not in durable_bytes
    assert canary.encode() not in performed.result.payload
    assert canary.encode() not in commit_bytes
    assert canary.encode() not in process_surfaces
    assert canary.encode() not in log_bytes
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM run_events").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM effect_receipts"
        ).fetchone() == (0,)
