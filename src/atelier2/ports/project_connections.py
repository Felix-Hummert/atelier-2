"""Narrow external boundaries used while managing a project source."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from atelier2.contracts.host_configuration import (
    ProjectSourceId,
    SourceAddress,
    SourceKind,
)


@dataclass(frozen=True)
class ParsedProjectSourceAddress:
    source_kind: SourceKind
    public_address: str


@dataclass(frozen=True)
class ProjectSourceAddressInvalid:
    reason: str


type ParseProjectSourceAddressResult = (
    ParsedProjectSourceAddress | ProjectSourceAddressInvalid
)


@dataclass(frozen=True)
class ValidatedProjectSource:
    source_kind: SourceKind
    source_address: SourceAddress
    public_address: str


@dataclass(frozen=True)
class ProjectSourceAuthenticationRefused:
    reason: str


@dataclass(frozen=True)
class ProjectSourceValidationUnavailable:
    detail: str | None = None


type ValidateProjectSourceResult = (
    ValidatedProjectSource
    | ProjectSourceAuthenticationRefused
    | ProjectSourceAddressInvalid
    | ProjectSourceValidationUnavailable
)


class ProjectSourceConnector(Protocol):
    """Parse provider addresses and prove one credential can read the source."""

    def parse_address(self, address: str) -> ParseProjectSourceAddressResult: ...

    def parse_stored_address(
        self, source_address: SourceAddress
    ) -> ParseProjectSourceAddressResult: ...

    def validate(
        self,
        parsed: ParsedProjectSourceAddress,
        credential_directory: Path,
    ) -> ValidateProjectSourceResult: ...

    def public_address(self, source_address: SourceAddress) -> str: ...


class ManagedCredentialDeposit(Protocol):
    """One staged token that is either atomically published or discarded."""

    @property
    def credential_directory(self) -> Path: ...

    def publish(self) -> Path: ...

    def discard(self) -> None: ...


@dataclass(frozen=True)
class CredentialDepositUnavailable:
    detail: str | None = None


type StageCredentialResult = ManagedCredentialDeposit | CredentialDepositUnavailable


class ManagedProjectSourceCredentialStore(Protocol):
    def stage(
        self, source_id: ProjectSourceId, token: str
    ) -> StageCredentialResult: ...
