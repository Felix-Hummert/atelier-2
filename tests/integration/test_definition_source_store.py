"""A definition source registered in a real store, and scanned out of a real repository.

The dogfood is this repository's own `workflows/*.yaml`: one connect claims the
whole directory, and one scan reports every file in it. That is the sentence the
operator was promised -- a source delivers many pieces in one act, not one piece
per act -- proved against real documents rather than a fixture that only ever
says yes.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.definition_source_store import DbosDefinitionSources
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    catalog_source_intakes,
    host_definition_source_revisions,
    host_definition_source_selections,
    initialize_schema,
    published_revisions,
)
from atelier2.contracts.definition_sources import (
    DefinitionSourceAccess,
    DefinitionSourceActor,
    DefinitionSourceId,
    DefinitionSourceKind,
    DefinitionSourceRevision,
    DefinitionSourceSelection,
    RepositoryLocation,
    RepositoryPath,
    RepositoryRef,
    SelectionPattern,
    SourceCommit,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.host import main
from atelier2.ports.definition_sources import (
    DefinitionSourceFound,
    DefinitionSourceMissing,
    DefinitionSourceRegistered,
    DefinitionSourceUnchanged,
)

MAIN = "refs/heads/main"
WORKFLOW_SELECTION = "workflows/*.yaml"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_AUTHORED_BY_THE_SCENARIO = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "scenario",
    "GIT_AUTHOR_EMAIL": "scenario@invalid",
    "GIT_COMMITTER_NAME": "scenario",
    "GIT_COMMITTER_EMAIL": "scenario@invalid",
}


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(path)
    initialize_schema(engine)
    engine.dispose()
    yield path


@pytest.fixture
def engine(database: Path) -> Iterator[Engine]:
    opened = create_canonical_engine(database)
    try:
        yield opened
    finally:
        opened.dispose()


def registration(
    *patterns: str, location: str = "/srv/definitions.git", ref: str = MAIN
) -> DefinitionSourceRevision:
    return DefinitionSourceRevision(
        1,
        DefinitionSourceKind.GIT,
        RepositoryLocation(location),
        RepositoryRef(ref),
        DefinitionSourceAccess.ANONYMOUS,
        DefinitionSourceActor("felix"),
        tuple(
            DefinitionSourceSelection(SelectionPattern(pattern), RevisionKind.WORKFLOW)
            for pattern in (patterns or (WORKFLOW_SELECTION,))
        ),
    )


def bare_repository_of(path: Path, files: Mapping[str, bytes]) -> str:
    """A bare repository carrying exactly these files at `refs/heads/main`."""

    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--bare", "--quiet", "--initial-branch=main", ".")
    return commit_to(path, files)


def commit_to(path: Path, files: Mapping[str, bytes]) -> str:
    described = "".join(
        f"100644 {_git(path, 'hash-object', '-w', '--stdin', stdin=content)}\t{name}\n"
        for name, content in files.items()
    )
    (path / "scenario.index").unlink(missing_ok=True)
    _git(path, "update-index", "--index-info", stdin=described.encode("utf-8"))
    commit = _git(path, "commit-tree", _git(path, "write-tree"), "-m", "scenario")
    _git(path, "update-ref", MAIN, commit)
    return commit


def _git(path: Path, *arguments: str, stdin: bytes = b"") -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=path,
        env={
            **os.environ,
            **_AUTHORED_BY_THE_SCENARIO,
            "GIT_DIR": str(path),
            "GIT_INDEX_FILE": str(path / "scenario.index"),
        },
        input=stdin,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("utf-8").strip()


def authored_workflows() -> Mapping[str, bytes]:
    """Every workflow this repository authors, as the source would serve them."""

    return {
        f"workflows/{authored.name}": authored.read_bytes()
        for authored in sorted((REPOSITORY_ROOT / "workflows").glob("*.yaml"))
    }


def logical_dump(database: Path) -> tuple[str, ...]:
    with sqlite3.connect(database) as connection:
        return tuple(connection.iterdump())


def stored_intake(
    engine: Engine,
    revision: DefinitionSourceRevision,
    path: str,
    document: bytes,
    intake_number: int,
) -> None:
    """One provenance row as the intake operation will write it.

    Written here rather than through an application door because the intake
    operation is the next slice; what this slice owes is that a scan reads the
    highest of these, and that is exactly what these rows let it be asked.
    """

    published = PublishedRevisionHash.of(document)
    with engine.begin() as connection:
        connection.execute(
            published_revisions.insert()
            .prefix_with("OR IGNORE")
            .values(
                kind=RevisionKind.WORKFLOW.value,
                revision_hash=published.value,
                document=document,
            )
        )
        connection.execute(
            catalog_source_intakes.insert().values(
                source_id=revision.source_id.value,
                source_path=path,
                intake_number=intake_number,
                revision_kind=RevisionKind.WORKFLOW.value,
                revision_hash=published.value,
                source_commit="a" * 40,
                intaken_by="felix",
                intaken_at="2026-09-02T00:00:00Z",
            )
        )


def test_a_connected_source_and_its_selections_survive_a_new_store_reader(
    engine: Engine, database: Path
) -> None:
    revision = registration("workflows/*.yaml", "flows/*.yaml")

    stored = DbosDefinitionSources(engine).register(revision)
    engine.dispose()

    assert stored == DefinitionSourceRegistered(revision)
    reopened = create_canonical_engine(database)
    try:
        read = DbosDefinitionSources(reopened).read_source(revision.source_id)
    finally:
        reopened.dispose()
    assert read == DefinitionSourceFound(revision)


def test_connecting_the_same_source_again_keeps_one_source_and_one_revision(
    engine: Engine,
) -> None:
    sources = DbosDefinitionSources(engine)
    sources.register(registration())

    again = sources.register(registration())

    assert again == DefinitionSourceUnchanged(registration())
    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(host_definition_source_revisions)
            ).scalar_one()
            == 1
        )


def test_a_changed_selection_set_appends_a_revision_of_the_same_source(
    engine: Engine,
) -> None:
    sources = DbosDefinitionSources(engine)
    sources.register(registration())

    widened = sources.register(registration("workflows/*.yaml", "flows/*.yaml"))

    assert isinstance(widened, DefinitionSourceRegistered)
    assert widened.revision.source_id == registration().source_id
    assert widened.revision.revision_number == 2
    assert sources.read_source(registration().source_id) == DefinitionSourceFound(
        widened.revision
    )


def test_a_source_nobody_registered_is_missing(engine: Engine) -> None:
    unknown = DefinitionSourceId("f" * 64)

    assert DbosDefinitionSources(engine).read_source(unknown) == (
        DefinitionSourceMissing(unknown)
    )


def test_the_latest_intake_of_a_path_is_the_highest_intake_number(
    engine: Engine,
) -> None:
    revision = registration()
    DbosDefinitionSources(engine).register(revision)
    stored_intake(engine, revision, "workflows/build.yaml", b"first bytes", 1)
    stored_intake(engine, revision, "workflows/build.yaml", b"second bytes", 2)
    stored_intake(engine, revision, "workflows/ship.yaml", b"ship bytes", 1)

    latest = DbosDefinitionSources(engine).latest_intakes(revision.source_id)

    assert isinstance(latest, Mapping)
    assert {
        path.value: (intake.intake_number, intake.revision_hash)
        for path, intake in latest.items()
    } == {
        "workflows/build.yaml": (2, PublishedRevisionHash.of(b"second bytes")),
        "workflows/ship.yaml": (1, PublishedRevisionHash.of(b"ship bytes")),
    }
    assert latest[RepositoryPath("workflows/build.yaml")].source_commit == (
        SourceCommit("a" * 40)
    )


@pytest.mark.parametrize(
    "table",
    [
        host_definition_source_revisions,
        host_definition_source_selections,
        catalog_source_intakes,
    ],
    ids=lambda table: str(table.name),
)
def test_what_a_definition_source_recorded_cannot_be_edited_or_removed(
    engine: Engine, table: sa.Table
) -> None:
    revision = registration()
    DbosDefinitionSources(engine).register(revision)
    stored_intake(engine, revision, "workflows/build.yaml", b"bytes", 1)

    for statement in (table.delete(), table.update().values(revision_hash="0" * 64)):
        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as opened:
            opened.execute(statement)


def test_the_command_connects_a_repository_and_scans_every_workflow_in_it(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    authored = authored_workflows()
    repository = tmp_path / "definitions.git"
    commit = bare_repository_of(repository, {**authored, "README.md": b"not selected"})

    assert (
        main(
            [
                "definition-source",
                "connect",
                "--database",
                str(database),
                "--location",
                str(repository),
                "--ref",
                MAIN,
                "--select",
                f"{WORKFLOW_SELECTION}=workflow",
                "--actor",
                "felix",
            ]
        )
        == 0
    )
    connected = capsys.readouterr().out
    source_id = connected.split(" as ")[1].split(" revision ")[0]
    before = logical_dump(database)

    assert (
        main(
            [
                "definition-source",
                "scan",
                "--database",
                str(database),
                "--source-id",
                source_id,
            ]
        )
        == 0
    )

    scanned = capsys.readouterr().out
    assert commit in scanned
    assert [line.split() for line in scanned.splitlines()[1:]] == [
        ["source_ahead", "workflow", path] for path in sorted(authored)
    ]
    assert logical_dump(database) == before


def test_a_moved_ref_changes_the_commit_the_command_reports(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = tmp_path / "definitions.git"
    document = (REPOSITORY_ROOT / "workflows" / "hello-atelier.yaml").read_bytes()
    first = bare_repository_of(repository, {"workflows/hello.yaml": document})
    main(
        [
            "definition-source",
            "connect",
            "--database",
            str(database),
            "--location",
            str(repository),
            "--ref",
            MAIN,
            "--select",
            f"{WORKFLOW_SELECTION}=workflow",
            "--actor",
            "felix",
        ]
    )
    source_id = capsys.readouterr().out.split(" as ")[1].split(" revision ")[0]
    scan = [
        "definition-source",
        "scan",
        "--database",
        str(database),
        "--source-id",
        source_id,
    ]
    assert main(scan) == 0
    standing = capsys.readouterr().out

    second = commit_to(repository, {"workflows/hello.yaml": document + b"\n"})
    assert main(scan) == 0

    moved = capsys.readouterr().out
    assert first != second
    assert first in standing and second in moved


def test_the_command_refuses_a_source_id_nobody_registered(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "definition-source",
            "scan",
            "--database",
            str(database),
            "--source-id",
            "f" * 64,
        ]
    )

    assert exit_code == 1
    assert "no definition source is registered" in capsys.readouterr().err
