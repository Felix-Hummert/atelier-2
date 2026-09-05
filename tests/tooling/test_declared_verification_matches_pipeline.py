"""The declared verification is what the pipeline's own job proves green.

`pyproject.toml`'s `[tool.atelier2.verification]` is the one owner of what
verifies this tree when a node redeems the `run-project-verification` grant
(`src/atelier2/adapters/project_verification.py` reads it, byte for byte, from
the pinned commit). `tests/crash` needs a systemd scope with cgroup delegation
that a grant's workspace does not have, so it stays the pipeline's own
`crash-recovery` job (#1152) and the declared command must not bundle it back
in. This test pins both halves of that contract: the declaration excludes
`tests/crash`, and it names exactly the same pytest invocation the Python test
job in `.github/workflows/ci.yml` actually runs -- so a change to either side
that drifts from the other fails here before it fails a live grant redemption.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parents[2]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
PYTHON_TEST_JOB = "tests"
PYTHON_TEST_STEP_NAME = "Test non-crash behavior"
UV_BINARY_TOKEN = "uv"
UV_PATH_VARIABLE = "${uv_path}"
CRASH_EXCLUSION = "--ignore=tests/crash"
PYTEST_INVOCATION = re.compile(
    r'"\$\{uv_path\}"\s+run\s+--locked\s+pytest.*?--junitxml="[^"]*"',
    re.DOTALL,
)


def declared_verification_command() -> tuple[str, ...]:
    manifest = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    command = manifest["tool"]["atelier2"]["verification"]["command"]
    assert isinstance(command, list)
    return tuple(command)


def python_test_job_pytest_invocation() -> tuple[str, ...]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        step
        for step in workflow["jobs"][PYTHON_TEST_JOB]["steps"]
        if step.get("name") == PYTHON_TEST_STEP_NAME
    )
    invocation = PYTEST_INVOCATION.search(step["run"])
    assert invocation is not None, (
        f"{PYTHON_TEST_STEP_NAME!r} no longer runs a recognizable pytest invocation"
    )
    tokens = [token for token in shlex.split(invocation.group()) if token.strip()]
    pytest_tokens = [token for token in tokens if not token.startswith("--junitxml=")]
    if pytest_tokens[0] == UV_PATH_VARIABLE:
        pytest_tokens[0] = UV_BINARY_TOKEN
    return tuple(pytest_tokens)


def test_the_declared_verification_excludes_crash_recovery() -> None:
    declared = declared_verification_command()

    assert CRASH_EXCLUSION in declared
    assert "tests/crash" not in declared


def test_the_declared_verification_matches_what_the_python_test_job_runs() -> None:
    declared = declared_verification_command()

    assert declared == python_test_job_pytest_invocation()
