"""The create-only git transport publishes one deterministic candidate commit."""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from atelier2.adapters.git_transport.effects import (
    GitCommandRunner,
    GitRemote,
    GitTransportEffectAdapterFactory,
)
from atelier2.contracts.effect_markers import marker_line
from atelier2.contracts.effect_requests import (
    GitCommitIdentity,
    HeadBranch,
    PushAtelierCommit,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectReceipt,
    EffectUnknownOutcome,
    LogicalEffectKey,
    PerformedEffect,
)
from atelier2.contracts.runs import RunId, WorkflowRevision

ATTEMPT_ID = "a1" * 32
HEAD_BRANCH = HeadBranch("atelier2/work-item/" + "b2" * 32)
AUTHOR = GitCommitIdentity("Atelier Agent", "agent@example.test")
COMMITTER = GitCommitIdentity("Atelier Core", "core@example.test")


def _git(repository: Path, *arguments: str, stdin: bytes | None = None) -> str:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.test",
    }
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        env=environment,
        input=stdin,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode().strip()


def _repositories(root: Path) -> tuple[Path, Path, str, str]:
    source = root / "source"
    source.mkdir()
    _git(source, "init", "--quiet", "--initial-branch=main")
    (source / "kept.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "kept.txt")
    _git(source, "commit", "--quiet", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    remote = root / "remote.git"
    _git(root, "init", "--bare", "--quiet", str(remote))
    _git(source, "push", "--quiet", str(remote), "HEAD:refs/heads/main")

    (source / "kept.txt").write_text("candidate exact bytes\n", encoding="utf-8")
    (source / "new.txt").write_bytes(b"\x00candidate\n")
    _git(source, "add", "--all")
    candidate_tree = _git(source, "write-tree")
    candidate_commit = _git(
        source, "commit-tree", candidate_tree, "-p", base, stdin=b"candidate\n"
    )
    store = root / "candidates.git"
    _git(root, "init", "--bare", "--quiet", str(store))
    _git(store, "fetch", "--quiet", str(source), candidate_commit)
    _git(store, "update-ref", f"refs/atelier/candidates/{ATTEMPT_ID}", candidate_tree)
    return store, remote, base, candidate_tree


def _intent(
    factory: GitTransportEffectAdapterFactory, base: str, tree: str
) -> tuple[EffectIntent, PushAtelierCommit]:
    request = PushAtelierCommit(
        ATTEMPT_ID,
        tree,
        base,
        HEAD_BRANCH,
        AUTHOR,
        COMMITTER,
        "2026-08-27T12:34:56Z",
    )
    return (
        EffectIntent(
            EffectBinding(
                LogicalEffectKey("run/push"),
                RunId("run"),
                WorkflowRevision(b"workflow").revision_hash,
                factory.binding.adapter_revision,
                factory.binding.destination,
                factory.binding.operational_identity,
                factory.binding.operation_name,
            ),
            CanonicalRequest(request.canonical_bytes()),
        ),
        request,
    )


def _factory(
    store: Path,
    remote: Path,
    runner: GitCommandRunner | None = None,
) -> GitTransportEffectAdapterFactory:
    arguments = (
        store,
        GitRemote("local-test", str(remote)),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
    )
    return (
        GitTransportEffectAdapterFactory(*arguments)
        if runner is None
        else GitTransportEffectAdapterFactory(*arguments, runner)
    )


def test_push_creates_the_declared_commit_and_replay_finds_no_twin(
    tmp_path: Path,
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    factory = GitTransportEffectAdapterFactory(
        store,
        GitRemote("local-test", str(remote)),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
    )
    intent, request = _intent(factory, base, tree)
    expected = request.expected_commit_oid(intent.request.request_hash.value)
    adapter = factory.open()
    try:
        assert isinstance(adapter.readback(intent), EffectUnknownOutcome)
        performed = adapter.execute(intent)
        replay = adapter.execute(intent)
        readback = adapter.readback(intent)
    finally:
        adapter.close()

    assert isinstance(performed, PerformedEffect)
    assert replay == performed
    assert performed.effect_id.value == expected
    assert isinstance(readback, EffectReceipt)
    assert _git(remote, "rev-parse", HEAD_BRANCH.full_ref) == expected
    assert _git(remote, "rev-parse", f"{expected}^{{tree}}") == tree
    assert _git(remote, "rev-parse", f"{expected}^") == base
    commit = _git(remote, "cat-file", "commit", expected)
    assert f"author {AUTHOR.name} <{AUTHOR.email}>" in commit
    assert f"committer {COMMITTER.name} <{COMMITTER.email}>" in commit
    assert marker_line(intent.request.request_hash.value) in commit.splitlines()


def test_existing_different_branch_is_unknown_and_never_force_updated(
    tmp_path: Path,
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    _git(remote, "update-ref", HEAD_BRANCH.full_ref, base)
    factory = GitTransportEffectAdapterFactory(
        store,
        GitRemote("local-test", str(remote)),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
    )
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        result = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(result, EffectUnknownOutcome)
    assert _git(remote, "rev-parse", HEAD_BRANCH.full_ref) == base
    assert factory.binding.operational_identity == AdapterOperationalIdentity(
        "local-test"
    )


def test_concurrent_replay_publishes_one_commit_and_no_twin(tmp_path: Path) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    factory = _factory(store, remote)
    intent, request = _intent(factory, base, tree)

    def publish() -> object:
        adapter = factory.open()
        try:
            return adapter.execute(intent)
        finally:
            adapter.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = tuple(workers.map(lambda _index: publish(), range(2)))

    expected = request.expected_commit_oid(intent.request.request_hash.value)
    assert all(
        isinstance(result, PerformedEffect) and result.effect_id.value == expected
        for result in results
    )
    assert (
        _git(remote, "for-each-ref", "--format=%(objectname)", HEAD_BRANCH.full_ref)
        == expected
    )
