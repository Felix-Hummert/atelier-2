"""Filesystem home of the host configuration channel.

A directory of content-addressed documents, not a table in a project store.
The store cannot hold `project id → root path`: that fact is what names the
store.
"""

from __future__ import annotations

import os
from pathlib import Path

from atelier2.contracts.hashing import SHA256_HEX_DIGEST
from atelier2.contracts.host_configuration import (
    HostConfigurationInvalid,
    HostConfigurationUnreadable,
    HostProjectRootRevision,
    HostProjectRootRevisionCollision,
    HostProjectRootRevisionConflict,
    HostProjectRootRevisionCreated,
    HostProjectRootRevisionExisting,
    ProjectId,
    ProjectRootFound,
    ProjectUnknown,
    read_host_project_root_document,
)

type PublishHostProjectRootResult = (
    HostProjectRootRevisionCreated
    | HostProjectRootRevisionExisting
    | HostProjectRootRevisionConflict
    | HostProjectRootRevisionCollision
    | HostConfigurationInvalid
    | HostConfigurationUnreadable
)

type ProjectRootLookup = ProjectRootFound | ProjectUnknown


class HostConfigurationChannel:
    """One append-only host-configuration directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def require_readable(self) -> None:
        """Refuse a declared channel that is not a readable directory."""

        if self._root.is_symlink() or not self._root.is_dir():
            raise HostConfigurationUnreadable(
                f"the host configuration channel must be a directory, not {self._root}"
            )
        try:
            list(self._root.iterdir())
        except OSError as error:
            raise HostConfigurationUnreadable(str(error)) from error

    def publish(self, document: bytes) -> PublishHostProjectRootResult:
        verdict = read_host_project_root_document(document)
        if isinstance(verdict, HostConfigurationInvalid):
            return verdict
        revision = HostProjectRootRevision(document, verdict)
        try:
            self.require_readable()
            stored = self._load_revisions()
        except HostConfigurationUnreadable as refusal:
            return refusal
        destination = self._root / revision.revision_hash.value
        if destination.is_file():
            try:
                existing_bytes = destination.read_bytes()
            except OSError as error:
                return HostConfigurationUnreadable(str(error))
            if existing_bytes == document:
                return HostProjectRootRevisionExisting(revision)
            return HostProjectRootRevisionCollision()
        for existing in stored:
            if (
                existing.project_id == revision.project_id
                and existing.revision_number == revision.revision_number
            ):
                if existing.document == document:
                    return HostProjectRootRevisionExisting(existing)
                return HostProjectRootRevisionConflict()
        temporary = self._root / f".{revision.revision_hash.value}.tmp"
        try:
            temporary.write_bytes(document)
            os.replace(temporary, destination)
        except OSError as error:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
            return HostConfigurationUnreadable(str(error))
        if temporary.exists():
            temporary.unlink(missing_ok=True)
        return HostProjectRootRevisionCreated(revision)

    def project_root(self, project_id: ProjectId) -> ProjectRootLookup:
        matching = tuple(
            revision
            for revision in self._load_revisions()
            if revision.project_id == project_id
        )
        if not matching:
            return ProjectUnknown(project_id)
        numbers = [revision.revision_number for revision in matching]
        if len(numbers) != len(set(numbers)):
            raise HostConfigurationUnreadable(
                f"project {project_id.value!r} has two mappings at one revision number"
            )
        latest = max(matching, key=lambda revision: revision.revision_number)
        return ProjectRootFound(latest.root_path, latest)

    def _load_revisions(self) -> tuple[HostProjectRootRevision, ...]:
        self.require_readable()
        loaded: list[HostProjectRootRevision] = []
        try:
            entries = tuple(self._root.iterdir())
        except OSError as error:
            raise HostConfigurationUnreadable(str(error)) from error
        for path in entries:
            if path.name.startswith("."):
                continue
            if SHA256_HEX_DIGEST.fullmatch(path.name) is None:
                raise HostConfigurationUnreadable(
                    f"{path.name} is not a host-configuration revision"
                )
            try:
                document = path.read_bytes()
            except OSError as error:
                raise HostConfigurationUnreadable(str(error)) from error
            verdict = read_host_project_root_document(document)
            if isinstance(verdict, HostConfigurationInvalid):
                raise HostConfigurationUnreadable(
                    f"{path.name} is not a project-root mapping: {verdict}"
                )
            revision = HostProjectRootRevision(document, verdict)
            if revision.revision_hash.value != path.name:
                raise HostConfigurationUnreadable(
                    f"{path.name} disagrees with its bytes"
                )
            loaded.append(revision)
        return tuple(loaded)
