from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts/check_acceptance.py")
WORKFLOW = Path(".github/workflows/ci.yml")
VITEST_CONFIGURATION = Path("frontend/vite.config.ts")
DECLARATION = Path("acceptance/94-acceptance-trace-in-ci.toml")
PROOFS = Path("tests/tooling/test_acceptance_gate.py")
DOCUMENTATION = Path("docs/requirements/README.md")
COPIED_FILES = (GATE, WORKFLOW, VITEST_CONFIGURATION, DECLARATION, PROOFS)

BOUND_START = "<!-- acceptance-gate-bound:start -->"
BOUND_END = "<!-- acceptance-gate-bound:end -->"
QUALITY_PYTEST = "pytest -n auto --ignore=tests/crash tests -q"
CRASH_PROOF = Path("tests/crash/test_acceptance_crash_proof.py")
VITEST_PROOF = Path("frontend/tests/lib/acceptance_proof.test.ts")
PLAYWRIGHT_PROOF = Path("frontend/tests/e2e/acceptance_proof.spec.ts")
MOVABLE_SENTENCE = "an-unproven-sentence-fails-the-gate"
UNDECLARED_SENTENCE = "a-sentence-no-story-declares"
UNRUN_SENTENCE = "a-proof-no-verification-invocation-runs-fails-the-gate"


def copied_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for relative in COPIED_FILES:
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)
    return project


def run_gate(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE)],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )


def rewrite(project: Path, relative: Path, before: str, after: str) -> None:
    edited = project / relative
    text = edited.read_text(encoding="utf-8")
    assert before in text, f"{relative} no longer contains {before!r}"
    edited.write_text(text.replace(before, after), encoding="utf-8")


def write(project: Path, relative: Path, text: str) -> None:
    written = project / relative
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(text, encoding="utf-8")


def append(project: Path, relative: Path, text: str) -> None:
    with (project / relative).open("a", encoding="utf-8") as target:
        target.write(text)


def marker_line(identifier: str) -> str:
    return f'@pytest.mark.proves("{identifier}")\n'


def python_proof(identifier: str, test_name: str) -> str:
    return f"\n\n{marker_line(identifier)}def {test_name}() -> None:\n    pass\n"


def vitest_proof(identifier: str) -> str:
    return f'it("proves({identifier}): the sentence travels in the run output", () => {{}});\n'


def drop_marker(project: Path, identifier: str) -> None:
    rewrite(project, PROOFS, marker_line(identifier), "")


def load_acceptance_script() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "check_acceptance", PROJECT_ROOT / GATE
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_the_repository_passes_its_own_acceptance_gate() -> None:
    result = run_gate(PROJECT_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    counts = re.search(
        r"Acceptance trace: (\d+) sentences, (\d+) proofs, "
        r"(\d+) verification invocations",
        result.stdout,
    )
    assert counts is not None
    assert all(count > 0 for count in map(int, counts.groups()))


@pytest.mark.proves("acceptance-sentences-are-declared-in-the-repository")
def test_every_declared_sentence_is_readable_from_a_versioned_repository_file() -> None:
    script = load_acceptance_script()

    sentences = script.read_declared_sentences(PROJECT_ROOT)

    declared = {sentence.identifier: sentence for sentence in sentences}
    assert MOVABLE_SENTENCE in declared
    assert declared[MOVABLE_SENTENCE].text.endswith("names that sentence.")
    assert all(
        (PROJECT_ROOT / sentence.declared_in).is_file()
        and sentence.story.startswith("https://github.com/")
        and sentence.text.strip()
        for sentence in sentences
    )


@pytest.mark.proves("an-unproven-sentence-fails-the-gate")
def test_a_sentence_no_test_claims_is_named_and_fails_the_gate(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    drop_marker(project, MOVABLE_SENTENCE)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Acceptance gate failed:" in result.stderr
    assert f"declares {MOVABLE_SENTENCE!r} with no test this pipeline runs" in (
        result.stderr
    )


@pytest.mark.proves("an-orphaned-proof-fails-the-gate")
def test_a_claim_no_story_declares_is_named_and_fails_the_gate(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    append(project, PROOFS, python_proof(UNDECLARED_SENTENCE, "test_orphaned_claim"))

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        f"test_orphaned_claim claims the sentence {UNDECLARED_SENTENCE!r}, "
        "which no story declares" in result.stderr
    )


def ignore_the_proving_directory(project: Path) -> None:
    rewrite(
        project,
        WORKFLOW,
        QUALITY_PYTEST,
        QUALITY_PYTEST.replace("tests -q", "--ignore=tests/tooling tests -q"),
    )


def prove_only_from_a_suite_vitest_does_not_run(project: Path) -> None:
    write(project, PLAYWRIGHT_PROOF, vitest_proof(UNRUN_SENTENCE))


@pytest.mark.proves("a-proof-no-verification-invocation-runs-fails-the-gate")
@pytest.mark.parametrize(
    "unrun",
    [ignore_the_proving_directory, prove_only_from_a_suite_vitest_does_not_run],
    ids=["ignored-by-the-unit-job", "outside-the-vitest-include"],
)
def test_a_proof_no_invocation_runs_is_named_and_fails_the_gate(
    tmp_path: Path, unrun: Callable[[Path], None]
) -> None:
    project = copied_project(tmp_path)
    unrun(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "but no verification invocation runs it" in result.stderr


@pytest.mark.proves("the-gate-states-the-bound-of-what-it-proves")
def test_the_gate_and_the_documentation_state_the_same_bound(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    documentation = (PROJECT_ROOT / DOCUMENTATION).read_text(encoding="utf-8")
    documented_bound = documentation.split(BOUND_START, 1)[1].split(BOUND_END, 1)[0]

    stated_bound = load_acceptance_script().render_honesty_bound()

    assert documented_bound.strip() == stated_bound.strip()
    assert stated_bound.strip() in result.stdout


def test_a_proof_only_the_crash_job_runs_still_counts(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    drop_marker(project, MOVABLE_SENTENCE)
    write(
        project,
        CRASH_PROOF,
        "import pytest\n"
        + python_proof(MOVABLE_SENTENCE, "test_recovers_after_a_crash").lstrip("\n"),
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_vitest_title_carrying_the_sentence_counts_as_its_proof(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    drop_marker(project, MOVABLE_SENTENCE)
    write(project, VITEST_PROOF, vitest_proof(MOVABLE_SENTENCE))

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def empty_the_declarations(project: Path) -> None:
    (project / DECLARATION).unlink()


def declare_an_unknown_key(project: Path) -> None:
    rewrite(
        project, DECLARATION, "schema_version = 1\n", 'schema_version = 1\nowner = ""\n'
    )


def declare_a_future_schema_version(project: Path) -> None:
    rewrite(project, DECLARATION, "schema_version = 1", "schema_version = 2")


def declare_a_sentence_twice(project: Path) -> None:
    append(
        project,
        DECLARATION,
        f'\n[[sentence]]\nid = "{MOVABLE_SENTENCE}"\ntext = "the same wish again"\n',
    )


def claim_without_naming_a_sentence(project: Path) -> None:
    append(
        project,
        PROOFS,
        "\n\n@pytest.mark.proves\ndef test_claims_nothing() -> None:\n    pass\n",
    )


def claim_from_something_pytest_never_collects(project: Path) -> None:
    append(project, PROOFS, python_proof(MOVABLE_SENTENCE, "helper_that_is_no_test"))


def narrow_the_pytest_selection(project: Path) -> None:
    rewrite(project, WORKFLOW, QUALITY_PYTEST, f"{QUALITY_PYTEST} -k acceptance")


def remove_every_pytest_invocation(project: Path) -> None:
    rewrite(project, WORKFLOW, "run --locked pytest", "run --locked echo")


def claim_outside_a_vitest_title(project: Path) -> None:
    write(project, VITEST_PROOF, f"// proves({MOVABLE_SENTENCE})\n")


@pytest.mark.parametrize(
    ("distrusted", "refusal"),
    [
        (empty_the_declarations, "declares no acceptance sentence"),
        (declare_an_unknown_key, "declares unknown keys ['owner']"),
        (declare_a_future_schema_version, "this gate reads version 1"),
        (declare_a_sentence_twice, "is declared twice"),
        (claim_without_naming_a_sentence, "without naming a sentence"),
        (
            claim_from_something_pytest_never_collects,
            "pytest collects no such test",
        ),
        (narrow_the_pytest_selection, "narrows its pytest selection with -k"),
        (remove_every_pytest_invocation, "runs no pytest invocation"),
        (claim_outside_a_vitest_title, "claims a sentence outside a test title"),
    ],
    ids=[
        "no-declaration",
        "unknown-declaration-key",
        "unread-schema-version",
        "duplicate-sentence",
        "marker-without-sentence",
        "marker-pytest-never-collects",
        "filtered-pytest-selection",
        "pipeline-without-pytest",
        "typescript-claim-outside-a-title",
    ],
)
def test_a_declaration_or_pipeline_the_gate_cannot_read_is_refused(
    tmp_path: Path, distrusted: Callable[[Path], None], refusal: str
) -> None:
    project = copied_project(tmp_path)
    distrusted(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Acceptance gate refused:" in result.stderr
    assert refusal in result.stderr
