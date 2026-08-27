from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from requirement_contract import (
    DOCUMENT_NAME,
    REGISTRY_LOCATION,
    RegistryEntry,
    RequirementContractError,
    RequirementShelf,
    SourceBinding,
    read_requirement_registry,
    read_requirement_shelf,
    read_requirement_source_bindings,
)

EXACT_GIT_SHA = re.compile(r"[0-9a-f]{40}")

HONESTY_BOUND = (
    "```text",
    "proves: every numbered requirement is a regular in-shelf file whose exact bytes match its sole active tip or frozen legacy pin",
    "proves: with an exact VCS base, legacy pins cannot grow or change, and may only migrate in place to approval-backed history",
    "proves: with an exact VCS base, every existing revision remains field-identical and history grows only by a valid successor",
    "proves: every strict requirement has only title, nonempty Intent, nonempty unique sourced rule sentences, and optional nonempty Non-goals",
    "proves: every approval-backed revision line is predecessor-complete, unbranched, and has one tip on one numbered path",
    "proves: every source binding names one exact approval-backed requirement revision, and with an exact VCS base prior bindings stay field-identical",
    "does not prove: that a cited source or approval comment exists or says what the registry claims - review judges that",
    "does not fetch: GitHub or another live authority",
    "does not judge: source meaning or freshness",
    "does not make: a frozen legacy document an approved revision",
    "```",
)


def render_honesty_bound() -> str:
    return "\n".join(HONESTY_BOUND)


def render_summary(shelf: RequirementShelf) -> str:
    return (
        f"Requirement contract: {shelf.document_count} document(s), "
        f"{len(shelf.rules)} rule(s), {shelf.legacy_count} frozen legacy, "
        f"{shelf.document_count - shelf.legacy_count} approval-backed"
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-revision")
    return parser.parse_args()


def _git_object_exists(project_root: Path, object_name: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", object_name],
            cwd=project_root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def _git_bytes(project_root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments], cwd=project_root, check=False, capture_output=True
    )
    if result.returncode != 0:
        raise RequirementContractError(
            f"exact base revision cannot supply {' '.join(arguments)}"
        )
    return result.stdout


def _legacy_entries(entries: tuple[RegistryEntry, ...]) -> dict[str, RegistryEntry]:
    return {entry.document: entry for entry in entries if not entry.predecessor}


def _git_regular_file(project_root: Path, base_revision: str, location: Path) -> bytes:
    listing = _git_bytes(
        project_root, "ls-tree", "-z", base_revision, "--", location.as_posix()
    )
    records = [record for record in listing.split(b"\0") if record]
    try:
        metadata, raw_location = records[0].split(b"\t", 1)
        mode, kind, _object = metadata.decode().split()
        exact_location = raw_location.decode() == location.as_posix()
    except (IndexError, UnicodeDecodeError, ValueError) as error:
        raise RequirementContractError(
            f"{location} is not a regular Git file in exact base {base_revision}"
        ) from error
    if (
        len(records) != 1
        or mode not in {"100644", "100755"}
        or kind != "blob"
        or not exact_location
    ):
        raise RequirementContractError(
            f"{location} is not a regular Git file in exact base {base_revision}"
        )
    return _git_bytes(project_root, "show", f"{base_revision}:{location.as_posix()}")


def _base_snapshot(
    project_root: Path, base_revision: str
) -> tuple[tuple[RegistryEntry, ...], tuple[SourceBinding, ...]]:
    registry_object = f"{base_revision}:{REGISTRY_LOCATION.as_posix()}"
    if not _git_object_exists(project_root, registry_object):
        return _bootstrap_base_snapshot(project_root, base_revision), ()
    with tempfile.TemporaryDirectory() as temporary:
        base_root = Path(temporary)
        registry = base_root / REGISTRY_LOCATION
        registry.parent.mkdir(parents=True)
        registry.write_bytes(
            _git_regular_file(project_root, base_revision, REGISTRY_LOCATION)
        )
        entries = read_requirement_registry(base_root)
        bindings = read_requirement_source_bindings(base_root)
    for entry in entries:
        _git_regular_file(project_root, base_revision, entry.location)
    return entries, bindings


def _bootstrap_base_snapshot(
    project_root: Path, base_revision: str
) -> tuple[RegistryEntry, ...]:
    listing = _git_bytes(
        project_root,
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        base_revision,
        "--",
        str(REGISTRY_LOCATION.parent),
    )
    entries: dict[str, RegistryEntry] = {}
    for raw_location in listing.rstrip(b"\0").split(b"\0"):
        if not raw_location:
            continue
        location = Path(raw_location.decode("utf-8"))
        match = DOCUMENT_NAME.fullmatch(location.name)
        if match is None:
            continue
        document = match["document"]
        if previous := entries.get(document):
            raise RequirementContractError(
                f"base requirement {document} has two paths: "
                f"{previous.location} and {location}"
            )
        content = _git_regular_file(project_root, base_revision, location)
        entries[document] = RegistryEntry(
            document, location, hashlib.sha256(content).hexdigest(), ""
        )
    return tuple(entries.values())


def _verify_snapshot_monotonicity(project_root: Path, base_revision: str) -> None:
    if EXACT_GIT_SHA.fullmatch(base_revision) is None or not _git_object_exists(
        project_root, f"{base_revision}^{{commit}}"
    ):
        raise RequirementContractError(
            f"exact base revision {base_revision!r} is absent or unresolvable"
        )
    base_entries, base_bindings = _base_snapshot(project_root, base_revision)
    current_entries = read_requirement_registry(project_root)
    base_legacy = _legacy_entries(base_entries)
    current_legacy = _legacy_entries(current_entries)
    if added := sorted(set(current_legacy) - set(base_legacy)):
        raise RequirementContractError(
            f"new legacy requirement {added[0]} is forbidden"
        )
    for document, base in base_legacy.items():
        current = current_legacy.get(document)
        if current == base:
            continue
        if current is not None:
            raise RequirementContractError(
                f"requirement {document} at {current.location} changes legacy VCS pin "
                f"from {base.location} sha256:{base.content_sha256} to "
                f"sha256:{current.content_sha256}; "
                "migrate it to an approval-backed revision instead of re-pinning"
            )
        revisions = tuple(
            entry
            for entry in current_entries
            if entry.document == document and entry.predecessor
        )
        if not revisions:
            raise RequirementContractError(
                f"legacy requirement {document} at {base.location} was removed "
                "without an approval-backed revision"
            )
        if {entry.location for entry in revisions} != {base.location}:
            raise RequirementContractError(
                f"requirement {document} migration changes path from {base.location}"
            )
    for base in base_entries:
        if base.predecessor and base not in current_entries:
            raise RequirementContractError(
                f"revision {base.document} {base.content_sha256} changed or deleted"
            )
    current_bindings = {
        (binding.document, binding.content_sha256): binding
        for binding in read_requirement_source_bindings(project_root)
    }
    for binding in base_bindings:
        current = current_bindings.get((binding.document, binding.content_sha256))
        if current != binding:
            raise RequirementContractError(
                f"source binding {binding.document} {binding.content_sha256} "
                "changed or deleted"
            )


def main() -> int:
    arguments = _arguments()
    try:
        shelf = read_requirement_shelf(Path.cwd())
        if arguments.base_revision is not None:
            _verify_snapshot_monotonicity(Path.cwd(), arguments.base_revision)
        elif os.environ.get("GITHUB_ACTIONS") == "true":
            raise RequirementContractError(
                "GitHub Actions supplied no exact base revision"
            )
    except RequirementContractError as error:
        print(f"Documentation-order gate refused: {error}", file=sys.stderr)
        return 1
    print(render_summary(shelf), flush=True)
    print(render_honesty_bound(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
