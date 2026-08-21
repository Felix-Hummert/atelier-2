from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

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
