from __future__ import annotations

import importlib.util
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.run_transitions import RunTransitionConflict

SCRIPT_PATH = Path(__file__).with_name("serve_cockpit.py")


def load_harness() -> ModuleType:
    specification = importlib.util.spec_from_file_location("serve_cockpit", SCRIPT_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


harness = load_harness()


class ClosingRuntime:
    def __init__(self, scratch_root: Path) -> None:
        self.scratch_root = scratch_root
        self.closed = False

    def close(self) -> None:
        assert self.scratch_root.is_dir()
        self.closed = True


class FailingRuntime:
    def __init__(self, scratch_root: Path) -> None:
        self.scratch_root = scratch_root

    def close(self) -> None:
        assert self.scratch_root.is_dir()
        raise RuntimeError("runtime close failed")


def test_a_harness_created_scratch_root_is_removed_after_runtime_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created_root = tmp_path / "created-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(created_root.mkdir() or created_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()
    runtime = ClosingRuntime(scratch_root.path)

    harness.close_runtime_and_scratch_root(runtime, scratch_root)

    assert runtime.closed
    assert not created_root.exists()


def test_a_caller_supplied_scratch_root_is_never_removed(tmp_path: Path) -> None:
    caller_root = tmp_path / "caller-root"
    caller_root.mkdir()
    scratch_root = harness.BrowserScratchRoot.borrow(caller_root)
    runtime = ClosingRuntime(scratch_root.path)

    harness.close_runtime_and_scratch_root(runtime, scratch_root)

    assert runtime.closed
    assert caller_root.is_dir()


def test_a_harness_created_scratch_root_is_removed_when_start_or_close_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created_root = tmp_path / "start-error-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(created_root.mkdir() or created_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()

    harness.close_runtime_and_scratch_root(None, scratch_root)

    assert not created_root.exists()

    failing_root = tmp_path / "close-error-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(failing_root.mkdir() or failing_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()
    with pytest.raises(RuntimeError, match="runtime close failed"):
        harness.close_runtime_and_scratch_root(
            FailingRuntime(scratch_root.path), scratch_root
        )

    assert not failing_root.exists()


def test_a_harness_created_scratch_root_is_removed_after_test_interruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    interrupted_root = tmp_path / "interrupted-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(interrupted_root.mkdir() or interrupted_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()
    runtime = ClosingRuntime(scratch_root.path)

    with pytest.raises(KeyboardInterrupt):
        try:
            raise KeyboardInterrupt
        finally:
            harness.close_runtime_and_scratch_root(runtime, scratch_root)

    assert runtime.closed
    assert not interrupted_root.exists()


def _run_states(database: Path) -> dict[str, str]:
    engine = harness.sa.create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            return {
                str(row.run_id): str(row.state)
                for row in connection.execute(
                    harness.sa.select(harness.runs.c.run_id, harness.runs.c.state)
                )
            }
    finally:
        engine.dispose()


def _effect_call_count(effects: Path) -> int:
    with closing(sqlite3.connect(effects)) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM loopback_effect_calls"
        ).fetchone()[0]


def _served_settings(
    tmp_path: Path, database: Path, effects: Path, application_version: str
) -> object:
    frontend_dist = tmp_path / "frontend"
    (frontend_dist / "assets").mkdir(parents=True)
    (frontend_dist / "index.html").write_text("index")
    return harness.HostSettings(
        database_path=database,
        effect_store_path=effects,
        effect_adapter_revision="loopback-v1",
        effect_destination="r3-phase5-e2e",
        application_version=application_version,
        source_commit="reset-test",
        source_tree="reset-test",
        frontend_dist=frontend_dist,
        project_id=harness.ProjectId("e2e-workshop"),
        project_root=SCRIPT_PATH.resolve().parents[2],
    )


def test_a_reset_recompose_restores_the_exact_cold_boot_baseline(
    tmp_path: Path,
) -> None:
    """#742: `/__e2e/recompose?reset=true` must not merely survive a restart --
    it must wipe every durable trace beyond the cold-boot baseline in BOTH
    physical stores (the DBOS database and the loopback effect store) and
    reseed exactly what a fresh boot gives, proven rather than merely claimed.

    Drives the harness's own `/__e2e/recompose`/`/__e2e/generation` doors
    through a `TestClient`, the same observable surface a spec's browser
    uses, but calls `recompose_after_server_stop` directly instead of
    stopping a real uvicorn process -- that real process-restart shape is
    already proven end-to-end by `connection-restart.spec.ts` and
    `workbench-conductor.spec.ts`. What this test owns is that the state
    left behind is the exact cold-boot baseline, not merely "smaller".
    """
    database = tmp_path / "atelier.sqlite"
    effects = tmp_path / "effects.sqlite"
    application_version = "e2e-reset-test"
    harness.seed_boot_baseline(database, effects, application_version)
    baseline_runs = _run_states(database)
    baseline_effect_calls = _effect_call_count(effects)

    settings = _served_settings(tmp_path, database, effects, application_version)

    def compose() -> tuple:
        return harness.serving.compose_application(settings)

    app, runtime = compose()
    # `harness` is loaded dynamically (`load_harness`), so pyright cannot know
    # `BrowserProofHarness`'s real type here -- `Any` names that honestly.
    proof: Any = None
    try:
        # Mutates BOTH stores: a third v1 run's own start (the `runs` table,
        # the DBOS database) and its action node's attempted effect (the
        # `loopback_effect_calls` table, the effect store) -- the same two
        # files `seed_boot_baseline` owns. Unlike the boot fixture's own
        # temporary runtime, this live, `compose_application`-built one proves
        # its effect adapter can confirm its own readback, so the run clears
        # its action node instead of parking on it -- irrelevant here, since
        # this test only needs the effect store's own row, not that specific
        # run state.
        mutation_run_id = "harness-reset-mutation"
        harness.start_published_v1_run(
            runtime.engine,
            runtime.settings,
            harness.RunId(mutation_run_id),
            harness.WorkflowRevision(harness.WORKFLOW),
        )
        deadline = time.monotonic() + harness.TIMEOUT_SECONDS
        while (
            _effect_call_count(effects) == baseline_effect_calls
            and time.monotonic() < deadline
        ):
            time.sleep(0.025)
        assert _run_states(database) != baseline_runs
        assert _effect_call_count(effects) == baseline_effect_calls + 1

        factory = harness.BlockingAgentExecutorFactory(
            "e2e", "blocking/v1", "e2e-blocking-process", b"reset-test-fixture"
        )
        proof_holder: list[Any] = []
        # The real process restarts on its own plain thread, outside uvicorn's
        # event loop, by the time `recompose_after_server_stop` runs (`main()`'s
        # own `while True` loop, after `server.run()` returns). A `TestClient`
        # request keeps an event loop live for the whole call, and DBOS's own
        # synchronous launch path refuses to run inside one -- so this restart
        # happens on its own thread too, joined before the next request reads
        # what it left behind, the same ordering the real restart guarantees.
        restart_threads: list[threading.Thread] = []

        def request_restart(reset: bool) -> None:
            thread = threading.Thread(
                target=proof_holder[0].recompose_after_server_stop, args=(reset,)
            )
            thread.start()
            restart_threads.append(thread)

        proof = harness.BrowserProofHarness(
            app,
            runtime,
            factory,
            compose,
            request_restart,
            lambda: harness.reset_to_boot_baseline(
                database, effects, application_version
            ),
        )
        proof_holder.append(proof)

        with TestClient(proof) as client:
            restarted = client.post("/__e2e/recompose?reset=true")
            assert restarted.status_code == 202
            expected_generation = restarted.text

            restart_threads[0].join(timeout=harness.TIMEOUT_SECONDS)
            assert not restart_threads[0].is_alive()

            observed_generation = client.get("/__e2e/generation")
            assert observed_generation.text == expected_generation

        assert _run_states(database) == baseline_runs
        assert _effect_call_count(effects) == baseline_effect_calls
    finally:
        if proof is not None:
            proof.runtime.close()
        else:
            runtime.close()


def _leftover_attempt_directory(scratch_root: Path) -> Path:
    leftover = scratch_root / ("ab" * 32)
    leftover.mkdir()
    return leftover


def _compose_with_scratch(settings: object, scratch_root: harness.BrowserScratchRoot):
    factory = harness.RecordingAgentExecutorFactoryV2(
        "e2e-v3", "immediate/v1", "e2e-immediate-process", b'"V3 provider bytes"'
    )

    def build_runtime(
        runtime_settings: object,
        effect_factory: object,
        agent_factory: object,
        agent_factories_v2: tuple,
    ) -> object:
        return harness.DbosRuntime(
            harness.replace(runtime_settings, agent_scratch_root=scratch_root.path),
            effect_factory,
            agent_factory,
            (*agent_factories_v2, factory),
        )

    with patch.object(harness.serving, "DbosRuntime", side_effect=build_runtime):
        return harness.serving.compose_application(settings)


def test_a_replaced_generation_scratch_root_drops_leftover_attempt_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    roots = iter((tmp_path / "generation-1", tmp_path / "generation-2"))

    def fake_mkdtemp(prefix: str) -> str:
        path = next(roots)
        path.mkdir()
        return str(path)

    monkeypatch.setattr(harness.tempfile, "mkdtemp", fake_mkdtemp)
    previous = harness.BrowserScratchRoot.create()
    leftover = _leftover_attempt_directory(previous.path)

    next_root = harness.replace_closed_generation_scratch_root(previous)

    assert not leftover.exists()
    assert not previous.path.exists()
    assert next_root.path.is_dir()
    assert list(next_root.path.iterdir()) == []
    next_root.close()


def test_reusing_a_scratch_root_that_still_holds_attempt_directories_refuses_the_next_runtime(
    tmp_path: Path,
) -> None:
    """#747 (c): leftover attempt directories after a close are not in the next
    store. Reusing the scratch root makes the next workspace owner refuse
    during reconcile -- the fixture-host crash under a later `/__e2e/recompose`.
    """
    database = tmp_path / "atelier.sqlite"
    effects = tmp_path / "effects.sqlite"
    application_version = "e2e-scratch-reuse"
    harness.seed_boot_baseline(database, effects, application_version)
    settings = _served_settings(tmp_path, database, effects, application_version)
    scratch_root = harness.BrowserScratchRoot.create()
    _, runtime = _compose_with_scratch(settings, scratch_root)
    runtime.close()
    leftover = _leftover_attempt_directory(scratch_root.path)

    with pytest.raises(RunTransitionConflict, match="agent attempt is missing"):
        _compose_with_scratch(settings, scratch_root)

    leftover.rmdir()
    scratch_root.close()


def test_releasing_fake_provider_holds_unblocks_a_held_decode_before_the_hold_bound() -> (
    None
):
    holds = harness.FakeProviderHolds()
    factory = harness.HeldAgentExecutorFactory(
        "e2e-v3-held", "held/v1", "e2e-held-process", b'"V3 provider bytes"', holds
    )
    executor = factory.open()
    executor.requests.append(SimpleNamespace(run_id=SimpleNamespace(value="held-run")))
    finished = threading.Event()

    def run() -> None:
        executor.decode_process_completion(
            SimpleNamespace(),
            harness.AgentProcessCompletion(0, b'"V3 provider bytes"', b""),
        )
        finished.set()

    thread = threading.Thread(target=run)
    thread.start()
    assert executor.holding.wait(harness.TIMEOUT_SECONDS)
    holds.release_all()
    thread.join(timeout=harness.TIMEOUT_SECONDS)
    assert not thread.is_alive()
    assert finished.is_set()


def test_a_reset_recompose_opens_the_next_runtime_on_a_fresh_scratch_root(
    tmp_path: Path,
) -> None:
    """#747 (c): each served generation owns its scratch root. A reset
    recompose must not reopen DBOS against leftover attempt directories of the
    generation it just closed.
    """
    database = tmp_path / "atelier.sqlite"
    effects = tmp_path / "effects.sqlite"
    application_version = "e2e-scratch-rotate"
    harness.seed_boot_baseline(database, effects, application_version)
    settings = _served_settings(tmp_path, database, effects, application_version)
    holds = harness.FakeProviderHolds()
    blocking = harness.BlockingAgentExecutorFactory(
        "e2e", "blocking/v1", "e2e-blocking-process", b"reset-scratch-fixture"
    )
    v2 = harness.RecordingAgentExecutorFactoryV2(
        "e2e-v3", "immediate/v1", "e2e-immediate-process", b'"V3 provider bytes"'
    )
    scratch = {"root": harness.BrowserScratchRoot.create()}
    first_root = scratch["root"].path

    def build_runtime(
        runtime_settings: object,
        effect_factory: object,
        agent_factory: object,
        agent_factories_v2: tuple,
    ) -> object:
        return harness.DbosRuntime(
            harness.replace(runtime_settings, agent_scratch_root=scratch["root"].path),
            effect_factory,
            agent_factory,
            (*agent_factories_v2, blocking, v2),
        )

    def compose() -> tuple:
        with patch.object(harness.serving, "DbosRuntime", side_effect=build_runtime):
            return harness.serving.compose_application(settings)

    def compose_next_generation() -> tuple:
        holds.start_generation()
        scratch["root"] = harness.replace_closed_generation_scratch_root(
            scratch["root"]
        )
        return compose()

    def drain_inflight() -> None:
        holds.release_all()
        blocking.release_in_flight()

    app, runtime = compose()
    leftover = _leftover_attempt_directory(first_root)
    proof: Any = None
    restart_threads: list[threading.Thread] = []
    proof_holder: list[Any] = []

    def request_restart(reset: bool) -> None:
        thread = threading.Thread(
            target=proof_holder[0].recompose_after_server_stop, args=(reset,)
        )
        thread.start()
        restart_threads.append(thread)

    try:
        proof = harness.BrowserProofHarness(
            app,
            runtime,
            blocking,
            compose_next_generation,
            request_restart,
            lambda: harness.reset_to_boot_baseline(
                database, effects, application_version
            ),
            drain_inflight,
        )
        proof_holder.append(proof)

        with TestClient(proof) as client:
            restarted = client.post("/__e2e/recompose?reset=true")
            assert restarted.status_code == 202
            expected_generation = restarted.text
            restart_threads[0].join(timeout=harness.TIMEOUT_SECONDS)
            assert not restart_threads[0].is_alive()
            observed_generation = client.get("/__e2e/generation")
            assert observed_generation.text == expected_generation

        assert not leftover.exists()
        assert not first_root.exists()
        assert scratch["root"].path.is_dir()
        assert scratch["root"].path != first_root
    finally:
        drain_inflight()
        if proof is not None:
            proof.runtime.close()
        else:
            runtime.close()
        scratch["root"].close()
