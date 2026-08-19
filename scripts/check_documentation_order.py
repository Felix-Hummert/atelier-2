from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REQUIREMENTS_DIRECTORY = Path("docs/requirements")
REQUIREMENT_DOCUMENT_NAME = re.compile(r"^\d{4}-.+\.md$")
REQUIREMENT_BLOCK_HEADING = re.compile(r"^### (REQ-[A-Z0-9]+-[0-9]{2}):\s*(.*)$")
HEADER_FENCE_OPEN = "```text"
HEADER_FIELDS = (
    "Status",
    "Owner-Issue",
    "Source-Threads",
    "Distilled-From",
    "Approved-By",
)
HEADER_FIELD_LINE = re.compile(r"^([A-Za-z-]+):\s*(.*)$")
QUELLE_FIELD = "Quelle"
TEMPLATE_FIELD_LINE = re.compile(
    r"^(Status|Quelle|Begründung|Journeys|Beweis|Offen):\s*(.*)$"
)
AUTHORITY_GRADES = frozenset({"OPERATOR", "DESK"})
VALID_DOCUMENT_STATUSES = frozenset({"DRAFT", "AGREED", "SUPERSEDED"})
SOURCE_SEPARATORS = "—–-:"
DISTILLED_FROM = "Distilled-From"

# Freshness against the live thread would need a GitHub token in CI and would
# flake with the tracker. Presence of Distilled-From is the in-repo invariant;
# currency, document-level Open questions, and the generating workflow stay
# named successors of this gate.
HONESTY_BOUND = (
    "```text",
    "proves: every numbered requirement document carries the header fields, and Distilled-From is not empty",
    "proves: every requirement rule's Quelle opens with OPERATOR or DESK and a source pointer",
    "does not prove: that Distilled-From is current against its live thread - no GitHub call",
    "does not prove: that the cited object still says what the document quotes - review judges that",
    "does not measure: document-level Open questions, journey files, or index completeness",
    "does not generate: a requirement document from new thread objects - that is half B",
    "```",
)


class DocumentationOrderError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RequirementRule:
    identifier: str
    quelle: str
    located_in: Path


@dataclass(frozen=True, slots=True)
class RequirementDocument:
    location: Path
    header: dict[str, str]
    header_order: tuple[str, ...]
    rules: tuple[RequirementRule, ...]


def read_requirement_documents(project_root: Path) -> tuple[RequirementDocument, ...]:
    directory = project_root / REQUIREMENTS_DIRECTORY
    if not directory.is_dir():
        raise DocumentationOrderError(f"{REQUIREMENTS_DIRECTORY} is not a directory")
    documents = tuple(
        read_requirement_document(path, path.relative_to(project_root))
        for path in sorted(directory.iterdir())
        if REQUIREMENT_DOCUMENT_NAME.match(path.name)
    )
    if not documents:
        raise DocumentationOrderError(
            f"{REQUIREMENTS_DIRECTORY} contains no numbered requirement document"
        )
    return documents


def read_requirement_document(path: Path, location: Path) -> RequirementDocument:
    text = path.read_text(encoding="utf-8")
    header, header_order = parse_header(text)
    return RequirementDocument(
        location, header, header_order, parse_requirement_rules(text, location)
    )


def parse_header(text: str) -> tuple[dict[str, str], tuple[str, ...]]:
    fence = first_text_fence(text)
    if fence is None:
        return {}, ()
    collected: dict[str, list[str]] = {}
    order: list[str] = []
    current: str | None = None
    for line in fence.splitlines():
        field = HEADER_FIELD_LINE.match(line)
        if field is not None:
            name = field.group(1)
            current = name
            collected[name] = [field.group(2) or ""]
            if name in HEADER_FIELDS and name not in order:
                order.append(name)
            continue
        if current is not None:
            collected[current].append(line)
    return (
        {name: "\n".join(values).strip() for name, values in collected.items()},
        tuple(order),
    )


def first_text_fence(text: str) -> str | None:
    start = text.find(HEADER_FENCE_OPEN)
    if start < 0:
        return None
    content_start = start + len(HEADER_FENCE_OPEN)
    if content_start < len(text) and text[content_start] == "\n":
        content_start += 1
    end = text.find("```", content_start)
    if end < 0:
        return None
    return text[content_start:end]


def parse_requirement_rules(text: str, location: Path) -> tuple[RequirementRule, ...]:
    lines = text.splitlines()
    rules: list[RequirementRule] = []
    index = 0
    while index < len(lines):
        heading = REQUIREMENT_BLOCK_HEADING.match(lines[index])
        if heading is None:
            index += 1
            continue
        index += 1
        body_start = index
        while index < len(lines) and not lines[index].startswith("##"):
            index += 1
        rules.append(
            parse_requirement_block(heading.group(1), lines[body_start:index], location)
        )
    return tuple(rules)


def parse_requirement_block(
    identifier: str, body: list[str], location: Path
) -> RequirementRule:
    collected: list[str] = []
    current = False
    for line in body:
        field = TEMPLATE_FIELD_LINE.match(line)
        if field is not None:
            name = field.group(1)
            if name == QUELLE_FIELD:
                current = True
                collected = [field.group(2) or ""]
                continue
            if current:
                break
            continue
        if current:
            collected.append(line)
    return RequirementRule(identifier, "\n".join(collected).strip(), location)


def split_quelle(quelle: str) -> tuple[str, str]:
    stripped = quelle.strip()
    if not stripped:
        return "", ""
    grade, _, remainder = stripped.partition(" ")
    return grade, remainder.lstrip().lstrip(SOURCE_SEPARATORS).strip()


def documentation_order_problems(
    documents: tuple[RequirementDocument, ...],
) -> tuple[str, ...]:
    problems: list[str] = []
    for document in documents:
        problems.extend(header_problems(document))
        for rule in document.rules:
            problems.extend(quelle_problems(rule))
    return tuple(problems)


def header_problems(document: RequirementDocument) -> tuple[str, ...]:
    if not document.header_order and not document.header:
        return (f"{document.location} has no header fence",)
    problems: list[str] = []
    for name in HEADER_FIELDS:
        if name not in document.header:
            problems.append(f"{document.location} has no {name} header field")
            continue
        if not document.header[name]:
            problems.append(f"{document.location} has an empty {name} header field")
    present = tuple(name for name in document.header_order if name in HEADER_FIELDS)
    expected = tuple(name for name in HEADER_FIELDS if name in document.header)
    if present != expected:
        problems.append(
            f"{document.location} has header fields {present} out of order; "
            f"expected {expected}"
        )
    status = first_token(document.header.get("Status", ""))
    if "Status" in document.header and status not in VALID_DOCUMENT_STATUSES:
        problems.append(
            f"{document.location} has status {status!r}; status is DRAFT, "
            "AGREED, or SUPERSEDED"
        )
    return tuple(problems)


def first_token(text: str) -> str:
    stripped = text.strip()
    return stripped.split()[0] if stripped else ""


def quelle_problems(rule: RequirementRule) -> tuple[str, ...]:
    if not rule.quelle:
        return (
            f"{rule.located_in} publishes {rule.identifier} without {QUELLE_FIELD}",
        )
    grade, source = split_quelle(rule.quelle)
    if grade not in AUTHORITY_GRADES:
        return (
            (
                f"{rule.located_in} publishes {rule.identifier} with {QUELLE_FIELD} "
                f"grade {grade!r}; grade is OPERATOR or DESK"
            ),
        )
    if not source:
        return (
            (
                f"{rule.located_in} publishes {rule.identifier} with {QUELLE_FIELD} "
                "that names no source pointer"
            ),
        )
    return ()


def render_honesty_bound() -> str:
    return "\n".join(HONESTY_BOUND)


def render_summary(documents: tuple[RequirementDocument, ...]) -> str:
    rules = sum(len(document.rules) for document in documents)
    return (
        f"Documentation order: {len(documents)} document(s), {rules} rule(s), "
        f"{DISTILLED_FROM} present, {QUELLE_FIELD} carries grade and source"
    )


def main() -> int:
    try:
        documents = read_requirement_documents(Path.cwd())
    except DocumentationOrderError as error:
        print(f"Documentation-order gate refused: {error}", file=sys.stderr)
        return 1
    problems = documentation_order_problems(documents)
    if problems:
        print("Documentation-order gate failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(render_summary(documents), flush=True)
    print(render_honesty_bound(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
