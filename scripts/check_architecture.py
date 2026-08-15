from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from importlinter.api import read_configuration
from importlinter.cli import lint_imports

EXPECTED_SOURCE_MODULE_FLOOR = 64
EXPECTED_CONTRACT_NAMES = {
    "layers": "Atelier package layers",
    "root-facade": "Root facade cannot bypass ports",
    "dbos-owner": "DBOS and SQLAlchemy stay inside their adapter",
}
EXPECTED_LAYER_ROWS = (
    "__main__",
    "host",
    "api | adapters",
    "application",
    "ports",
    "contracts",
)
EXPECTED_LAYER_MEMBERS = frozenset(
    {"__main__", "host", "api", "adapters", "application", "ports", "contracts"}
)


@dataclass(frozen=True, slots=True)
class ArchitectureConfiguration:
    contracts: tuple[dict[str, Any], ...]
    layer_rows: tuple[str, ...]
    layer_members: frozenset[str]
    dbos_owner: str
    root_facade_owners: tuple[str, ...]


class ArchitecturePreflightError(Exception):
    pass


def read_architecture_configuration(
    configuration_path: Path,
) -> ArchitectureConfiguration:
    configuration = read_configuration(str(configuration_path))
    contracts = tuple(configuration["contracts_options"])
    actual_contract_names = {
        str(contract.get("id", "")): str(contract.get("name", ""))
        for contract in contracts
    }
    if len(actual_contract_names) != len(contracts):
        raise ArchitecturePreflightError("contract identifiers must be unique")
    if actual_contract_names != EXPECTED_CONTRACT_NAMES:
        raise ArchitecturePreflightError(
            "the reviewed contract identifiers or names changed"
        )

    layers_contract = next(
        contract for contract in contracts if contract["id"] == "layers"
    )
    layer_rows = tuple(layers_contract.get("layers", ()))
    layer_members = frozenset(
        member.strip() for row in layer_rows for member in str(row).split("|")
    )
    if layer_rows != EXPECTED_LAYER_ROWS or layer_members != EXPECTED_LAYER_MEMBERS:
        raise ArchitecturePreflightError(
            "the reviewed layer order or member set changed"
        )

    dbos_contract = next(
        contract for contract in contracts if contract["id"] == "dbos-owner"
    )
    dbos_owners = {
        str(import_expression).split(" -> ", 1)[0].removesuffix(".**")
        for import_expression in dbos_contract.get("ignore_imports", ())
    }
    if len(dbos_owners) != 1:
        raise ArchitecturePreflightError("the DBOS external-import owner is ambiguous")

    root_contract = next(
        contract for contract in contracts if contract["id"] == "root-facade"
    )
    root_facade_owners = tuple(
        dict.fromkeys(
            str(module).removeprefix("atelier2.").split(".", 1)[0]
            for module in root_contract.get("forbidden_modules", ())
        )
    )
    return ArchitectureConfiguration(
        contracts,
        layer_rows,
        layer_members,
        dbos_owners.pop(),
        root_facade_owners,
    )


def render_contract_view(configuration: ArchitectureConfiguration) -> str:
    return "\n".join(
        (
            "```text",
            f"layers: {' > '.join(configuration.layer_rows)}",
            f"dbos-owner: {configuration.dbos_owner}",
            f"root-facade-forbids: {', '.join(configuration.root_facade_owners)}",
            "```",
        )
    )


def source_module_count(source_root: Path) -> int:
    return sum(1 for _ in source_root.rglob("*.py"))


def architecture_preflight(project_root: Path) -> ArchitectureConfiguration:
    source_count = source_module_count(project_root / "src/atelier2")
    if source_count < EXPECTED_SOURCE_MODULE_FLOOR:
        raise ArchitecturePreflightError(
            f"found {source_count} source modules; expected at least {EXPECTED_SOURCE_MODULE_FLOOR}"
        )
    configuration = read_architecture_configuration(project_root / "pyproject.toml")
    print(
        "Architecture preflight: "
        f"{source_count} source modules, {len(configuration.contracts)} contracts, "
        f"{len(configuration.layer_members)} layer members",
        flush=True,
    )
    return configuration


def main() -> int:
    project_root = Path.cwd()
    try:
        architecture_preflight(project_root)
    except (
        ArchitecturePreflightError,
        FileNotFoundError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        print(f"Architecture preflight refused: {error}", file=sys.stderr)
        return 1
    sys.path.insert(0, str(project_root / "src"))
    return lint_imports(
        config_filename=str(project_root / "pyproject.toml"),
        no_cache=True,
        show_timings=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
