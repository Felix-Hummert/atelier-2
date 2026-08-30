from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.requirement_contract import (
    RequirementContractError,
    approval_bytes,
    read_requirement_shelf,
)

PROJECT_ROOT = Path(__file__).parents[2]
REQUIREMENTS = Path("docs/requirements")
REGISTRY = REQUIREMENTS / "revisions.toml"
STRICT_DOCUMENT = REQUIREMENTS / "0008-example.md"
LEGACY_DOCUMENT = REQUIREMENTS / "0008-legacy.md"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_legacy_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    directory = project / REQUIREMENTS
    directory.mkdir(parents=True)
    content = (
        "# Legacy requirement\n\n"
        "### REQ-LEGACY-01: Legacy bytes remain frozen.\n"
        "Quelle: DESK — test fixture\n"
    ).encode()
    (project / LEGACY_DOCUMENT).write_bytes(content)
    (project / REGISTRY).write_text(
        "schema_version = 1\n\n"
        "[[legacy]]\n"
        'document = "0008"\n'
        f'path = "{LEGACY_DOCUMENT}"\n'
        f'content_sha256 = "{sha256(content)}"\n',
        encoding="utf-8",
    )
    return project


def strict_document(
    *, intent: str = "The operator can rely on the result.", rules: str | None = None
) -> str:
    rule_text = rules or (
        "### REQ-EXAMPLE-01: The result is visible.\n"
        "Quelle: OPERATOR — issue 1 comment 2"
    )
    return (
        "# A usable capability\n\n"
        f"## Intent\n\n{intent}\n\n"
        f"## Rules\n\n{rule_text}\n\n"
        "## Non-goals\n\n- Provider-specific policy.\n"
    )


def revision_table(
    content_digest: str,
    *,
    predecessor: str = "GENESIS",
    approval_digest: str | None = None,
    path: str = "docs/requirements/0008-example.md",
    extra: str = "",
) -> str:
    approved = approval_digest or sha256(approval_bytes("0008", content_digest))
    return (
        "[[revision]]\n"
        'document = "0008"\n'
        f'path = "{path}"\n'
        f'content_sha256 = "{content_digest}"\n'
        "approval_comment_id = 123456\n"
        f'approval_sha256 = "{approved}"\n'
        f'predecessor = "{predecessor}"\n'
        f"{extra}"
    )


def write_strict_project(
    tmp_path: Path, content: str, *, registry: str | None = None
) -> Path:
    project = tmp_path / "project"
    directory = project / REQUIREMENTS
    directory.mkdir(parents=True)
    encoded = content.encode()
    (project / STRICT_DOCUMENT).write_bytes(encoded)
    (project / REGISTRY).write_text(
        registry or f"schema_version = 1\n\n{revision_table(sha256(encoded))}",
        encoding="utf-8",
    )
    return project


@pytest.mark.proves("legacy-requirement-bytes-are-frozen-until-migration")
@pytest.mark.proves("a-requirement-rule-names-its-source-and-degree")
def test_the_current_requirement_shelf_matches_its_registry() -> None:
    shelf = read_requirement_shelf(PROJECT_ROOT)

    assert shelf.document_count == 7
    assert shelf.legacy_count == 4


@pytest.mark.proves("legacy-requirement-bytes-are-frozen-until-migration")
def test_legacy_byte_drift_fails_by_document_and_cannot_use_the_new_grammar(
    tmp_path: Path,
) -> None:
    project = write_legacy_project(tmp_path)
    document = project / LEGACY_DOCUMENT
    document.write_bytes(document.read_bytes() + b"\n")

    with pytest.raises(RequirementContractError) as refused:
        read_requirement_shelf(project)

    assert str(LEGACY_DOCUMENT) in str(refused.value)
    assert "legacy content digest" in str(refused.value)
    assert "migrate" in str(refused.value)


@pytest.mark.proves("a-requirement-revision-registry-is-linear-and-bound")
@pytest.mark.parametrize("relative", (STRICT_DOCUMENT, REGISTRY))
def test_current_requirement_files_must_be_regular_and_inside_the_real_shelf(
    tmp_path: Path, relative: Path
) -> None:
    project = write_strict_project(tmp_path, strict_document())
    target = project / relative
    outside = tmp_path / f"outside-{target.name}"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(RequirementContractError) as refused:
        read_requirement_shelf(project)

    assert str(relative) in str(refused.value)
    assert "regular non-symlink" in str(refused.value)


@pytest.mark.proves("a-strict-requirement-has-only-contract-sections")
def test_a_minimal_strict_requirement_is_readable(tmp_path: Path) -> None:
    shelf = read_requirement_shelf(write_strict_project(tmp_path, strict_document()))

    assert tuple(rule.identifier for rule in shelf.rules) == ("REQ-EXAMPLE-01",)
    assert shelf.legacy_count == 0


@pytest.mark.proves("a-strict-requirement-has-only-contract-sections")
@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (strict_document(intent=" "), "empty Intent"),
        (strict_document(rules=" "), "no requirement rule"),
        (
            strict_document(
                rules=(
                    "### REQ-EXAMPLE-01: First.\n"
                    "Quelle: DESK — issue 1\n\n"
                    "### REQ-EXAMPLE-01: Second.\n"
                    "Quelle: DESK — issue 2"
                )
            ),
            "REQ-EXAMPLE-01",
        ),
        (
            strict_document(
                rules="### REQ-EXAMPLE-01: Result.\nQuelle: ROBOT — issue 1"
            ),
            "OPERATOR or DESK",
        ),
        (
            strict_document(rules="### REQ-EXAMPLE-01: Result.\nQuelle: OPERATOR —"),
            "source pointer",
        ),
        (
            strict_document(
                rules=(
                    "### REQ-EXAMPLE-01: Result.\n"
                    "Quelle: OPERATOR — issue 1\n"
                    "Status: AGREED"
                )
            ),
            "unknown rule field",
        ),
        (strict_document() + "\n## Open questions\n\n- Later.\n", "unknown section"),
    ],
    ids=(
        "empty-intent",
        "empty-rules",
        "duplicate-id",
        "unknown-source-grade",
        "empty-source",
        "stale-field",
        "stale-section",
    ),
)
def test_strict_grammar_refuses_missing_or_stale_content(
    tmp_path: Path, content: str, problem: str
) -> None:
    with pytest.raises(RequirementContractError, match=problem):
        read_requirement_shelf(write_strict_project(tmp_path, content))


@pytest.mark.proves("a-requirement-rule-names-its-source-and-degree")
@pytest.mark.parametrize(
    "source",
    ("Quelle: issue 1 comment 2", "Quelle: OPERATOR —   "),
    ids=("missing-grade", "missing-pointer"),
)
def test_a_strict_source_refusal_names_its_rule_and_exact_reason(
    tmp_path: Path, source: str
) -> None:
    content = strict_document(rules=f"### REQ-EXAMPLE-01: Result.\n{source}")

    with pytest.raises(RequirementContractError) as refused:
        read_requirement_shelf(write_strict_project(tmp_path, content))

    assert "REQ-EXAMPLE-01" in str(refused.value)
    assert "OPERATOR or DESK and a source pointer" in str(refused.value)


@pytest.mark.proves("a-strict-requirement-has-only-contract-sections")
def test_a_whitespace_only_strict_rule_sentence_is_named_and_refused(
    tmp_path: Path,
) -> None:
    content = strict_document(
        rules="### REQ-EXAMPLE-01:   \nQuelle: OPERATOR — issue 1 comment 2"
    )

    with pytest.raises(RequirementContractError) as refused:
        read_requirement_shelf(write_strict_project(tmp_path, content))

    assert "REQ-EXAMPLE-01" in str(refused.value)
    assert "sentence" in str(refused.value)


@pytest.mark.proves("a-requirement-revision-registry-is-linear-and-bound")
@pytest.mark.parametrize(
    ("registry_for", "problem"),
    [
        (
            lambda current, _old: (
                "schema_version = 1\nunknown = true\n\n" + revision_table(current)
            ),
            "unknown fields",
        ),
        (
            lambda current, _old: (
                "schema_version = 1\n\n"
                + revision_table(current, extra="surprise = true\n")
            ),
            "unknown fields",
        ),
        (
            lambda current, _old: (
                "schema_version = 1\n\n" + revision_table(current, predecessor=current)
            ),
            "references itself",
        ),
        (
            lambda current, old: (
                "schema_version = 1\n\n" + revision_table(current, predecessor=old)
            ),
            "unknown predecessor",
        ),
        (
            lambda current, old: (
                "schema_version = 1\n\n"
                + revision_table(old)
                + "\n"
                + revision_table(current, predecessor=old)
                + "\n"
                + revision_table(sha256(b"branch"), predecessor=old)
            ),
            "branches",
        ),
        (
            lambda current, old: (
                "schema_version = 1\n\n"
                + revision_table(old)
                + "\n"
                + revision_table(current)
            ),
            "multiple tips",
        ),
        (
            lambda current, _old: (
                "schema_version = 1\n\n"
                + revision_table(current, approval_digest="0" * 64)
            ),
            "approval digest",
        ),
        (
            lambda _current, _old: "schema_version = 1\n\n" + revision_table("f" * 64),
            "current file digest",
        ),
    ],
    ids=(
        "unknown-root-field",
        "unknown-entry-field",
        "self-reference",
        "unknown-predecessor",
        "branch",
        "multiple-tips",
        "approval-digest",
        "file-digest",
    ),
)
def test_invalid_revision_registries_fail_by_reason(
    tmp_path: Path, registry_for: Callable[[str, str], str], problem: str
) -> None:
    content = strict_document()
    current = sha256(content.encode())
    old = sha256(b"older approved requirement")
    registry = registry_for(current, old)

    with pytest.raises(RequirementContractError, match=problem):
        read_requirement_shelf(
            write_strict_project(tmp_path, content, registry=registry)
        )


@pytest.mark.proves("a-requirement-revision-registry-is-linear-and-bound")
def test_a_revision_lineage_refuses_two_numbered_paths_by_document_and_path(
    tmp_path: Path,
) -> None:
    old_content = strict_document(intent="The old intent remains traceable.")
    current_content = strict_document()
    old_digest = sha256(old_content.encode())
    current_digest = sha256(current_content.encode())
    old_path = "docs/requirements/0008-old.md"
    current_path = "docs/requirements/0008-current.md"
    registry = (
        "schema_version = 1\n\n"
        + revision_table(old_digest, path=old_path)
        + "\n"
        + revision_table(current_digest, predecessor=old_digest, path=current_path)
    )
    project = write_strict_project(tmp_path, current_content, registry=registry)
    (project / REQUIREMENTS / "0008-example.md").unlink()
    (project / old_path).write_text(old_content, encoding="utf-8")
    (project / current_path).write_text(current_content, encoding="utf-8")

    with pytest.raises(RequirementContractError) as refused:
        read_requirement_shelf(project)

    assert "requirement 0008" in str(refused.value)
    assert old_path in str(refused.value)
    assert current_path in str(refused.value)
