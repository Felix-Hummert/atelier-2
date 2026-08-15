from __future__ import annotations

import ast
import fnmatch
import re
import shlex
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ACCEPTANCE_DIRECTORY = Path("acceptance")
DECLARATION_SUFFIX = ".toml"
SUPPORTED_SCHEMA_VERSION = 1
DECLARATION_KEYS = frozenset({"schema_version", "story", "sentence"})
SENTENCE_KEYS = frozenset({"id", "text"})
SENTENCE_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

WORKFLOW_PATH = Path(".github/workflows/ci.yml")
PYTEST_COMMAND = "pytest"
PYTEST_IGNORE_FLAG = "--ignore"
PYTEST_SELECTION_FLAGS = frozenset({"-k", "-m", "--deselect"})
PYTEST_VALUE_FLAGS = frozenset({"-c", "-n", "-o", "-p", "--numprocesses", "--rootdir"})
PYTEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
PYTEST_FUNCTION_PREFIX = "test_"
PYTEST_CLASS_PREFIX = "Test"

NPM_COMMAND = "npm"
NPM_TEST_SCRIPT = "test"
VITEST_CONFIGURATION = Path("vite.config.ts")
VITEST_INCLUDE_ARRAY = re.compile(r"include:\s*\[(?P<patterns>[^\]]*)\]")
VITEST_PATTERN_SEPARATOR = "/**/"

PROOF_MARKER = "proves"
PYTHON_MARKER_NAMESPACE = "mark"
TYPESCRIPT_SUFFIX = ".ts"
TYPESCRIPT_TEST_CALL = re.compile(
    r"""\b(?:it|test)\(\s*(?P<quote>["'`])(?P<title>[^"'`]*)(?P=quote)"""
)
TYPESCRIPT_PROOF_CLAIM = re.compile(rf"\b{PROOF_MARKER}\((?P<identifier>[^)]*)\)")

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


@dataclass(frozen=True, slots=True)
class AcceptanceSentence:
    identifier: str
    text: str
    story: str
    declared_in: Path


@dataclass(frozen=True, slots=True)
class ProofClaim:
    sentence_identifier: str
    proving_test: str
    located_in: Path


@dataclass(frozen=True, slots=True)
class PytestInvocation:
    name: str
    targets: tuple[Path, ...]
    ignored: tuple[Path, ...]

    def runs(self, proof: Path) -> bool:
        selected = any(
            target == proof or target in proof.parents for target in self.targets
        )
        excluded = any(
            ignored == proof or ignored in proof.parents for ignored in self.ignored
        )
        return selected and not excluded


@dataclass(frozen=True, slots=True)
class VitestInvocation:
    name: str
    directory: Path
    file_pattern: str

    def runs(self, proof: Path) -> bool:
        return self.directory in proof.parents and fnmatch.fnmatch(
            proof.name, self.file_pattern
        )


VerificationInvocation = PytestInvocation | VitestInvocation


@dataclass(frozen=True, slots=True)
class AcceptanceTrace:
    sentences: tuple[AcceptanceSentence, ...]
    claims: tuple[ProofClaim, ...]
    invocations: tuple[VerificationInvocation, ...]
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
        document = tomllib.loads(declaration.read_text(encoding="utf-8"))
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


def workflow_commands(project_root: Path) -> Iterator[tuple[str, Path, str]]:
    document: Any = yaml.safe_load(
        (project_root / WORKFLOW_PATH).read_text(encoding="utf-8")
    )
    for job_name, job in document["jobs"].items():
        job_directory = job.get("defaults", {}).get("run", {}).get("working-directory")
        for step in job.get("steps", ()):
            command = step.get("run")
            if command is None:
                continue
            directory = Path(str(step.get("working-directory", job_directory or ".")))
            name = str(step.get("name", job_name))
            for line in str(command).replace("\\\n", " ").splitlines():
                yield name, directory, line


def command_tokens(name: str, line: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(line, comments=True))
    except ValueError as error:
        raise AcceptanceGateError(
            f"the step {name!r} runs a command line this gate cannot read: {error}"
        ) from error


def read_verification_invocations(
    project_root: Path,
) -> tuple[VerificationInvocation, ...]:
    invocations: list[VerificationInvocation] = []
    for name, directory, line in workflow_commands(project_root):
        tokens = command_tokens(name, line)
        arguments = pytest_arguments(tokens)
        if arguments is not None:
            invocations.append(read_pytest_invocation(name, directory, arguments))
        elif npm_script(tokens) == NPM_TEST_SCRIPT:
            invocations.extend(read_vitest_invocations(project_root, name, directory))
    if not any(isinstance(invocation, PytestInvocation) for invocation in invocations):
        raise AcceptanceGateError(f"{WORKFLOW_PATH} runs no pytest invocation")
    return tuple(invocations)


def pytest_arguments(tokens: tuple[str, ...]) -> tuple[str, ...] | None:
    for index, token in enumerate(tokens):
        if Path(token).name == PYTEST_COMMAND:
            return tokens[index + 1 :]
    return None


def read_pytest_invocation(
    name: str, directory: Path, arguments: tuple[str, ...]
) -> PytestInvocation:
    targets: list[Path] = []
    ignored: list[Path] = []
    awaited_flag: str | None = None
    for token in arguments:
        if awaited_flag is not None:
            if awaited_flag == PYTEST_IGNORE_FLAG:
                ignored.append(directory / token)
            awaited_flag = None
            continue
        if not token.startswith("-"):
            targets.append(directory / token)
            continue
        flag, _, inline_value = token.partition("=")
        if flag in PYTEST_SELECTION_FLAGS:
            raise AcceptanceGateError(
                f"the step {name!r} narrows its pytest selection with {flag}; "
                "the gate cannot prove which test that leaves running"
            )
        if flag == PYTEST_IGNORE_FLAG:
            if inline_value:
                ignored.append(directory / inline_value)
            else:
                awaited_flag = flag
            continue
        if not inline_value and flag in PYTEST_VALUE_FLAGS:
            awaited_flag = flag
    return PytestInvocation(name, tuple(targets) or (directory,), tuple(ignored))


def npm_script(tokens: tuple[str, ...]) -> str | None:
    if not tokens or Path(tokens[0]).name != NPM_COMMAND:
        return None
    arguments = tokens[1:]
    if arguments[:1] == ("run",):
        arguments = arguments[1:]
    return arguments[0] if arguments else None


def read_vitest_invocations(
    project_root: Path, name: str, directory: Path
) -> tuple[VitestInvocation, ...]:
    configuration = project_root / directory / VITEST_CONFIGURATION
    if not configuration.is_file():
        raise AcceptanceGateError(
            f"the step {name!r} runs vitest but {directory / VITEST_CONFIGURATION} is absent"
        )
    includes = VITEST_INCLUDE_ARRAY.findall(configuration.read_text(encoding="utf-8"))
    if len(includes) != 1:
        raise AcceptanceGateError(
            f"{directory / VITEST_CONFIGURATION} declares {len(includes)} include lists; "
            "the gate reads exactly one"
        )
    patterns = re.findall(r"""["'`]([^"'`]+)["'`]""", str(includes[0]))
    if not patterns:
        raise AcceptanceGateError(
            f"{directory / VITEST_CONFIGURATION} includes no test pattern"
        )
    invocations: list[VitestInvocation] = []
    for pattern in patterns:
        head, separator, file_pattern = pattern.partition(VITEST_PATTERN_SEPARATOR)
        if not separator or "*" in head:
            raise AcceptanceGateError(
                f"{directory / VITEST_CONFIGURATION} includes {pattern!r}; the gate reads "
                f"a pattern shaped '<directory>{VITEST_PATTERN_SEPARATOR}<file glob>'"
            )
        invocations.append(
            VitestInvocation(f"{name} ({pattern})", directory / head, file_pattern)
        )
    return tuple(invocations)


def python_proof_files(
    project_root: Path, invocations: tuple[VerificationInvocation, ...]
) -> tuple[Path, ...]:
    files: set[Path] = set()
    for invocation in invocations:
        if not isinstance(invocation, PytestInvocation):
            continue
        for target in invocation.targets:
            located = project_root / target
            if located.is_dir():
                files.update(
                    found.relative_to(project_root) for found in located.rglob("*.py")
                )
            elif located.is_file() and located.suffix == ".py":
                files.add(target)
    return tuple(sorted(files))


def read_python_proof_claims(
    project_root: Path, invocations: tuple[VerificationInvocation, ...]
) -> tuple[ProofClaim, ...]:
    claims: list[ProofClaim] = []
    for location in python_proof_files(project_root, invocations):
        tree = ast.parse(
            (project_root / location).read_text(encoding="utf-8"),
            filename=str(location),
        )
        for node in ast.walk(tree):
            if not isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ):
                continue
            for decorator in node.decorator_list:
                identifier = python_marker_identifier(decorator, node.name, location)
                if identifier is None:
                    continue
                require_pytest_collects(node, location)
                claims.append(ProofClaim(identifier, node.name, location))
    return tuple(claims)


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
    if not isinstance(decorator, ast.Call):
        raise AcceptanceGateError(
            f"{location}:{test_name} carries the {PROOF_MARKER} marker without naming a sentence"
        )
    if len(decorator.args) != 1:
        raise AcceptanceGateError(
            f"{location}:{test_name} names no single sentence in its {PROOF_MARKER} marker"
        )
    argument = decorator.args[0]
    if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
        raise AcceptanceGateError(
            f"{location}:{test_name} names its sentence with something other than a literal"
        )
    if SENTENCE_IDENTIFIER.match(argument.value) is None:
        raise AcceptanceGateError(
            f"{location}:{test_name} names the sentence {argument.value!r}; "
            "identifiers are lowercase words joined by single hyphens"
        )
    return argument.value


def require_pytest_collects(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, location: Path
) -> None:
    collected_file = any(
        fnmatch.fnmatch(location.name, pattern) for pattern in PYTEST_FILE_PATTERNS
    )
    prefix = (
        PYTEST_CLASS_PREFIX
        if isinstance(node, ast.ClassDef)
        else PYTEST_FUNCTION_PREFIX
    )
    if not collected_file or not node.name.startswith(prefix):
        raise AcceptanceGateError(
            f"{location}:{node.name} claims a sentence but pytest collects no such test"
        )


def read_typescript_proof_claims(
    project_root: Path, invocations: tuple[VerificationInvocation, ...]
) -> tuple[ProofClaim, ...]:
    directories = {
        invocation.directory
        for invocation in invocations
        if isinstance(invocation, VitestInvocation)
    }
    claims: list[ProofClaim] = []
    for directory in sorted(directories):
        for found in sorted((project_root / directory).rglob(f"*{TYPESCRIPT_SUFFIX}")):
            location = found.relative_to(project_root)
            source = found.read_text(encoding="utf-8")
            titled = 0
            for call in TYPESCRIPT_TEST_CALL.finditer(source):
                title = call.group("title")
                for claim in TYPESCRIPT_PROOF_CLAIM.finditer(title):
                    titled += 1
                    identifier = claim.group("identifier")
                    if SENTENCE_IDENTIFIER.match(identifier) is None:
                        raise AcceptanceGateError(
                            f"{location} names the sentence {identifier!r}; "
                            "identifiers are lowercase words joined by single hyphens"
                        )
                    claims.append(ProofClaim(identifier, title, location))
            if titled != len(TYPESCRIPT_PROOF_CLAIM.findall(source)):
                raise AcceptanceGateError(
                    f"{location} claims a sentence outside a test title, where no run reports it"
                )
    return tuple(claims)


def acceptance_problems(
    sentences: tuple[AcceptanceSentence, ...],
    claims: tuple[ProofClaim, ...],
    invocations: tuple[VerificationInvocation, ...],
) -> tuple[str, ...]:
    problems: list[str] = []
    declared = {sentence.identifier: sentence for sentence in sentences}
    proven: set[str] = set()
    for claim in claims:
        if claim.sentence_identifier not in declared:
            problems.append(
                f"{claim.located_in}:{claim.proving_test} claims the sentence "
                f"{claim.sentence_identifier!r}, which no story declares"
            )
            continue
        if not any(invocation.runs(claim.located_in) for invocation in invocations):
            problems.append(
                f"{claim.located_in}:{claim.proving_test} proves "
                f"{claim.sentence_identifier!r}, but no verification invocation runs it"
            )
            continue
        proven.add(claim.sentence_identifier)
    for sentence in sentences:
        if sentence.identifier not in proven:
            problems.append(
                f"{sentence.declared_in} declares {sentence.identifier!r} with no test "
                f"this pipeline runs: {sentence.text}"
            )
    return tuple(problems)


def trace_acceptance(project_root: Path) -> AcceptanceTrace:
    sentences = read_declared_sentences(project_root)
    invocations = read_verification_invocations(project_root)
    claims = read_python_proof_claims(
        project_root, invocations
    ) + read_typescript_proof_claims(project_root, invocations)
    return AcceptanceTrace(
        sentences,
        claims,
        invocations,
        acceptance_problems(sentences, claims, invocations),
    )


def render_honesty_bound() -> str:
    return "\n".join(HONESTY_BOUND)


def main() -> int:
    try:
        trace = trace_acceptance(Path.cwd())
    except (
        AcceptanceGateError,
        FileNotFoundError,
        KeyError,
        TypeError,
        ValueError,
        yaml.YAMLError,
    ) as error:
        print(f"Acceptance gate refused: {error}", file=sys.stderr)
        return 1
    if trace.problems:
        print("Acceptance gate failed:", file=sys.stderr)
        for problem in trace.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"Acceptance trace: {len(trace.sentences)} sentences, {len(trace.claims)} proofs, "
        f"{len(trace.invocations)} verification invocations",
        flush=True,
    )
    print(render_honesty_bound(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
