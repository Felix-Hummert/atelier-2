"""A local git repository read as a definition source, once per scan.

The ref is resolved to a commit exactly once, and every path and byte of that
scan comes out of that commit -- so a ref that moves mid-scan cannot hand back
half of one tree and half of another. Reading is write-free in the strong
sense: no ref is created, no object is written, and no index of the operator's
is touched.

Every git call crosses the boundary `adapters/project_source` already owns, so
a definition source inherits the same hookless, configuration-free child that
a project source gets: no repository's `pre-push`, no machine-wide `filter`,
no credential helper, and no environment variable nobody here reasoned about.

What this reader refuses, it refuses by the source's own closed vocabulary and
before any caller has opened a transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from atelier2.adapters.project_source import (
    GitRefused,
    answered_git,
    isolated_git_environment,
)
from atelier2.contracts.definition_sources import (
    AmbiguousSelection,
    DefinitionSourceConfiguration,
    DefinitionSourceRefusal,
    DefinitionSourceSelection,
    RepositoryPath,
    SourceCommit,
)
from atelier2.ports.definition_sources import (
    DefinitionSourceUnreadable,
    ScannedSource,
    SelectedFile,
)

_REGULAR_BLOB_MODES = frozenset({"100644", "100755"})
_SYMLINK_MODE = "120000"
_GITLINK_MODE = "160000"
"""The tree entry modes this reader knows. Git prints them zero-padded to six."""

_ENTRY_SEPARATOR = "\0"
_PATH_SEPARATOR = "\t"
_COMMIT_OF = "^{commit}"
_END_OF_OPTIONS = "--end-of-options"
"""What keeps a configured ref that starts with a dash a ref rather than a flag."""


@dataclass(frozen=True)
class _GitAnswered:
    """Git answered this probe with one line."""

    line: str


@dataclass(frozen=True)
class _GitSilent:
    """Git gave this probe no line, and this is why it gave none."""

    reason: str


type _Probed = _GitAnswered | _GitSilent


class GitDefinitionSource:
    """One local git repository as the source of the definitions it carries."""

    def resolve(self, configuration: DefinitionSourceConfiguration) -> SourceCommit:
        """The commit this source stands on, reading no file of it.

        What a caller learns from this is exactly whether the location is a
        repository and whether the ref names something in it, which is what a
        registration is answerable for; everything a *selection* can be wrong
        about is the scan's to say.
        """

        return self._resolved_commit(configuration.location.value, configuration)

    def scan(self, configuration: DefinitionSourceConfiguration) -> ScannedSource:
        repository = configuration.location.value
        commit = self.resolve(configuration)
        selected = tuple(self._selected(repository, commit, configuration))
        if not selected:
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.NO_SELECTED_FILES,
                f"{commit.value} carries no file the configured selections claim",
            )
        return ScannedSource(commit, selected)

    def _resolved_commit(
        self, repository: str, configuration: DefinitionSourceConfiguration
    ) -> SourceCommit:
        """The commit the configured ref names, refusing the two failures apart.

        A repository that cannot be read at all and a ref that does not resolve
        are different sentences to the operator -- a typo in the location and a
        typo in the branch -- so the repository is asked first.
        """

        self._require_repository_at(repository)
        try:
            named = self._line(
                repository,
                (
                    "rev-parse",
                    "--verify",
                    _END_OF_OPTIONS,
                    f"{configuration.ref.value}{_COMMIT_OF}",
                ),
            )
            return SourceCommit(named)
        except (GitRefused, ValueError) as error:
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.REF_UNRESOLVED,
                f"{configuration.ref.value!r} names no commit in {repository}: {error}",
            ) from error

    def _require_repository_at(self, repository: str) -> None:
        """Refuse unless the configured location *is* the repository being read.

        Git walks upwards until it finds one, so a plain directory inside a
        checkout answers with the repository above it -- and a source
        configured at that directory would then be scanned out of a repository
        the operator never named, under a source id minted from a location that
        holds none of those files. The answer is therefore compared against the
        location rather than merely awaited.

        A working tree is asked for its top level rather than for its git
        directory, because the two are only the same for a plain checkout: a
        linked worktree keeps its administrative files under the repository
        that created it, so its git directory is never anywhere near the
        location the operator gave. A bare repository has no top level at all,
        and answers for itself with its git directory.
        """

        try:
            resolved = Path(repository).resolve(strict=True)
        except OSError as error:
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.UNREACHABLE,
                f"{repository} names no directory: {error}",
            ) from error
        probed = self._answering(repository, ("rev-parse", "--show-toplevel"))
        if isinstance(probed, _GitSilent):
            self._require_bare_repository_at(repository, resolved)
            return
        top_level = probed.line
        if Path(top_level).resolve() != resolved:
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.UNREACHABLE,
                f"{repository} is not a git repository; it lies inside the working "
                f"tree at {top_level}, whose content this source never named",
            )

    def _require_bare_repository_at(self, repository: str, resolved: Path) -> None:
        """A location with no working tree is a repository only if it is the bare one.

        Git's own words travel into the refusal: a location that is merely
        misspelled and one this user may not enter earn the same word, and
        only what git said tells the operator which of the two they have.
        """

        probed = self._answering(repository, ("rev-parse", "--absolute-git-dir"))
        if isinstance(probed, _GitSilent):
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.UNREACHABLE,
                f"{repository} could not be read as a git repository: {probed.reason}",
            )
        found = probed.line
        if Path(found).resolve() != resolved:
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.UNREACHABLE,
                f"{repository} is not a git repository; it lies inside the one at "
                f"{found}, whose content this source never named",
            )

    def _answering(self, repository: str, arguments: tuple[str, ...]) -> _Probed:
        """What git said here, or why it said nothing.

        A silent probe is an answer rather than a failure -- a bare repository
        has no top level -- so the reason travels beside the silence instead of
        replacing it, and the caller that turns silence into a refusal is the
        one that can say why.
        """

        try:
            answered = self._line(repository, arguments)
        except (GitRefused, OSError) as error:
            return _GitSilent(str(error))
        if not answered:
            return _GitSilent(f"git {' '.join(arguments)} answered no line")
        return _GitAnswered(answered)

    def _selected(
        self,
        repository: str,
        commit: SourceCommit,
        configuration: DefinitionSourceConfiguration,
    ) -> Iterator[SelectedFile]:
        for mode, object_name, path in self._entries(repository, commit):
            selection = self._claiming(configuration, path)
            if selection is None:
                continue
            self._require_regular_blob(mode, path)
            yield SelectedFile(
                path, selection, self._blob(repository, object_name, path)
            )

    def _entries(
        self, repository: str, commit: SourceCommit
    ) -> tuple[tuple[str, str, RepositoryPath], ...]:
        """Every file the commit's tree holds, in byte-stable path order.

        Git's own tree order sorts a directory as though it ended in a
        separator, so it is not the order of the paths it prints; sorting here
        makes one scan of one commit answer the same sequence every time,
        whatever git's storage order happens to be.
        """

        try:
            listed = self._answered(
                repository, ("ls-tree", "-r", "-z", "--full-tree", commit.value)
            ).decode("utf-8")
        except GitRefused as error:
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.UNREACHABLE,
                f"the tree of {commit.value} in {repository} could not be listed: "
                f"{error}",
            ) from error
        except UnicodeDecodeError as error:
            # Refused whole rather than per entry: a path that is not text
            # cannot be compared against a pattern at all, so this reader
            # cannot even say whether the operator selected it.
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.LAYOUT_UNRECOGNIZED,
                f"{repository} carries a path at {commit.value} that is not "
                f"exact UTF-8: {error}",
            ) from error
        return tuple(
            sorted(
                (self._entry(record, repository) for record in _records(listed)),
                key=lambda entry: entry[2].value.encode("utf-8"),
            )
        )

    def _entry(self, record: str, repository: str) -> tuple[str, str, RepositoryPath]:
        head, separator, raw_path = record.partition(_PATH_SEPARATOR)
        fields = head.split(" ")
        if not separator or len(fields) != 3:
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.LAYOUT_UNRECOGNIZED,
                f"{repository} listed the tree entry {record!r}, which is no "
                "mode, type, object and path",
            )
        mode, _object_type, object_name = fields
        try:
            return mode, object_name, RepositoryPath(raw_path)
        except ValueError as error:
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.PATH_ESCAPES_REPOSITORY,
                f"{repository} carries the path {raw_path!r}: {error}",
            ) from error

    def _claiming(
        self, configuration: DefinitionSourceConfiguration, path: RepositoryPath
    ) -> DefinitionSourceSelection | None:
        try:
            return configuration.selection_for(path)
        except AmbiguousSelection as error:
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.SELECTION_AMBIGUOUS, str(error)
            ) from error

    def _require_regular_blob(self, mode: str, path: RepositoryPath) -> None:
        """Refuse a selected entry that is not a file whose bytes are its content.

        A symlink's bytes are a path into a tree nobody published, and a
        gitlink's are a commit of another repository entirely; taking either as
        a document would publish a reference dressed as content.
        """

        if mode in _REGULAR_BLOB_MODES:
            return
        refusal = {
            _SYMLINK_MODE: DefinitionSourceRefusal.SYMLINK_SELECTED,
            _GITLINK_MODE: DefinitionSourceRefusal.GITLINK_SELECTED,
        }.get(mode, DefinitionSourceRefusal.LAYOUT_UNRECOGNIZED)
        raise DefinitionSourceUnreadable(
            refusal,
            f"the selected path {path.value!r} is a tree entry of mode {mode}, "
            "not a regular file",
        )

    def _blob(self, repository: str, object_name: str, path: RepositoryPath) -> bytes:
        try:
            return self._answered(repository, ("cat-file", "blob", object_name))
        except GitRefused as error:
            raise DefinitionSourceUnreadable(
                DefinitionSourceRefusal.UNREACHABLE,
                f"the selected path {path.value!r} could not be read out of "
                f"{repository}: {error}",
            ) from error

    def _line(self, repository: str, arguments: tuple[str, ...]) -> str:
        return self._answered(repository, arguments).decode("utf-8", "replace").strip()

    def _answered(self, repository: str, arguments: tuple[str, ...]) -> bytes:
        return answered_git(
            arguments,
            working_directory=repository,
            environment=isolated_git_environment(),
        )


def _records(listed: str) -> Iterator[str]:
    return (record for record in listed.split(_ENTRY_SEPARATOR) if record)
