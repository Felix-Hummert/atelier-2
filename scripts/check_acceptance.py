from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

ACCEPTANCE_DIRECTORY = Path("acceptance")
DECLARATION_SUFFIX = ".toml"
SUPPORTED_SCHEMA_VERSION = 1
DECLARATION_KEYS = frozenset({"schema_version", "story", "sentence"})
SENTENCE_KEYS = frozenset({"id", "text"})
SENTENCE_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

PROOF_MARKER = "proves"
TYPESCRIPT_PROOF_CLAIM = re.compile(rf"\b{PROOF_MARKER}\((?P<identifier>[^)]*)\)")

JUNIT_TESTCASE = "testcase"
JUNIT_PROPERTY = "properties/property"
JUNIT_UNPASSED_OUTCOMES = ("failure", "error", "skipped")
VITEST_PASSED_STATUS = "passed"

HONESTY_BOUND = (
    "```text",
    "proves: every declared sentence is claimed by a test this pipeline runs",
    "proves: every claim names a sentence some story declared",
    "does not prove: that a test carries its sentence in meaning - review judges that",
    "does not measure: any ratio, case count, or coverage target",
    "```",
)


class AcceptanceGateError(Exception):
    pass


class ReportFormat(Enum):
    PYTEST_JUNIT = "pytest --junitxml"
    VITEST_JSON = "vitest --reporter=json"


@dataclass(frozen=True, slots=True)
class RequiredReport:
    file_name: str
    format: ReportFormat
    written_by: str


REQUIRED_REPORTS = (
    RequiredReport(
        "quality.junit.xml", ReportFormat.PYTEST_JUNIT, "Static and behavior"
    ),
    RequiredReport("crash.junit.xml", ReportFormat.PYTEST_JUNIT, "Crash recovery"),
    RequiredReport(
        "frontend.vitest.json", ReportFormat.VITEST_JSON, "Cockpit static and behavior"
    ),
)


@dataclass(frozen=True, slots=True)
class AcceptanceSentence:
    identifier: str
    text: str
    story: str
    declared_in: Path


@dataclass(frozen=True, slots=True)
class ReportedProof:
    sentence_identifier: str
    proving_test: str
    reported_in: str


@dataclass(frozen=True, slots=True)
class AcceptanceTrace:
    sentences: tuple[AcceptanceSentence, ...]
    proofs: tuple[ReportedProof, ...]
    problems: tuple[str, ...]


def read_declared_sentences(project_root: Path) -> tuple[AcceptanceSentence, ...]:
    declarations = sorted(
        (project_root / ACCEPTANCE_DIRECTORY).glob(f"*{DECLARATION_SUFFIX}")
    )
    if not declarations:
        raise AcceptanceGateError(
            f"{ACCEPTANCE_DIRECTORY} declares no acceptance sentence"
        )
    declared: dict[str, AcceptanceSentence] = {}
    for declaration in declarations:
        location = declaration.relative_to(project_root)
        document = read_declaration_document(declaration, location)
        unknown_keys = sorted(set(document) - DECLARATION_KEYS)
        if unknown_keys:
            raise AcceptanceGateError(
                f"{location} declares unknown keys {unknown_keys}"
            )
        if document.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
            raise AcceptanceGateError(
                f"{location} declares schema version {document.get('schema_version')!r}; "
                f"this gate reads version {SUPPORTED_SCHEMA_VERSION}"
            )
        story = document.get("story")
        if not isinstance(story, str) or not story.strip():
            raise AcceptanceGateError(f"{location} names no story")
        entries = document.get("sentence")
        if not isinstance(entries, list) or not entries:
            raise AcceptanceGateError(f"{location} declares no sentence")
        for entry in entries:
            sentence = read_sentence_entry(entry, story, location)
            if sentence.identifier in declared:
                raise AcceptanceGateError(
                    f"sentence {sentence.identifier!r} is declared twice, in "
                    f"{declared[sentence.identifier].declared_in} and {location}"
                )
            declared[sentence.identifier] = sentence
    return tuple(declared.values())


def read_declaration_document(declaration: Path, location: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(declaration.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise AcceptanceGateError(f"{location} is not readable: {error}") from error


def read_sentence_entry(entry: Any, story: str, location: Path) -> AcceptanceSentence:
    if not isinstance(entry, dict):
        raise AcceptanceGateError(f"{location} declares a sentence that is not a table")
    unknown_keys = sorted(set(entry) - SENTENCE_KEYS)
    if unknown_keys:
        raise AcceptanceGateError(
            f"{location} declares a sentence with unknown keys {unknown_keys}"
        )
    identifier = entry.get("id")
    if not isinstance(identifier, str) or SENTENCE_IDENTIFIER.match(identifier) is None:
        raise AcceptanceGateError(
            f"{location} declares the sentence identifier {identifier!r}; "
            "identifiers are lowercase words joined by single hyphens"
        )
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        raise AcceptanceGateError(
            f"{location} declares sentence {identifier!r} without text"
        )
    return AcceptanceSentence(identifier, text, story, location)


def read_junit_proofs(report: Path) -> Iterator[ReportedProof]:
    try:
        run = ElementTree.parse(report).getroot()
    except ElementTree.ParseError as error:
        raise AcceptanceGateError(
            f"{report.name} is not readable as a run report: {error}"
        ) from error
    for testcase in run.iter(JUNIT_TESTCASE):
        if any(
            testcase.find(outcome) is not None for outcome in JUNIT_UNPASSED_OUTCOMES
        ):
            continue
        for recorded in testcase.iterfind(JUNIT_PROPERTY):
            if recorded.get("name") == PROOF_MARKER:
                yield ReportedProof(
                    recorded.get("value", ""), testcase.get("name", ""), report.name
                )


def read_vitest_proofs(report: Path) -> Iterator[ReportedProof]:
    try:
        run = json.loads(report.read_text(encoding="utf-8"))
        reported = [
            assertion
            for file_run in run["testResults"]
            for assertion in file_run["assertionResults"]
        ]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise AcceptanceGateError(
            f"{report.name} is not readable as a run report: {error}"
        ) from error
    for assertion in reported:
        if assertion.get("status") != VITEST_PASSED_STATUS:
            continue
        title = str(assertion.get("title", ""))
        for claim in TYPESCRIPT_PROOF_CLAIM.finditer(title):
            yield ReportedProof(claim.group("identifier"), title, report.name)


REPORT_READERS: dict[ReportFormat, Callable[[Path], Iterator[ReportedProof]]] = {
    ReportFormat.PYTEST_JUNIT: read_junit_proofs,
    ReportFormat.VITEST_JSON: read_vitest_proofs,
}


def read_passing_proofs(reports_directory: Path) -> tuple[ReportedProof, ...]:
    proofs: list[ReportedProof] = []
    for required in REQUIRED_REPORTS:
        report = reports_directory / required.file_name
        if not report.is_file():
            raise AcceptanceGateError(
                f"the run report {required.file_name} is absent from "
                f"{reports_directory}; the {required.written_by!r} job writes it with "
                f"{required.format.value}"
            )
        proofs.extend(REPORT_READERS[required.format](report))
    return tuple(proofs)


def acceptance_problems(
    sentences: tuple[AcceptanceSentence, ...], proofs: tuple[ReportedProof, ...]
) -> tuple[str, ...]:
    problems: list[str] = []
    declared = {sentence.identifier for sentence in sentences}
    proven: set[str] = set()
    for proof in proofs:
        if proof.sentence_identifier not in declared:
            problems.append(
                f"{proof.reported_in} reports {proof.proving_test} proving "
                f"{proof.sentence_identifier!r}, which no story declares"
            )
            continue
        proven.add(proof.sentence_identifier)
    for sentence in sentences:
        if sentence.identifier not in proven:
            problems.append(
                f"{sentence.declared_in} declares {sentence.identifier!r} with no test "
                f"that ran and passed in this pipeline: {sentence.text}"
            )
    return tuple(problems)


def trace_acceptance(project_root: Path, reports_directory: Path) -> AcceptanceTrace:
    sentences = read_declared_sentences(project_root)
    proofs = read_passing_proofs(reports_directory)
    return AcceptanceTrace(sentences, proofs, acceptance_problems(sentences, proofs))


def render_honesty_bound() -> str:
    return "\n".join(HONESTY_BOUND)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check that every declared acceptance sentence was proven by a "
        "test this pipeline ran."
    )
    parser.add_argument(
        "--reports",
        type=Path,
        required=True,
        help="directory holding the run reports the verification jobs uploaded",
    )
    arguments = parser.parse_args()
    try:
        trace = trace_acceptance(Path.cwd(), arguments.reports)
    except AcceptanceGateError as error:
        print(f"Acceptance gate refused: {error}", file=sys.stderr)
        return 1
    if trace.problems:
        print("Acceptance gate failed:", file=sys.stderr)
        for problem in trace.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"Acceptance trace: {len(trace.sentences)} sentences, {len(trace.proofs)} "
        f"passing proofs, {len(REQUIRED_REPORTS)} run reports",
        flush=True,
    )
    print(render_honesty_bound(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
