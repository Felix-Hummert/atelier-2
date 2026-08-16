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
ACCEPTANCE = Path("acceptance")
DECLARATION = ACCEPTANCE / "94-acceptance-trace-in-ci.toml"
SECOND_STORY_DECLARATION = ACCEPTANCE / "89-a-second-story.toml"
CONFTEST = Path("tests/conftest.py")
DOCUMENTATION = Path("docs/requirements/README.md")
PROOFS = Path("tests/tooling/test_acceptance_gate.py")
COPIED_FILES = (GATE, PROOFS)

BOUND_START = "<!-- acceptance-gate-bound:start -->"
BOUND_END = "<!-- acceptance-gate-bound:end -->"
REPORTS_DIRECTORY = Path("reports")
QUALITY_REPORT = "quality.junit.xml"
CRASH_REPORT = "crash.junit.xml"
FRONTEND_REPORT = "frontend.vitest.json"
MOVABLE_SENTENCE = "an-unproven-sentence-fails-the-gate"
REPORT_ONLY_SENTENCE = "one-page-of-a-stream-is-decided-before-any-frame-is-written"
UNDECLARED_SENTENCE = "a-sentence-no-story-declares"
UNPROVEN_REFUSAL = "with no test that ran and passed in this pipeline"
UNCOLLECTED_PYTHON_CLAIM = Path("scripts/proof_helper.py")
UNCOLLECTED_TYPESCRIPT_CLAIM = Path("frontend/tools/proof.ts")
SECOND_STORY_SENTENCE = "a-sentence-a-second-story-declares"
PULL_REQUEST_BODY = Path("pull-request-body.md")
LANDING_FIELD_LINE = (
    "- Literal acceptance sentence(s), by their identifier in `acceptance/`, "
    "or `none` and why this change declares none:"
)
STATES_NEITHER = "states no acceptance sentence and claims no exemption"
SECOND_DECLARED_SENTENCE = "an-orphaned-proof-fails-the-gate"
TRACE_COUNTS = re.compile(
    r"Acceptance trace: (\d+) sentences, (\d+) claims, (\d+) passing proofs, "
    r"(\d+) run reports"
)


class Outcome(Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ReportedTest:
    name: str
    claims: str | None = None
    outcome: Outcome = Outcome.PASSED
    located_in: Path | None = None
    class_scope: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TracedCounts:
    sentences: int
    claims: int
    passing_proofs: int
    run_reports: int


def traced_counts(stdout: str) -> TracedCounts:
    counted = TRACE_COUNTS.search(stdout)
    assert counted is not None, stdout
    return TracedCounts(*(int(count) for count in counted.groups()))


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
        location = (
            ""
            if test.located_in is None
            else (
                ' classname="'
                f"{'.'.join((*test.located_in.with_suffix('').parts, *test.class_scope))}"
                '"'
            )
        )
        cases.append(
            f'<testcase{location} name="{test.name}" time="0.001">{body}</testcase>'
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>'
    )


def vitest_report(
    reported: Iterable[ReportedTest],
    located_in: Path = Path("frontend/tests/lib/proof.test.ts"),
) -> str:
    return json.dumps(
        {
            "testResults": [
                {
                    "name": f"/workspace/{located_in}",
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


def a_pytest_run_proving_every_sentence(
    project: Path, *, without: str | None = None
) -> str:
    script = load_acceptance_script()
    claims = script.read_source_claims(project)
    claims_by_sentence = {
        sentence.identifier: tuple(
            claim
            for claim in claims
            if claim.sentence_identifier == sentence.identifier
            and claim.located_in.suffix == ".py"
        )
        for sentence in script.read_declared_sentences(project)
    }
    reported: list[ReportedTest] = []
    for sentence_identifier, sentence_claims in claims_by_sentence.items():
        if sentence_identifier == without:
            continue
        reported.extend(
            ReportedTest(
                claim.claiming_test,
                sentence_identifier,
                located_in=claim.located_in,
            )
            for claim in sentence_claims
        )
        if not sentence_claims:
            reported.append(
                ReportedTest(
                    f"test_proves_{sentence_identifier.replace('-', '_')}",
                    sentence_identifier,
                )
            )
    return junit_report(reported)


def copied_project(
    tmp_path: Path,
    reports: Mapping[str, str] | None = None,
    *,
    also_declaring: Mapping[Path, str] | None = None,
    unproven: str | None = None,
) -> Path:
    project = tmp_path / "project"
    for relative in COPIED_FILES:
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)
    shutil.copytree(PROJECT_ROOT / ACCEPTANCE, project / ACCEPTANCE)
    for relative, declaration in (also_declaring or {}).items():
        (project / relative).write_text(declaration, encoding="utf-8")
    written = {
        QUALITY_REPORT: a_pytest_run_proving_every_sentence(project, without=unproven),
        CRASH_REPORT: junit_report(()),
        FRONTEND_REPORT: vitest_report(()),
        **(reports or {}),
    }
    (project / REPORTS_DIRECTORY).mkdir(parents=True)
    for file_name, content in written.items():
        (project / REPORTS_DIRECTORY / file_name).write_text(content, encoding="utf-8")
    return project


def run_gate(
    project: Path, proposed_by: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the gate over a project, optionally as the landing a pull request proposes."""

    landing: list[str] = []
    if proposed_by is not None:
        (project / PULL_REQUEST_BODY).write_text(proposed_by, encoding="utf-8")
        landing = ["--pull-request-body", str(PULL_REQUEST_BODY)]
    return subprocess.run(
        [sys.executable, str(GATE), "--reports", str(REPORTS_DIRECTORY), *landing],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )


def a_pull_request_stating(binding: str) -> str:
    return (
        "## Story binding\n\n"
        "- HumanRequirement Issue URL: https://github.com/FlexOr2/atelier-2/issues/94\n"
        f"{LANDING_FIELD_LINE} {binding}\n"
        "- Context sources consulted: the gate\n"
    )


def a_pull_request_without_the_field() -> str:
    return "## What this is\n\nProse a template field never reached.\n"


def rewrite(project: Path, relative: Path, before: str, after: str) -> None:
    edited = project / relative
    text = edited.read_text(encoding="utf-8")
    assert before in text, f"{relative} no longer contains {before!r}"
    edited.write_text(text.replace(before, after), encoding="utf-8")


def test_a_run_proving_every_declared_sentence_passes_the_gate(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    counted = traced_counts(result.stdout)
    assert counted.sentences > 0 and counted.claims > 0
    assert counted.passing_proofs > 0 and counted.run_reports > 0


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
    project = copied_project(tmp_path, unproven=MOVABLE_SENTENCE)

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


@pytest.mark.proves("a-proof-that-did-not-run-and-pass-fails-the-gate")
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


@pytest.mark.proves("the-gate-states-the-bound-of-what-it-proves")
def test_the_bound_names_that_the_body_is_read_as_the_run_received_it() -> None:
    """The landing binding is a snapshot, and the bound has to say so.

    The gate reads the pull-request body the event handed it, so a body edited
    after a green run is never re-checked. That is an accepted limit rather than
    a defect -- re-firing CI on every edit would cancel running work, and reading
    the tracker live would put back the derivation this gate exists without. What
    is not acceptable is leaving it unsaid in the one place that lists what this
    gate does not prove.
    """
    stated_bound = load_acceptance_script().render_honesty_bound()

    unproven = [line for line in stated_bound.splitlines() if "does not prove" in line]

    assert any("edited after this run" in line for line in unproven), (
        "the bound lists what the gate cannot judge, and the body snapshot is "
        f"one of those things; it names only: {unproven}"
    )


@pytest.mark.parametrize(
    "run",
    [
        {
            CRASH_REPORT: junit_report(
                (ReportedTest("test_recovers_after_a_crash", REPORT_ONLY_SENTENCE),)
            )
        },
        {
            FRONTEND_REPORT: vitest_report(
                (ReportedTest(f"proves({REPORT_ONLY_SENTENCE}): the title carries it"),)
            )
        },
    ],
    ids=["only-the-crash-run", "only-the-cockpit-run"],
)
def test_a_sentence_any_required_report_proves_counts(
    tmp_path: Path, run: Mapping[str, str]
) -> None:
    result = run_gate(copied_project(tmp_path, run, unproven=REPORT_ONLY_SENTENCE))

    assert result.returncode == 0, result.stdout + result.stderr


def a_second_story_declaration() -> str:
    return (
        "schema_version = 1\n"
        'story = "https://github.com/FlexOr2/atelier-2/issues/89"\n\n'
        "[[sentence]]\n"
        f'id = "{SECOND_STORY_SENTENCE}"\n'
        'text = "A second story declares an acceptance sentence of its own."\n'
    )


@pytest.mark.proves("a-second-story-declaration-verifies-like-the-first")
def test_a_second_story_declaration_verifies_like_the_first(tmp_path: Path) -> None:
    one_story = run_gate(copied_project(tmp_path / "one-story"))

    two_stories = run_gate(
        copied_project(
            tmp_path / "two-stories",
            also_declaring={SECOND_STORY_DECLARATION: a_second_story_declaration()},
        )
    )

    assert two_stories.returncode == 0, two_stories.stdout + two_stories.stderr
    assert (
        traced_counts(two_stories.stdout).sentences
        == traced_counts(one_story.stdout).sentences + 1
    )


@pytest.mark.proves("a-story-that-declares-no-sentence-is-named-by-verification")
def test_a_landing_that_declares_no_acceptance_sentence_is_named(
    tmp_path: Path,
) -> None:
    result = run_gate(copied_project(tmp_path), a_pull_request_stating(""))

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Acceptance gate failed:" in result.stderr
    assert STATES_NEITHER in result.stderr


@pytest.mark.parametrize(
    ("proposed_by", "problem"),
    [
        (a_pull_request_without_the_field(), STATES_NEITHER),
        (a_pull_request_stating("none"), STATES_NEITHER),
        (a_pull_request_stating("none:"), STATES_NEITHER),
        (
            a_pull_request_stating(UNDECLARED_SENTENCE),
            (
                f"names {UNDECLARED_SENTENCE!r} as an acceptance sentence of "
                "this landing, which no story declares"
            ),
        ),
        (
            a_pull_request_stating(f"`{MOVABLE_SENTENCE}`, `{UNDECLARED_SENTENCE}`"),
            (
                f"names {UNDECLARED_SENTENCE!r} as an acceptance sentence of "
                "this landing, which no story declares"
            ),
        ),
    ],
    ids=[
        "no-acceptance-field-at-all",
        "the-word-none-without-a-reason",
        "an-exemption-with-an-empty-reason",
        "an-identifier-no-story-declares",
        "one-declared-identifier-beside-an-undeclared-one",
    ],
)
def test_a_landing_whose_binding_does_not_hold_is_named(
    tmp_path: Path, proposed_by: str, problem: str
) -> None:
    result = run_gate(copied_project(tmp_path), proposed_by)

    assert result.returncode != 0, result.stdout + result.stderr
    assert problem in result.stderr


@pytest.mark.parametrize(
    "binding",
    [
        MOVABLE_SENTENCE,
        f"`{MOVABLE_SENTENCE}`, `{SECOND_DECLARED_SENTENCE}`",
        "none: documentation only",
        "None - this head moves modules and proves no new sentence",
    ],
    ids=[
        "one-declared-identifier",
        "several-declared-identifiers-in-backticks",
        "a-reasoned-exemption",
        "a-reasoned-exemption-written-in-prose",
    ],
)
def test_a_landing_that_states_its_binding_passes(tmp_path: Path, binding: str) -> None:
    result = run_gate(copied_project(tmp_path), a_pull_request_stating(binding))

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_run_no_pull_request_proposes_says_the_binding_was_not_checked(
    tmp_path: Path,
) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no proposed pull request" in result.stdout


def write_claim(project: Path, relative: Path, text: str) -> None:
    written = project / relative
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(text, encoding="utf-8")


def a_python_claim_no_runner_collects(identifier: str) -> str:
    return (
        "import pytest\n\n\n"
        f'@pytest.mark.proves("{identifier}")\n'
        "def test_helps() -> None:\n    pass\n"
    )


@pytest.mark.parametrize(
    ("claim", "located_in", "claiming", "problem"),
    [
        (
            a_python_claim_no_runner_collects(UNDECLARED_SENTENCE),
            UNCOLLECTED_PYTHON_CLAIM,
            "test_helps",
            f"claims {UNDECLARED_SENTENCE!r}, which no story declares",
        ),
        (
            a_python_claim_no_runner_collects(MOVABLE_SENTENCE),
            UNCOLLECTED_PYTHON_CLAIM,
            "test_helps",
            f"claims {MOVABLE_SENTENCE!r}, which no run report shows passing",
        ),
        (
            f'it("proves({MOVABLE_SENTENCE}): nothing runs this file", () => {{}});\n',
            UNCOLLECTED_TYPESCRIPT_CLAIM,
            f"proves({MOVABLE_SENTENCE}): nothing runs this file",
            f"claims {MOVABLE_SENTENCE!r}, which no run report shows passing",
        ),
    ],
    ids=["undeclared-sentence", "declared-sentence", "typescript-claim"],
)
def test_a_claim_no_run_report_carries_is_named_wherever_it_sits(
    tmp_path: Path, claim: str, located_in: Path, claiming: str, problem: str
) -> None:
    project = copied_project(tmp_path, unproven=MOVABLE_SENTENCE)
    write_claim(project, located_in, claim)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{located_in}:{claiming} {problem}" in result.stderr


def test_each_claiming_test_must_appear_in_a_required_report(tmp_path: Path) -> None:
    project = copied_project(tmp_path, unproven=MOVABLE_SENTENCE)
    missing_claim = Path("tests/test_missing_claim.py")
    reported_claim = Path("tests/test_reported_claim.py")
    write_claim(
        project,
        missing_claim,
        a_python_claim_no_runner_collects(MOVABLE_SENTENCE).replace(
            "test_helps", "test_missing_claim"
        ),
    )
    write_claim(
        project,
        reported_claim,
        a_python_claim_no_runner_collects(MOVABLE_SENTENCE).replace(
            "test_helps", "test_reported_claim"
        ),
    )
    (project / REPORTS_DIRECTORY / CRASH_REPORT).write_text(
        junit_report(
            (
                ReportedTest(
                    "test_reported_claim",
                    MOVABLE_SENTENCE,
                    located_in=reported_claim,
                ),
            )
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        f"{missing_claim}:test_missing_claim claims {MOVABLE_SENTENCE!r}, "
        "which no run report shows passing"
    ) in result.stderr
    assert f"{reported_claim}:test_reported_claim claims" not in result.stderr


def test_each_typescript_title_must_appear_in_a_required_report(tmp_path: Path) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    missing_title = f"proves({REPORT_ONLY_SENTENCE}): the browser-only flow"
    reported_title = f"proves({REPORT_ONLY_SENTENCE}): the unit flow"
    missing_claim = Path("frontend/tests/e2e/missing.spec.ts")
    reported_claim = Path("frontend/tests/lib/reported.test.ts")
    write_claim(project, missing_claim, f'it("{missing_title}", () => {{}});\n')
    write_claim(project, reported_claim, f'it("{reported_title}", () => {{}});\n')
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report((ReportedTest(reported_title),), located_in=reported_claim),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        f"{missing_claim}:{missing_title} claims {REPORT_ONLY_SENTENCE!r}, "
        "which no run report shows passing"
    ) in result.stderr
    assert f"{reported_claim}:{reported_title} claims" not in result.stderr


def test_same_named_python_tests_in_different_files_are_distinct_claims(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    missing_claim = Path("tests/test_missing_duplicate.py")
    reported_claim = Path("tests/test_reported_duplicate.py")
    claim = a_python_claim_no_runner_collects(REPORT_ONLY_SENTENCE).replace(
        "test_helps", "test_duplicate"
    )
    write_claim(project, missing_claim, claim)
    write_claim(project, reported_claim, claim)
    (project / REPORTS_DIRECTORY / CRASH_REPORT).write_text(
        junit_report(
            (
                ReportedTest(
                    "test_duplicate",
                    REPORT_ONLY_SENTENCE,
                    located_in=reported_claim,
                ),
            )
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{missing_claim}:test_duplicate claims" in result.stderr
    assert f"{reported_claim}:test_duplicate claims" not in result.stderr


def test_same_titled_typescript_tests_in_different_files_are_distinct_claims(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    title = f"proves({REPORT_ONLY_SENTENCE}): the duplicate title"
    missing_claim = Path("frontend/tests/e2e/missing.spec.ts")
    reported_claim = Path("frontend/tests/lib/reported.test.ts")
    claim = f'it("{title}", () => {{}});\n'
    write_claim(project, missing_claim, claim)
    write_claim(project, reported_claim, claim)
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report((ReportedTest(title),), located_in=reported_claim),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{missing_claim}:{title} claims" in result.stderr
    assert f"{reported_claim}:{title} claims" not in result.stderr


@pytest.mark.parametrize(
    ("located_in", "source"),
    [
        (
            Path("tests/test_duplicate.py"),
            (
                "import pytest\n\n\n"
                f'@pytest.mark.proves("{REPORT_ONLY_SENTENCE}")\n'
                "def test_duplicate() -> None:\n    pass\n\n\n"
                "class TestNested:\n"
                f'    @pytest.mark.proves("{REPORT_ONLY_SENTENCE}")\n'
                "    def test_duplicate(self) -> None:\n        pass\n"
            ),
        ),
        (
            Path("frontend/tests/lib/duplicate.test.ts"),
            (
                f'describe("one", () => it("proves({REPORT_ONLY_SENTENCE}): duplicate", () => {{}}));\n'
                f'describe("two", () => it("proves({REPORT_ONLY_SENTENCE}): duplicate", () => {{}}));\n'
            ),
        ),
    ],
    ids=["python-class-scope", "vitest-suite-scope"],
)
def test_a_repeated_source_test_identity_is_refused_as_ambiguous(
    tmp_path: Path, located_in: Path, source: str
) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    write_claim(project, located_in, source)
    if located_in.suffix == ".py":
        report_name = "test_duplicate"
        report = junit_report(
            (
                ReportedTest(
                    report_name,
                    REPORT_ONLY_SENTENCE,
                    located_in=located_in,
                ),
            )
        )
        report_file = CRASH_REPORT
    else:
        report_name = f"proves({REPORT_ONLY_SENTENCE}): duplicate"
        report = vitest_report((ReportedTest(report_name),), located_in=located_in)
        report_file = FRONTEND_REPORT
    (project / REPORTS_DIRECTORY / report_file).write_text(report, encoding="utf-8")

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        f"{located_in}:{report_name} declares the same proof identity more than once"
        in result.stderr
    )


@pytest.mark.parametrize(
    "reported_name",
    ["test_helps", "test_helps@direct-systemd-user-manager"],
    ids=["plain", "xdist-group"],
)
def test_a_unique_python_class_method_matches_its_junit_identity(
    tmp_path: Path, reported_name: str
) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    located_in = Path("tests/test_class_claim.py")
    write_claim(
        project,
        located_in,
        (
            "import pytest\n\n\n"
            "class TestClaim:\n"
            f'    @pytest.mark.proves("{REPORT_ONLY_SENTENCE}")\n'
            "    def test_helps(self) -> None:\n        pass\n"
        ),
    )
    (project / REPORTS_DIRECTORY / CRASH_REPORT).write_text(
        junit_report(
            (
                ReportedTest(
                    reported_name,
                    REPORT_ONLY_SENTENCE,
                    located_in=located_in,
                    class_scope=("TestClaim",),
                ),
            )
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("placeholder", "rendered"),
    [("%s", "one"), ("%s", ""), ("%c", ""), ("%#", "0"), ("%$", "1")],
    ids=[
        "value",
        "empty-value",
        "empty-css-format",
        "zero-based-index",
        "one-based-index",
    ],
)
def test_a_parameterized_typescript_title_matches_its_reported_cases(
    tmp_path: Path, placeholder: str, rendered: str
) -> None:
    project = copied_project(tmp_path)
    claim = Path("frontend/tests/lib/parameterized.test.ts")
    title = f"proves({REPORT_ONLY_SENTENCE}): {placeholder} remains named"
    write_claim(project, claim, f'it.each(["one"])("{title}", () => {{}});\n')
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report(
            (
                ReportedTest(
                    f"proves({REPORT_ONLY_SENTENCE}): {rendered} remains named"
                ),
            ),
            located_in=claim,
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_parameterized_typescript_claim_cannot_borrow_a_static_tests_proof(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    claim = Path("frontend/tests/lib/parameterized.test.ts")
    parameterized_title = f"proves({REPORT_ONLY_SENTENCE}): %s remains named"
    static_title = f"proves({REPORT_ONLY_SENTENCE}): one remains named"
    write_claim(
        project,
        claim,
        (
            f'it.each(["one"])("{parameterized_title}", () => {{}});\n'
            f'it("{static_title}", () => {{}});\n'
        ),
    )
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report((ReportedTest(static_title),), located_in=claim),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{claim}:{parameterized_title} claims" in result.stderr
    assert f"{claim}:{static_title} claims" not in result.stderr


def test_an_escaped_typescript_title_matches_its_decoded_reported_title(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    claim = Path("frontend/tests/lib/escaped.test.ts")
    reported_title = f'proves({REPORT_ONLY_SENTENCE}): "one" remains named'
    source_title = reported_title.replace('"', '\\"')
    write_claim(project, claim, f'it("{source_title}", () => {{}});\n')
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report((ReportedTest(reported_title),), located_in=claim),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_typescript_claim_is_its_whole_title_and_not_a_comment() -> None:
    script = load_acceptance_script()
    title = f"context proves({REPORT_ONLY_SENTENCE}): remains one test"
    source = (
        f'// "proves({MOVABLE_SENTENCE}): comments make no claim"\n'
        f'it("{title}", () => {{}});\n'
    )

    claims = tuple(script.read_typescript_claims(source, Path("a.test.ts")))

    assert [(claim.sentence_identifier, claim.claiming_test) for claim in claims] == [
        (REPORT_ONLY_SENTENCE, title)
    ]


def test_every_sentence_this_repository_declares_is_claimed_by_one_of_its_tests() -> (
    None
):
    script = load_acceptance_script()

    claimed = {
        claim.sentence_identifier for claim in script.read_source_claims(PROJECT_ROOT)
    }

    declared = {
        sentence.identifier for sentence in script.read_declared_sentences(PROJECT_ROOT)
    }
    assert declared <= claimed, f"unclaimed: {sorted(declared - claimed)}"
    assert claimed <= declared, f"undeclared: {sorted(claimed - declared)}"


def empty_the_declarations(project: Path) -> None:
    for declaration in (project / ACCEPTANCE).glob(f"*{DECLARATION.suffix}"):
        declaration.unlink()


def claim_without_naming_a_sentence(project: Path) -> None:
    write_claim(
        project,
        UNCOLLECTED_PYTHON_CLAIM,
        "import pytest\n\n\n@pytest.mark.proves\ndef test_helps() -> None:\n    pass\n",
    )


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
        (claim_without_naming_a_sentence, "without naming one sentence"),
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
        "marker-without-a-sentence",
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
