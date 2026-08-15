from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts/check_acceptance.py")
DECLARATION = Path("acceptance/94-acceptance-trace-in-ci.toml")
CONFTEST = Path("tests/conftest.py")
DOCUMENTATION = Path("docs/requirements/README.md")
COPIED_FILES = (GATE, DECLARATION)

BOUND_START = "<!-- acceptance-gate-bound:start -->"
BOUND_END = "<!-- acceptance-gate-bound:end -->"
REPORTS_DIRECTORY = Path("reports")
QUALITY_REPORT = "quality.junit.xml"
CRASH_REPORT = "crash.junit.xml"
FRONTEND_REPORT = "frontend.vitest.json"
MOVABLE_SENTENCE = "an-unproven-sentence-fails-the-gate"
UNDECLARED_SENTENCE = "a-sentence-no-story-declares"
UNPROVEN_REFUSAL = "with no test that ran and passed in this pipeline"


class Outcome(Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ReportedTest:
    name: str
    claims: str | None = None
    outcome: Outcome = Outcome.PASSED


def junit_report(reported: Iterable[ReportedTest]) -> str:
    cases = []
    for test in reported:
        body = ""
        if test.claims is not None:
            body += (
                f'<properties><property name="proves" value="{test.claims}" />'
                "</properties>"
            )
        if test.outcome is Outcome.FAILED:
            body += '<failure message="AssertionError: deliberate" />'
        elif test.outcome is Outcome.SKIPPED:
            body += '<skipped type="pytest.skip" message="deliberate" />'
        cases.append(f'<testcase name="{test.name}" time="0.001">{body}</testcase>')
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>'
    )


def vitest_report(reported: Iterable[ReportedTest]) -> str:
    return json.dumps(
        {
            "testResults": [
                {
                    "name": "/workspace/frontend/tests/lib/proof.test.ts",
                    "assertionResults": [
                        {
                            "title": test.name,
                            "fullName": test.name,
                            "status": test.outcome.value,
                        }
                        for test in reported
                    ],
                }
            ]
        }
    )


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


def a_pytest_run_proving_every_sentence(*, without: str | None = None) -> str:
    return junit_report(
        ReportedTest(
            f"test_proves_{sentence.identifier.replace('-', '_')}", sentence.identifier
        )
        for sentence in load_acceptance_script().read_declared_sentences(PROJECT_ROOT)
        if sentence.identifier != without
    )


def copied_project(tmp_path: Path, reports: Mapping[str, str] | None = None) -> Path:
    project = tmp_path / "project"
    for relative in COPIED_FILES:
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)
    written = {
        QUALITY_REPORT: a_pytest_run_proving_every_sentence(),
        CRASH_REPORT: junit_report(()),
        FRONTEND_REPORT: vitest_report(()),
        **(reports or {}),
    }
    (project / REPORTS_DIRECTORY).mkdir(parents=True)
    for file_name, content in written.items():
        (project / REPORTS_DIRECTORY / file_name).write_text(content, encoding="utf-8")
    return project


def run_gate(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--reports", str(REPORTS_DIRECTORY)],
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


def test_a_run_proving_every_declared_sentence_passes_the_gate(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    counts = re.search(
        r"Acceptance trace: (\d+) sentences, (\d+) passing proofs, (\d+) run reports",
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
    project = copied_project(
        tmp_path,
        {QUALITY_REPORT: a_pytest_run_proving_every_sentence(without=MOVABLE_SENTENCE)},
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Acceptance gate failed:" in result.stderr
    assert f"declares {MOVABLE_SENTENCE!r} {UNPROVEN_REFUSAL}" in result.stderr


@pytest.mark.proves("an-orphaned-proof-fails-the-gate")
def test_a_claim_no_story_declares_is_named_and_fails_the_gate(tmp_path: Path) -> None:
    project = copied_project(
        tmp_path,
        {
            CRASH_REPORT: junit_report(
                (ReportedTest("test_orphaned_claim", UNDECLARED_SENTENCE),)
            )
        },
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        f"reports test_orphaned_claim proving {UNDECLARED_SENTENCE!r}, "
        "which no story declares" in result.stderr
    )


def a_pytest_run_of_the_proving_test(outcome: Outcome) -> str:
    return junit_report(
        (ReportedTest("test_proves_the_sentence", MOVABLE_SENTENCE, outcome),)
    )


@pytest.mark.proves("a-proof-no-verification-invocation-runs-fails-the-gate")
@pytest.mark.parametrize(
    "run",
    [
        {QUALITY_REPORT: junit_report(())},
        {QUALITY_REPORT: a_pytest_run_of_the_proving_test(Outcome.SKIPPED)},
        {QUALITY_REPORT: a_pytest_run_of_the_proving_test(Outcome.FAILED)},
        {
            QUALITY_REPORT: junit_report(()),
            FRONTEND_REPORT: vitest_report((ReportedTest("shows the failed stream"),)),
        },
    ],
    ids=[
        "collected-but-never-executed",
        "skipped-in-the-run",
        "failed-in-the-run",
        "claimed-only-where-the-cockpit-run-never-reported-it",
    ],
)
def test_a_proof_the_run_reports_did_not_pass_is_named_and_fails_the_gate(
    tmp_path: Path, run: Mapping[str, str]
) -> None:
    result = run_gate(copied_project(tmp_path, run))

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"declares {MOVABLE_SENTENCE!r} {UNPROVEN_REFUSAL}" in result.stderr


@pytest.mark.proves("the-gate-states-the-bound-of-what-it-proves")
def test_the_gate_and_the_documentation_state_the_same_bound(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    documentation = (PROJECT_ROOT / DOCUMENTATION).read_text(encoding="utf-8")
    documented_bound = documentation.split(BOUND_START, 1)[1].split(BOUND_END, 1)[0]

    stated_bound = load_acceptance_script().render_honesty_bound()

    assert documented_bound.strip() == stated_bound.strip()
    assert stated_bound.strip() in result.stdout


@pytest.mark.parametrize(
    "run",
    [
        {
            QUALITY_REPORT: a_pytest_run_proving_every_sentence(
                without=MOVABLE_SENTENCE
            ),
            CRASH_REPORT: junit_report(
                (ReportedTest("test_recovers_after_a_crash", MOVABLE_SENTENCE),)
            ),
        },
        {
            QUALITY_REPORT: a_pytest_run_proving_every_sentence(
                without=MOVABLE_SENTENCE
            ),
            FRONTEND_REPORT: vitest_report(
                (ReportedTest(f"proves({MOVABLE_SENTENCE}): the title carries it"),)
            ),
        },
    ],
    ids=["only-the-crash-run", "only-the-cockpit-run"],
)
def test_a_sentence_any_required_report_proves_counts(
    tmp_path: Path, run: Mapping[str, str]
) -> None:
    result = run_gate(copied_project(tmp_path, run))

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
    with (project / DECLARATION).open("a", encoding="utf-8") as declaration:
        declaration.write(
            f'\n[[sentence]]\nid = "{MOVABLE_SENTENCE}"\ntext = "the same wish again"\n'
        )


def lose_the_cockpit_report(project: Path) -> None:
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).unlink()


def truncate_the_quality_report(project: Path) -> None:
    (project / REPORTS_DIRECTORY / QUALITY_REPORT).write_text(
        "<testsuites>", encoding="utf-8"
    )


def truncate_the_cockpit_report(project: Path) -> None:
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text("{}", encoding="utf-8")


@pytest.mark.parametrize(
    ("distrusted", "refusal"),
    [
        (empty_the_declarations, "declares no acceptance sentence"),
        (declare_an_unknown_key, "declares unknown keys ['owner']"),
        (declare_a_future_schema_version, "this gate reads version 1"),
        (declare_a_sentence_twice, "is declared twice"),
        (lose_the_cockpit_report, f"the run report {FRONTEND_REPORT} is absent"),
        (
            truncate_the_quality_report,
            f"{QUALITY_REPORT} is not readable as a run report",
        ),
        (
            truncate_the_cockpit_report,
            f"{FRONTEND_REPORT} is not readable as a run report",
        ),
    ],
    ids=[
        "no-declaration",
        "unknown-declaration-key",
        "unread-schema-version",
        "duplicate-sentence",
        "missing-run-report",
        "unreadable-pytest-report",
        "unreadable-vitest-report",
    ],
)
def test_a_declaration_or_run_report_the_gate_cannot_read_is_refused(
    tmp_path: Path, distrusted: Callable[[Path], None], refusal: str
) -> None:
    project = copied_project(tmp_path)
    distrusted(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Acceptance gate refused:" in result.stderr
    assert refusal in result.stderr


def test_the_proves_marker_reaches_the_run_report_under_parallel_execution(
    tmp_path: Path,
) -> None:
    project = tmp_path / "run"
    (project / CONFTEST).parent.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / CONFTEST, project / CONFTEST)
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'addopts = "--strict-config --strict-markers"\n'
        'filterwarnings = ["error"]\n'
        'markers = ["proves(sentence_id): name the sentence this test proves"]\n',
        encoding="utf-8",
    )
    (project / "tests/test_claim.py").write_text(
        "import pytest\n\n\n"
        f'@pytest.mark.proves("{MOVABLE_SENTENCE}")\n'
        "def test_claims_its_sentence() -> None:\n    pass\n",
        encoding="utf-8",
    )
    report = project / QUALITY_REPORT

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "auto",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            "tests",
        ],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    proofs = tuple(load_acceptance_script().read_junit_proofs(report))
    assert [(proof.sentence_identifier, proof.proving_test) for proof in proofs] == [
        (MOVABLE_SENTENCE, "test_claims_its_sentence")
    ]
