"""Real git repositories to pin, read and unpack in tests.

A pin is a fact about a repository, so these scenarios build one rather than
imitating it: what a test asserts about a pinned tree is asserted against git
itself. Every repository is created under the test's own temporary directory and
reads no configuration of the machine it runs on -- the operator's global git
configuration could otherwise sign commits, run hooks or rename branches, and a
test that inherits it is a test that fails on somebody else's laptop.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from atelier2.adapters.project_source import LocalGitProjectSource
from atelier2.adapters.project_verification import PROJECT_MANIFEST_NAME
from atelier2.contracts.project_sources import GitObjectFormat, ProjectSourcePin

COMMITTING_SCENARIO = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "scenario",
    "GIT_AUTHOR_EMAIL": "scenario@invalid",
    "GIT_COMMITTER_NAME": "scenario",
    "GIT_COMMITTER_EMAIL": "scenario@invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}
"""Who commits and when, so a scenario's pins depend on its content alone."""


def git_project(
    root: Path,
    files: Mapping[str, str],
    object_format: GitObjectFormat = GitObjectFormat.SHA1,
) -> ProjectSourcePin:
    """A repository holding exactly these files at one commit, and the pin for it.

    The format is named because a repository's own decides how long every object
    name in it is, and a scenario that only ever builds SHA-1 repositories can
    say nothing about a project that chose the other one.
    """

    root.mkdir(parents=True, exist_ok=True)
    _git(
        root,
        "init",
        "--quiet",
        "--initial-branch=main",
        f"--object-format={object_format.value}",
    )
    return commit_to_project(root, files)


def commit_to_project(root: Path, files: Mapping[str, str]) -> ProjectSourcePin:
    """Write these files into the checkout, commit them, and pin what results."""

    write_into_checkout(root, files)
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "--message", "scenario")
    return LocalGitProjectSource(root).head()


def declared_in_checkout(root: Path, settings: Mapping[str, str]) -> None:
    """Write these settings into this repository's own `.git/config`.

    A project's local configuration is neither the machine's nor the product's:
    it travels with the checkout, and a scenario needs it to say what happens
    when a project declares something the product must not act on.
    """

    for name, value in settings.items():
        _git(root, "config", name, value)


def write_into_checkout(root: Path, files: Mapping[str, str]) -> None:
    """Change the working copy alone, leaving every commit as it stands."""

    for name, body in files.items():
        written = root / name
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text(body, encoding="utf-8")


def declaring_verification(
    command: list[str], timeout_seconds: float = 30
) -> Mapping[str, str]:
    """The one file a project needs to declare the command that verifies it."""

    return {
        PROJECT_MANIFEST_NAME: (
            "[tool.atelier2.verification]\n"
            f"command = {json.dumps(command)}\n"
            f"timeout_seconds = {timeout_seconds}\n"
        )
    }


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        env={**os.environ, **COMMITTING_SCENARIO},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
    )
