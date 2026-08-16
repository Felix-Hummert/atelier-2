from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from importlinter.api import read_configuration
from importlinter.cli import lint_imports

EXPECTED_SOURCE_MODULE_FLOOR = 103
EXPECTED_CONTRACT_NAMES = {
    "layers": "Atelier package layers",
    "root-facade": "Root facade cannot bypass ports",
    "dbos-owner": "DBOS and SQLAlchemy stay inside their adapter",
    "wire-projection-split": "Wire schemas name no port type",
    "route-vocabulary": "Routes name no port type",
    "schema-owner": "JSON Schema evaluation stays inside one profile owner",
}
PORT_PACKAGE = "atelier2.ports"
USE_CASE_RECORD_MODULE = "src/atelier2/api/context.py"
USE_CASE_RECORD_NAME = "ApiUseCases"
PORTS_RECORD_NAME = "ApiPorts"
ROUTE_PACKAGE = "src/atelier2/api/routes"
ROUTE_MODULES_STILL_HOLDING_PORTS = frozenset({"agents", "events", "revisions", "runs"})
"""Route modules that have not been translated into use-cases yet.

The list is read in both directions: a module outside it that reaches a port is
red, and a module inside it that reaches none is red too. A stale entry is
therefore a failure rather than a comfortable lie, exactly as
`unmatched_ignore_imports_alerting = "error"` already reads the import contract.
It shrinks to empty, and with the last entry it deletes itself.
"""
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


def _parsed(module: Path) -> ast.Module:
    return ast.parse(module.read_text(encoding="utf-8"), filename=str(module))


def _port_names_bound_by(module: ast.Module) -> frozenset[str]:
    """Every local name in this module that stands for something under `ports`.

    `if TYPE_CHECKING:` blocks are read like any other, because this check is
    textual and does not honour the quarantine — the same escape `pyproject.toml`
    keeps shut repository-wide with `exclude_type_checking_imports = false`.
    """
    bound: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom):
            source = node.module or ""
            if source == PORT_PACKAGE or source.startswith(f"{PORT_PACKAGE}."):
                bound.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PORT_PACKAGE or alias.name.startswith(
                    f"{PORT_PACKAGE}."
                ):
                    bound.add(alias.asname or alias.name.split(".")[0])
    return frozenset(bound)


def _annotation_expression(annotation: ast.expr) -> ast.expr:
    """The type an annotation states, with quoting and `Annotated` metadata removed."""
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return _annotation_expression(
            ast.parse(annotation.value, mode="eval").body,
        )
    if isinstance(annotation, ast.Subscript):
        base = annotation.value
        named = (
            base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        )
        if named == "Annotated" and isinstance(annotation.slice, ast.Tuple):
            return _annotation_expression(annotation.slice.elts[0])
    return annotation


def _dotted_path(attribute: ast.Attribute) -> str:
    parts = [attribute.attr]
    current: ast.expr = attribute.value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _named_in(annotation: ast.expr) -> tuple[frozenset[str], tuple[str, ...]]:
    reduced = _annotation_expression(annotation)
    names = {node.id for node in ast.walk(reduced) if isinstance(node, ast.Name)}
    paths = tuple(
        _dotted_path(node)
        for node in ast.walk(reduced)
        if isinstance(node, ast.Attribute)
    )
    return frozenset(names), paths


def use_case_record_problems(project_root: Path) -> tuple[str, ...]:
    """Every way the use-case record could hand a port back to a route.

    The record is what the routes hold, so a field of it that resolves to a port
    reopens exactly the call the three other locks close. There is no exception
    list: the record does not predate this rule, so it never legitimately holds
    one.
    """
    module_path = project_root / USE_CASE_RECORD_MODULE
    module = _parsed(module_path)
    port_names = _port_names_bound_by(module)
    records = [
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == USE_CASE_RECORD_NAME
    ]
    if not records:
        return (
            (
                f"{USE_CASE_RECORD_MODULE} declares no {USE_CASE_RECORD_NAME}; "
                "the routes' use-case record is what this check exists for"
            ),
        )
    problems: list[str] = []
    for statement in records[0].body:
        if isinstance(statement, (ast.Assign, ast.AugAssign)):
            problems.append(
                f"{USE_CASE_RECORD_NAME} carries an unannotated assignment; "
                "a field without an annotation is a hole in this check"
            )
            continue
        if not isinstance(statement, ast.AnnAssign):
            continue
        names, paths = _named_in(statement.annotation)
        reaches_port = bool(names & port_names) or any(
            path.startswith(("ports.", f"{PORT_PACKAGE}.")) for path in paths
        )
        if reaches_port:
            field = ast.unparse(statement.target)
            problems.append(
                f"{USE_CASE_RECORD_NAME}.{field} is annotated with "
                f"{ast.unparse(statement.annotation)}, which resolves to {PORT_PACKAGE}"
            )
    return tuple(problems)


def _route_module_reaches_ports(module: ast.Module) -> bool:
    for node in ast.walk(module):
        if isinstance(node, ast.Name) and node.id == PORTS_RECORD_NAME:
            return True
        if isinstance(node, ast.Attribute) and node.attr == "ports":
            return True
    return False


def route_port_problems(project_root: Path) -> tuple[str, ...]:
    """Which route modules still reach a port, read against the declared list."""
    problems: list[str] = []
    for module_path in sorted((project_root / ROUTE_PACKAGE).glob("*.py")):
        name = module_path.stem
        reaches = _route_module_reaches_ports(_parsed(module_path))
        declared = name in ROUTE_MODULES_STILL_HOLDING_PORTS
        if reaches and not declared:
            problems.append(
                f"{ROUTE_PACKAGE}/{name}.py reaches a port; a route reads the "
                "use-case record the composition bound for it"
            )
        if declared and not reaches:
            problems.append(
                f"{ROUTE_PACKAGE}/{name}.py reaches no port any more; remove it "
                "from ROUTE_MODULES_STILL_HOLDING_PORTS"
            )
    return tuple(problems)


def architecture_preflight(project_root: Path) -> ArchitectureConfiguration:
    source_count = source_module_count(project_root / "src/atelier2")
    if source_count < EXPECTED_SOURCE_MODULE_FLOOR:
        raise ArchitecturePreflightError(
            f"found {source_count} source modules; expected at least {EXPECTED_SOURCE_MODULE_FLOOR}"
        )
    problems = use_case_record_problems(project_root) + route_port_problems(
        project_root
    )
    if problems:
        raise ArchitecturePreflightError(
            "a route can still reach a port:\n  " + "\n  ".join(problems)
        )
    configuration = read_architecture_configuration(project_root / "pyproject.toml")
    print(
        "Architecture preflight: "
        f"{source_count} source modules, {len(configuration.contracts)} contracts, "
        f"{len(configuration.layer_members)} layer members, "
        f"{len(ROUTE_MODULES_STILL_HOLDING_PORTS)} route modules still holding ports",
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
