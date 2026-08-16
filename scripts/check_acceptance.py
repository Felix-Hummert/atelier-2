from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import tomllib
from collections import Counter
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
PYTHON_MARKER_NAMESPACE = "mark"
TYPESCRIPT_PROOF_CLAIM = re.compile(rf"\b{PROOF_MARKER}\((?P<identifier>[^)]*)\)")
VITEST_TITLE_PARAMETER = re.compile(r"%(?:s|d|i|f|j|o|O|c|#|\$)")
TYPESCRIPT_SIMPLE_ESCAPES = {
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}
PYTHON_SUFFIX = ".py"
TYPESCRIPT_SUFFIX = ".ts"
FRONTEND_DIRECTORY = "frontend"
CLAIMABLE_SUFFIXES = (PYTHON_SUFFIX, TYPESCRIPT_SUFFIX)
UNSCANNED_DIRECTORIES = frozenset(
    {".git", ".venv", "__pycache__", "node_modules", "dist"}
)

JUNIT_TESTCASE = "testcase"
JUNIT_PROPERTY = "properties/property"
JUNIT_UNPASSED_OUTCOMES = ("failure", "error", "skipped")
VITEST_PASSED_STATUS = "passed"

LANDING_FIELD = re.compile(
    r"^[ \t]*[-*]?[ \t]*Literal acceptance sentence\(s\)[^:]*:[ \t]*(?P<stated>.*?)[ \t]*$",
    re.MULTILINE,
)
LANDING_EXEMPTION = re.compile(r"^none\b[ \t]*:?[ \t]*(?P<reason>.*)$", re.IGNORECASE)
LANDING_SEPARATOR = re.compile(r"[,\s]+")
LANDING_UNCHECKED = (
    "Landing binding: not checked, this run carries no proposed pull request"
)

HONESTY_BOUND = (
    "```text",
    "proves: every declared sentence was proven by a test that ran and passed here",
    "proves: every claim in this repository names a sentence some story declared",
    "proves: every claim was honoured by a passing test in this pipeline's reports",
    "proves: a proposed landing states its sentences by identifier, or why it has none",
    "does not prove: that a test carries its sentence in meaning - review judges that",
    "does not prove: that a stated exemption is honest - review judges that",
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
    located_in: Path | None
    python_class_name: str | None
    reported_in: str


@dataclass(frozen=True, slots=True)
class ProofClaim:
    sentence_identifier: str
    claiming_test: str
    located_in: Path


@dataclass(frozen=True, slots=True)
class LandingBinding:
    """What the pull request proposing this landing states about its own sentences.

    At most one side is filled: a landing either names sentences or claims a
    reasoned exemption. Neither filled is a landing that stated nothing, whether
    it emptied the template field, wrote a bare ``none``, or carries no such
    field at all.
    """

    named_sentences: tuple[str, ...]
    exemption_reason: str | None


@dataclass(frozen=True, slots=True)
class AcceptanceTrace:
    sentences: tuple[AcceptanceSentence, ...]
    claims: tuple[ProofClaim, ...]
    proofs: tuple[ReportedProof, ...]
    landing: LandingBinding | None
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
                    recorded.get("value", ""),
                    testcase.get("name", ""),
                    None,
                    testcase.get("classname"),
                    report.name,
                )


def read_vitest_proofs(report: Path) -> Iterator[ReportedProof]:
    try:
        run = json.loads(report.read_text(encoding="utf-8"))
        reported = [
            (file_run.get("name"), assertion)
            for file_run in run["testResults"]
            for assertion in file_run["assertionResults"]
        ]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise AcceptanceGateError(
            f"{report.name} is not readable as a run report: {error}"
        ) from error
    for file_name, assertion in reported:
        if assertion.get("status") != VITEST_PASSED_STATUS:
            continue
        title = str(assertion.get("title", ""))
        for claim in TYPESCRIPT_PROOF_CLAIM.finditer(title):
            yield ReportedProof(
                claim.group("identifier"),
                title,
                typescript_test_location(file_name),
                None,
                report.name,
            )


def typescript_test_location(file_name: Any) -> Path | None:
    if not isinstance(file_name, str) or not file_name:
        return None
    parts = Path(file_name.replace("\\", "/")).parts
    if FRONTEND_DIRECTORY in parts:
        frontend_index = max(
            index for index, part in enumerate(parts) if part == FRONTEND_DIRECTORY
        )
        return Path(*parts[frontend_index:])
    if parts and parts[0] == "tests":
        return Path(FRONTEND_DIRECTORY, *parts)
    return None


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


def read_source_claims(project_root: Path) -> tuple[ProofClaim, ...]:
    """Find every claim the repository makes, bound to no runner and no invocation.

    A claim a runner never collects has to be reachable here, or it stays
    invisible instead of being named as a missing test or a line to delete.
    """
    claims: list[ProofClaim] = []
    for directory, subdirectories, file_names in os.walk(project_root):
        subdirectories[:] = [
            name for name in subdirectories if name not in UNSCANNED_DIRECTORIES
        ]
        for file_name in sorted(file_names):
            found = Path(directory, file_name)
            if found.suffix not in CLAIMABLE_SUFFIXES:
                continue
            source = found.read_text(encoding="utf-8")
            if PROOF_MARKER not in source:
                continue
            location = found.relative_to(project_root)
            reader = (
                read_python_claims
                if found.suffix == PYTHON_SUFFIX
                else read_typescript_claims
            )
            claims.extend(reader(source, location))
    return tuple(claims)


def read_python_claims(source: str, location: Path) -> Iterator[ProofClaim]:
    try:
        tree = ast.parse(source, filename=str(location))
    except SyntaxError as error:
        raise AcceptanceGateError(f"{location} is not readable: {error}") from error
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            identifier = python_marker_identifier(decorator, node.name, location)
            if identifier is not None:
                yield ProofClaim(identifier, node.name, location)


def python_marker_identifier(
    decorator: ast.expr, test_name: str, location: Path
) -> str | None:
    marked = decorator.func if isinstance(decorator, ast.Call) else decorator
    if not isinstance(marked, ast.Attribute) or marked.attr != PROOF_MARKER:
        return None
    namespace = marked.value
    if (
        not isinstance(namespace, ast.Attribute)
        or namespace.attr != PYTHON_MARKER_NAMESPACE
    ):
        return None
    named = (
        decorator.args[0]
        if isinstance(decorator, ast.Call) and len(decorator.args) == 1
        else None
    )
    if not isinstance(named, ast.Constant) or not isinstance(named.value, str):
        raise AcceptanceGateError(
            f"{location}:{test_name} carries the {PROOF_MARKER} marker "
            "without naming one sentence in a literal"
        )
    return named.value


def read_typescript_claims(source: str, location: Path) -> Iterator[ProofClaim]:
    for title in typescript_string_literals(source):
        for claim in TYPESCRIPT_PROOF_CLAIM.finditer(title):
            yield ProofClaim(claim.group("identifier"), title, location)


def typescript_string_literals(source: str) -> Iterator[str]:
    """Read static string contents without mistaking a closing quote for an opener."""

    index = 0
    while index < len(source):
        if source.startswith("//", index):
            line_end = source.find("\n", index + 2)
            index = len(source) if line_end == -1 else line_end + 1
            continue
        if source.startswith("/*", index):
            comment_end = source.find("*/", index + 2)
            index = len(source) if comment_end == -1 else comment_end + 2
            continue
        quote = source[index]
        if quote not in {'"', "'", "`"}:
            index += 1
            continue
        index += 1
        contents: list[str] = []
        while index < len(source) and source[index] != quote:
            if source[index] == "\\" and index + 1 < len(source):
                decoded, index = decode_typescript_escape(source, index)
                contents.append(decoded)
                continue
            contents.append(source[index])
            index += 1
        if index < len(source):
            yield "".join(contents)
            index += 1


def decode_typescript_escape(source: str, backslash: int) -> tuple[str, int]:
    escaped = source[backslash + 1]
    if escaped == "\n":
        return "", backslash + 2
    if escaped == "\r":
        after_line = backslash + 2
        if after_line < len(source) and source[after_line] == "\n":
            after_line += 1
        return "", after_line
    if escaped == "x" and re.fullmatch(
        r"[0-9A-Fa-f]{2}", source[backslash + 2 : backslash + 4]
    ):
        return chr(int(source[backslash + 2 : backslash + 4], 16)), backslash + 4
    if escaped == "u":
        unicode_escape = re.match(
            r"(?:[0-9A-Fa-f]{4}|\{[0-9A-Fa-f]+\})", source[backslash + 2 :]
        )
        if unicode_escape is not None:
            digits = unicode_escape.group(0).strip("{}")
            return chr(int(digits, 16)), backslash + 2 + len(unicode_escape.group(0))
    return TYPESCRIPT_SIMPLE_ESCAPES.get(escaped, escaped), backslash + 2


def proof_honours_claim(proof: ReportedProof, claim: ProofClaim) -> bool:
    if proof.sentence_identifier != claim.sentence_identifier:
        return False
    if claim.located_in.suffix == PYTHON_SUFFIX:
        if not python_class_name_matches_source(
            proof.python_class_name, claim.located_in
        ):
            return False
        return proof.proving_test == claim.claiming_test or any(
            proof.proving_test.startswith(f"{claim.claiming_test}{suffix}")
            for suffix in ("[", "@")
        )
    if proof.located_in != claim.located_in:
        return False
    return (
        re.fullmatch(vitest_title_pattern(claim.claiming_test), proof.proving_test)
        is not None
    )


def vitest_title_pattern(title: str) -> str:
    pattern: list[str] = []
    previous_end = 0
    for parameter in VITEST_TITLE_PARAMETER.finditer(title):
        pattern.append(re.escape(title[previous_end : parameter.start()]))
        pattern.append(r"\d+" if parameter.group(0) in {"%#", "%$"} else r".*?")
        previous_end = parameter.end()
    pattern.append(re.escape(title[previous_end:]))
    return "".join(pattern)


def python_class_name_matches_source(class_name: str | None, source: Path) -> bool:
    if class_name is None:
        return False
    source_module = ".".join(source.with_suffix("").parts)
    return class_name == source_module or class_name.startswith(f"{source_module}.")


def read_landing_binding(body: Path) -> LandingBinding:
    """Read what the pull request states, from the body the run's own event carries.

    Nothing here consults the issue tracker or decides for itself whether a
    change is a story: the landing states its position and the gate reads it.
    """

    try:
        proposed = body.read_text(encoding="utf-8")
    except OSError as error:
        raise AcceptanceGateError(
            f"the pull request body at {body} is not readable: {error}"
        ) from error
    field = LANDING_FIELD.search(proposed)
    if field is None:
        return LandingBinding((), None)
    stated = field.group("stated")
    exempted = LANDING_EXEMPTION.match(stated)
    if exempted is not None:
        return LandingBinding((), exempted.group("reason").strip() or None)
    return LandingBinding(
        tuple(
            stripped
            for token in LANDING_SEPARATOR.split(stated)
            if (stripped := token.strip("`"))
        ),
        None,
    )


def landing_problems(
    landing: LandingBinding, sentences: tuple[AcceptanceSentence, ...]
) -> tuple[str, ...]:
    if landing.exemption_reason is not None:
        return ()
    if not landing.named_sentences:
        return (
            (
                "the pull request states no acceptance sentence and claims no "
                "exemption: answer the template's acceptance field with the "
                f"identifiers in {ACCEPTANCE_DIRECTORY} this landing proves, or "
                "with 'none' and one line saying why this change declares none"
            ),
        )
    declared = {sentence.identifier for sentence in sentences}
    return tuple(
        f"the pull request names {named!r} as an acceptance sentence of this "
        "landing, which no story declares"
        for named in landing.named_sentences
        if named not in declared
    )


def acceptance_problems(
    sentences: tuple[AcceptanceSentence, ...],
    claims: tuple[ProofClaim, ...],
    proofs: tuple[ReportedProof, ...],
) -> tuple[str, ...]:
    problems: list[str] = []
    declared = {sentence.identifier for sentence in sentences}
    claim_counts = Counter(
        (claim.located_in, claim.claiming_test, claim.sentence_identifier)
        for claim in claims
    )
    for (located_in, claiming_test, sentence_identifier), count in sorted(
        claim_counts.items()
    ):
        if count > 1:
            problems.append(
                f"{located_in}:{claiming_test} declares the same proof identity "
                f"more than once ({count} claims for {sentence_identifier!r}); each "
                "source claim must name one distinct test"
            )
    proven: set[str] = set()
    for proof in proofs:
        if proof.sentence_identifier not in declared:
            problems.append(
                f"{proof.reported_in} reports {proof.proving_test} proving "
                f"{proof.sentence_identifier!r}, which no story declares"
            )
            continue
        proven.add(proof.sentence_identifier)
    unhonoured_claims = set(claims_without_distinct_proofs(claims, proofs))
    for claim in claims:
        unhonoured = (
            "which no story declares"
            if claim.sentence_identifier not in declared
            else "which no run report shows passing"
        )
        if claim in unhonoured_claims:
            problems.append(
                f"{claim.located_in}:{claim.claiming_test} claims "
                f"{claim.sentence_identifier!r}, {unhonoured}"
            )
    for sentence in sentences:
        if sentence.identifier not in proven:
            problems.append(
                f"{sentence.declared_in} declares {sentence.identifier!r} with no test "
                f"that ran and passed in this pipeline: {sentence.text}"
            )
    return tuple(problems)


def claims_without_distinct_proofs(
    claims: tuple[ProofClaim, ...], proofs: tuple[ReportedProof, ...]
) -> tuple[ProofClaim, ...]:
    reserved_proofs: set[int] = set()
    honoured_claims: set[int] = set()
    parameterized_claims: list[int] = []
    for claim_index, claim in enumerate(claims):
        if (
            claim.located_in.suffix == TYPESCRIPT_SUFFIX
            and VITEST_TITLE_PARAMETER.search(claim.claiming_test) is not None
        ):
            parameterized_claims.append(claim_index)
            continue
        proof_index = next(
            (
                index
                for index, proof in enumerate(proofs)
                if index not in reserved_proofs and proof_honours_claim(proof, claim)
            ),
            None,
        )
        if proof_index is not None:
            reserved_proofs.add(proof_index)
            honoured_claims.add(claim_index)

    parameterized_claim_by_proof: dict[int, int] = {}

    def assign_distinct_proof(claim_index: int, visited: set[int]) -> bool:
        for proof_index, proof in enumerate(proofs):
            if proof_index in reserved_proofs or proof_index in visited:
                continue
            if not proof_honours_claim(proof, claims[claim_index]):
                continue
            visited.add(proof_index)
            previous_claim = parameterized_claim_by_proof.get(proof_index)
            if previous_claim is None or assign_distinct_proof(previous_claim, visited):
                parameterized_claim_by_proof[proof_index] = claim_index
                return True
        return False

    for claim_index in parameterized_claims:
        if assign_distinct_proof(claim_index, set()):
            honoured_claims.add(claim_index)
    return tuple(
        claim for index, claim in enumerate(claims) if index not in honoured_claims
    )


def trace_acceptance(
    project_root: Path, reports_directory: Path, pull_request_body: Path | None
) -> AcceptanceTrace:
    sentences = read_declared_sentences(project_root)
    claims = read_source_claims(project_root)
    proofs = read_passing_proofs(reports_directory)
    landing = (
        None if pull_request_body is None else read_landing_binding(pull_request_body)
    )
    problems = acceptance_problems(sentences, claims, proofs) + (
        () if landing is None else landing_problems(landing, sentences)
    )
    return AcceptanceTrace(sentences, claims, proofs, landing, problems)


def render_landing(landing: LandingBinding | None) -> str:
    if landing is None:
        return LANDING_UNCHECKED
    if landing.exemption_reason is not None:
        return f"Landing binding: exempt, {landing.exemption_reason}"
    return (
        f"Landing binding: {len(landing.named_sentences)} sentence(s) named by "
        "the proposed landing"
    )


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
    parser.add_argument(
        "--pull-request-body",
        type=Path,
        default=None,
        help="file holding the body of the pull request proposing this landing; "
        "absent when no pull request proposes the checked commit",
    )
    arguments = parser.parse_args()
    try:
        trace = trace_acceptance(
            Path.cwd(), arguments.reports, arguments.pull_request_body
        )
    except AcceptanceGateError as error:
        print(f"Acceptance gate refused: {error}", file=sys.stderr)
        return 1
    if trace.problems:
        print("Acceptance gate failed:", file=sys.stderr)
        for problem in trace.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"Acceptance trace: {len(trace.sentences)} sentences, {len(trace.claims)} "
        f"claims, {len(trace.proofs)} passing proofs, "
        f"{len(REQUIRED_REPORTS)} run reports",
        flush=True,
    )
    print(render_landing(trace.landing), flush=True)
    print(render_honesty_bound(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
