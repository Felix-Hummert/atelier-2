from __future__ import annotations

import hashlib
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIREMENTS_DIRECTORY = Path("docs/requirements")
REGISTRY_LOCATION = REQUIREMENTS_DIRECTORY / "revisions.toml"
DOCUMENT_NAME = re.compile(r"^(?P<document>\d{4})-.+\.md$")
REQUIREMENT_HEADING = re.compile(
    r"^### (?P<identifier>REQ-[A-Z0-9]+-[0-9]{2}):(?P<sentence>.*)$"
)
SOURCE_LINE = re.compile(r"^Quelle:\s*(?:OPERATOR|DESK)\s+—\s*\S.*$")
GENESIS = "GENESIS"
ROOT_FIELDS = frozenset({"schema_version", "legacy", "revision"})
LEGACY_FIELDS = frozenset({"document", "path", "content_sha256"})
REVISION_FIELDS = frozenset(
    {*LEGACY_FIELDS, "approval_comment_id", "approval_sha256", "predecessor"}
)


class RequirementContractError(Exception):
    pass


Refusal = RequirementContractError


@dataclass(frozen=True, slots=True)
class RequirementRule:
    identifier: str
    located_in: Path


@dataclass(frozen=True, slots=True)
class RequirementShelf:
    document_count: int
    legacy_count: int
    rules: tuple[RequirementRule, ...]


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    document: str
    location: Path
    content_sha256: str
    predecessor: str
    approval_comment_id: int | None = None
    approval_sha256: str | None = None


RequirementRules = tuple[RequirementRule, ...]


def approval_bytes(document: str, digest: str) -> bytes:
    return f"APPROVE REQUIREMENT REVISION {document} sha256:{digest}".encode()


def read_requirement_shelf(project_root: Path) -> RequirementShelf:
    entries = read_requirement_registry(project_root)
    grouped: defaultdict[str, list[RegistryEntry]] = defaultdict(list)
    location_owners: dict[Path, str] = {}
    for entry in entries:
        owner = location_owners.setdefault(entry.location, entry.document)
        if owner != entry.document:
            raise Refusal(f"{entry.location} has owners {owner} and {entry.document}")
        grouped[entry.document].append(entry)
    registered = set(location_owners)
    directory = project_root.resolve() / REQUIREMENTS_DIRECTORY
    discovered = {
        path.relative_to(project_root.resolve())
        for path in directory.iterdir()
        if DOCUMENT_NAME.fullmatch(path.name)
    }
    if not discovered:
        raise Refusal("requirements directory has no numbered requirement document")
    if missing := sorted(discovered - registered):
        raise Refusal(f"requirement registry omits {', '.join(map(str, missing))}")
    if extra := sorted(registered - discovered):
        raise Refusal(f"requirement registry names absent {', '.join(map(str, extra))}")

    rules: list[RequirementRule] = []
    legacy_count = 0
    for document, group in sorted(grouped.items()):
        locations = sorted({entry.location for entry in group})
        if len(locations) != 1:
            raise Refusal(
                f"requirement {document} has lineage paths {', '.join(map(str, locations))}"
            )
        kinds = {not entry.predecessor for entry in group}
        if len(kinds) != 1:
            raise Refusal(f"requirement {document} is both legacy and revisioned")
        if not group[0].predecessor:
            if len(group) != 1:
                raise Refusal(f"requirement {document} has multiple legacy pins")
            active = group[0]
            legacy_count += 1
        else:
            active = _lineage_tip(document, tuple(group))
        rules.extend(_read_document(project_root, active))
    seen: dict[str, Path] = {}
    for rule in rules:
        if previous := seen.get(rule.identifier):
            raise Refusal(
                f"{rule.located_in} publishes {rule.identifier} again; first in {previous}"
            )
        seen[rule.identifier] = rule.located_in
    return RequirementShelf(len(grouped), legacy_count, tuple(rules))


def read_requirement_registry(project_root: Path) -> tuple[RegistryEntry, ...]:
    registry = _regular_file(project_root, REGISTRY_LOCATION)
    try:
        parsed = tomllib.loads(registry.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise Refusal(f"{REGISTRY_LOCATION} is unreadable: {error}") from error
    if unknown := sorted(set(parsed) - ROOT_FIELDS):
        raise Refusal(f"{REGISTRY_LOCATION} has unknown fields {unknown}")
    schema_version = parsed.get("schema_version")
    if schema_version != 1:
        raise Refusal(f"{REGISTRY_LOCATION} has unsupported schema {schema_version!r}")
    entries: list[RegistryEntry] = []
    for field, legacy in (("legacy", True), ("revision", False)):
        value = parsed.get(field, [])
        valid = isinstance(value, list) and all(
            isinstance(item, dict) for item in value
        )
        if not valid:
            raise Refusal(f"{REGISTRY_LOCATION} {field} must be an array of tables")
        entries.extend(_registry_entry(item, legacy) for item in value)
    return tuple(entries)


def _registry_entry(raw: dict[str, Any], legacy: bool) -> RegistryEntry:
    kind = "legacy" if legacy else "revision"
    expected = LEGACY_FIELDS if legacy else REVISION_FIELDS
    if unknown := sorted(set(raw) - expected):
        raise Refusal(f"{REGISTRY_LOCATION} {kind} entry has unknown fields {unknown}")
    if missing := sorted(expected - set(raw)):
        raise Refusal(f"{REGISTRY_LOCATION} {kind} entry lacks fields {missing}")
    document = _text(raw, "document", f"{kind} entry")
    if re.fullmatch(r"\d{4}", document) is None:
        raise Refusal(f"registry document {document!r} is not NNNN")
    location = _location(raw, document)
    content = _text(raw, "content_sha256", f"{kind} {document}", digest=True)
    if legacy:
        return RegistryEntry(document, location, content, "")
    comment = raw.get("approval_comment_id")
    if isinstance(comment, bool) or not isinstance(comment, int) or comment <= 0:
        raise Refusal(
            f"revision {document} has invalid approval_comment_id {comment!r}"
        )
    approval_digest = _text(raw, "approval_sha256", f"revision {document}", digest=True)
    approved = hashlib.sha256(approval_bytes(document, content)).hexdigest()
    if approval_digest != approved:
        raise Refusal(
            f"revision {document} {content} has approval digest "
            f"{approval_digest}; expected {approved} for the exact approval line"
        )
    predecessor = _text(raw, "predecessor", f"revision {document}")
    return RegistryEntry(
        document, location, content, predecessor, comment, approval_digest
    )


def _text(raw: dict[str, Any], field: str, owner: str, *, digest: bool = False) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise Refusal(f"{owner} has invalid {field} {value!r}")
    value = value.strip()
    if digest and re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise Refusal(f"{owner} has invalid {field} {value!r}")
    return value


def _location(raw: dict[str, Any], document: str) -> Path:
    value = _text(raw, "path", f"requirement {document}")
    location = Path(value)
    if (
        location.is_absolute()
        or ".." in location.parts
        or location.parent != REQUIREMENTS_DIRECTORY
        or DOCUMENT_NAME.fullmatch(location.name) is None
        or not location.name.startswith(f"{document}-")
    ):
        raise Refusal(f"requirement {document} has invalid registry path {value!r}")
    return Path(*location.parts)


def _regular_file(project_root: Path, location: Path) -> Path:
    root = project_root.resolve()
    directory = root / REQUIREMENTS_DIRECTORY
    target = root / location
    if directory.is_symlink() or not directory.is_dir():
        raise Refusal(f"{REQUIREMENTS_DIRECTORY} is not a real directory")
    if target.is_symlink() or not target.is_file() or target.parent != directory:
        raise Refusal(f"{location} is not a regular non-symlink file under {directory}")
    return target


def _lineage_tip(document: str, lineage: tuple[RegistryEntry, ...]) -> RegistryEntry:
    by_digest = {revision.content_sha256: revision for revision in lineage}
    if len(by_digest) != len(lineage):
        raise Refusal(f"requirement {document} repeats a revision")
    for revision in lineage:
        predecessor = revision.predecessor
        if predecessor == revision.content_sha256:
            raise Refusal(
                f"requirement {document} revision {revision.content_sha256} references itself"
            )
        if predecessor != GENESIS and predecessor not in by_digest:
            raise Refusal(
                f"requirement {document} revision {revision.content_sha256} has unknown "
                f"predecessor {predecessor}"
            )
    successors = Counter(
        revision.predecessor for revision in lineage if revision.predecessor != GENESIS
    )
    if branch := next((key for key, count in successors.items() if count > 1), None):
        raise Refusal(f"requirement {document} branches after {branch}")
    tips = set(by_digest) - set(successors)
    if len(tips) != 1:
        raise Refusal(f"requirement {document} has multiple tips {sorted(tips)}")
    tip = by_digest[tips.pop()]
    visited: set[str] = set()
    cursor = tip
    while cursor.predecessor != GENESIS:
        visited.add(cursor.content_sha256)
        cursor = by_digest[cursor.predecessor]
    visited.add(cursor.content_sha256)
    if len(visited) != len(lineage):
        raise Refusal(
            f"requirement {document} has multiple tips outside its active line"
        )
    return tip


def _read_document(project_root: Path, active: RegistryEntry) -> RequirementRules:
    try:
        content = _regular_file(project_root, active.location).read_bytes()
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Refusal(f"{active.location} is not UTF-8") from error
    actual = hashlib.sha256(content).hexdigest()
    if actual != active.content_sha256:
        if not active.predecessor:
            raise Refusal(
                f"{active.location} has legacy content digest {actual}, expected "
                f"{active.content_sha256}; migrate it instead of replacing the legacy pin"
            )
        raise Refusal(
            f"{active.location} has current file digest {actual}, but its sole "
            f"registry tip is {active.content_sha256}"
        )
    parser = _parse_legacy_rules if not active.predecessor else _parse_strict_document
    return parser(text, active.location)


def _parse_legacy_rules(text: str, location: Path) -> RequirementRules:
    lines = text.splitlines()
    headings = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := REQUIREMENT_HEADING.fullmatch(line)) is not None
    ]
    rules: list[RequirementRule] = []
    for position, (index, heading) in enumerate(headings):
        if not heading["sentence"].strip():
            raise Refusal(f"{location} {heading['identifier']} has empty sentence")
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        source = next(
            (line for line in lines[index + 1 : end] if line.startswith("Quelle:")),
            "",
        )
        if SOURCE_LINE.fullmatch(source) is None:
            raise Refusal(
                f"{location} publishes {heading['identifier']} without a valid Quelle"
            )
        rules.append(RequirementRule(heading["identifier"], location))
    if not rules:
        raise Refusal(f"{location} publishes no requirement rule")
    return tuple(rules)


def _parse_strict_document(text: str, location: Path) -> RequirementRules:
    lines = text.splitlines()
    if not lines or re.fullmatch(r"# \S.*", lines[0]) is None:
        raise Refusal(f"{location} has no nonempty title")
    sections = [
        (line[3:].strip(), index)
        for index, line in enumerate(lines)
        if line.startswith("## ")
    ]
    names = tuple(name for name, _ in sections)
    allowed = (("Intent", "Rules"), ("Intent", "Rules", "Non-goals"))
    if names not in allowed:
        unknown = next(
            (name for name in names if name not in {"Intent", "Rules", "Non-goals"}),
            None,
        )
        detail = f"unknown section {unknown!r}" if unknown else f"sections {names}"
        raise Refusal(f"{location} has {detail}")
    if any(line.strip() for line in lines[1 : sections[0][1]]):
        raise Refusal(f"{location} has content before Intent")
    ranges = {
        name: lines[
            start + 1 : sections[index + 1][1] if index + 1 < len(sections) else None
        ]
        for index, (name, start) in enumerate(sections)
    }
    if not any(line.strip() for line in ranges["Intent"]):
        raise Refusal(f"{location} has empty Intent")
    if any(line.startswith("#") for line in ranges["Intent"]):
        raise Refusal(f"{location} has a heading inside Intent")
    non_goals = ranges.get("Non-goals")
    if non_goals is not None and not any(line.strip() for line in non_goals):
        raise Refusal(f"{location} has empty Non-goals")
    if non_goals is not None and any(line.startswith("#") for line in non_goals):
        raise Refusal(f"{location} has a heading inside Non-goals")
    return _parse_strict_rules(ranges["Rules"], location)


def _parse_strict_rules(lines: list[str], location: Path) -> RequirementRules:
    rules: list[RequirementRule] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        heading = REQUIREMENT_HEADING.fullmatch(lines[index])
        if heading is None:
            raise Refusal(f"{location} has unknown rule field {lines[index]!r}")
        if not heading["sentence"].strip():
            raise Refusal(f"{location} {heading['identifier']} has empty sentence")
        index += 1
        body: list[str] = []
        while (
            index < len(lines) and REQUIREMENT_HEADING.fullmatch(lines[index]) is None
        ):
            if lines[index].strip():
                body.append(lines[index])
            index += 1
        unknown = next((line for line in body if not line.startswith("Quelle:")), None)
        if unknown is not None:
            raise Refusal(f"{location} has unknown rule field {unknown!r}")
        source = SOURCE_LINE.fullmatch(body[0]) if len(body) == 1 else None
        if source is None:
            raise Refusal(
                f"{location} publishes {heading['identifier']} without exactly one "
                "Quelle grade OPERATOR or DESK and a source pointer"
            )
        rules.append(RequirementRule(heading["identifier"], location))
    if not rules:
        raise Refusal(f"{location} has no requirement rule")
    return tuple(rules)
