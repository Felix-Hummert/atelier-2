from __future__ import annotations

import ast
import importlib
import sys
import typing
from collections import abc
from collections.abc import Iterator
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
APPLICATION_PACKAGE = "atelier2.application"
USE_CASE_RECORD_IMPORT = "atelier2.api.context"
USE_CASE_RECORD_MODULE = "src/atelier2/api/context.py"
USE_CASE_RECORD_NAME = "ApiUseCases"
PORTS_RECORD_NAME = "ApiPorts"
ROUTE_PACKAGE = "src/atelier2/api/routes"
ROUTE_CALLS_STILL_HOLDING_PORTS = {
    "agents": frozenset(
        {
            "publish_auth_profile_revision_route",
            "publish_agent_configuration_revision_route",
        }
    ),
    "events": frozenset({"event_stream_route"}),
    "revisions": frozenset({"publish_revision"}),
    "runs": frozenset(
        {
            "start_run_route",
            "cancel_agent_attempt_route",
            "answer_run_route",
            "reconcile_run_route",
        }
    ),
}
"""The exact calls that have not been translated into use-cases yet.

Named per call rather than per module, because a module is not translated all at
once: `events` legitimately keeps a port for the stream its B3 head still owns,
and a whole-module exception would let the *read* beside it quietly take its port
back while every proof stayed green. The unit of the exception is the unit of the
work.

Read in both directions: a call outside this map that reaches a port is red, and a
call named here that reaches none is red too. A stale entry is a failure rather
than a comfortable lie, exactly as `unmatched_ignore_imports_alerting = "error"`
already reads the import contract. It shrinks to empty, and with the last entry it
deletes itself.
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


def _record_under_test(project_root: Path) -> type:
    """The record as Python resolves it, from the tree being checked.

    Reading the annotations as text can only ever compare spellings, and a route
    receives the resolved object rather than its spelling — an alias or a
    re-export defeats any amount of name matching. So the type is resolved by the
    language itself.

    The module's file is checked against the tree under test rather than trusted:
    an editable install that shadowed the copy would otherwise let this check pass
    on a different tree than the one it claims to judge.
    """
    sys.path.insert(0, str(project_root / "src"))
    try:
        module = importlib.import_module(USE_CASE_RECORD_IMPORT)
    except ImportError as error:
        raise ArchitecturePreflightError(
            f"{USE_CASE_RECORD_MODULE} could not be imported to resolve its "
            f"annotations: {error}"
        ) from error
    origin = Path(module.__file__ or "")
    if origin != (project_root / USE_CASE_RECORD_MODULE).resolve():
        raise ArchitecturePreflightError(
            f"{USE_CASE_RECORD_IMPORT} resolved to {origin}, which is not the "
            f"{USE_CASE_RECORD_MODULE} of the tree under test"
        )
    record = getattr(module, USE_CASE_RECORD_NAME, None)
    if not isinstance(record, type):
        raise ArchitecturePreflightError(
            f"{USE_CASE_RECORD_MODULE} declares no {USE_CASE_RECORD_NAME}; "
            "the routes' use-case record is what this check exists for"
        )
    return record


def _declared_in(annotation: Any) -> Iterator[str]:
    """The module every type inside one annotation was declared in."""
    module = getattr(annotation, "__module__", None)
    if isinstance(module, str):
        yield module
    for argument in typing.get_args(annotation):
        if isinstance(argument, list):
            for element in argument:
                yield from _declared_in(element)
        else:
            yield from _declared_in(argument)


def _owned_by(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def use_case_record_problems(project_root: Path) -> tuple[str, ...]:
    """Every way the use-case record could hand a port back to a route.

    The record is what the routes hold, so a field of it that resolves to a port
    reopens exactly the call the other locks close. The rule is positive and
    therefore fail-closed: a field is a call into this application, or it is
    refused. A field that is not a callable at all, or whose outcome was declared
    anywhere but `atelier2.application`, fails without anyone having to predict the
    spelling it would have used.

    There is no exception list: the record does not predate this rule, so it never
    legitimately holds a port.
    """
    record = _record_under_test(project_root)
    problems = list(_unannotated_fields(project_root))
    try:
        resolved = typing.get_type_hints(record)
    except (NameError, TypeError, AttributeError) as error:
        # An annotation nobody can resolve is refused rather than skipped: what a
        # route would hold cannot be judged, and the safe answer to that is no.
        # Any other failure still ends the run — this check never reports green
        # for a record it could not read.
        return (
            *problems,
            (
                f"{USE_CASE_RECORD_NAME} carries an annotation that resolves to "
                f"nothing, so what a route would hold cannot be judged: {error}"
            ),
        )
    for field, annotation in resolved.items():
        stated = f"{USE_CASE_RECORD_NAME}.{field} is {annotation}"
        if typing.get_origin(annotation) is not abc.Callable:
            problems.append(
                f"{stated}, which is not a call into {APPLICATION_PACKAGE}; every "
                "field of this record is a use-case the composition already bound"
            )
            continue
        declared = tuple(_declared_in(annotation))
        if any(_owned_by(module, PORT_PACKAGE) for module in declared):
            problems.append(f"{stated}, which resolves to {PORT_PACKAGE}")
            continue
        outcome = typing.get_args(annotation)[1]
        if not all(
            _owned_by(module, APPLICATION_PACKAGE) for module in _declared_in(outcome)
        ):
            problems.append(
                f"{stated}, whose outcome was not declared in "
                f"{APPLICATION_PACKAGE}; a route reads this layer's own answer"
            )
    return tuple(problems)


def _unannotated_fields(project_root: Path) -> Iterator[str]:
    """A class-body assignment carrying no annotation is invisible to the resolver.

    `typing.get_type_hints` reports annotated fields only, so an unannotated
    assignment would never reach the rule above. It is refused here rather than
    tolerated, because a field without an annotation is a hole in exactly this
    check.
    """
    module = _parsed(project_root / USE_CASE_RECORD_MODULE)
    for node in ast.walk(module):
        if not isinstance(node, ast.ClassDef) or node.name != USE_CASE_RECORD_NAME:
            continue
        for statement in node.body:
            if isinstance(statement, (ast.Assign, ast.AugAssign)):
                yield (
                    f"{USE_CASE_RECORD_NAME} carries an unannotated assignment; "
                    "a field without an annotation is a hole in this check"
                )


def _reaches_a_port(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == PORTS_RECORD_NAME:
            return True
        if isinstance(child, ast.Attribute) and child.attr == "ports":
            return True
    return False


def _calls_reaching_ports(module: ast.Module) -> frozenset[str]:
    """Which named call of one route module reaches a port, innermost one wins."""
    reaching: set[str] = set()
    definitions = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for definition in definitions:
        nested = {
            id(inner)
            for child in ast.iter_child_nodes(definition)
            for inner in ast.walk(child)
            if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
            and inner is not definition
        }
        own = [
            child
            for child in ast.walk(definition)
            if child is not definition and id(child) not in nested
        ]
        if any(_reaches_a_port(child) for child in own):
            reaching.add(definition.name)
    outside = [
        statement
        for statement in module.body
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if any(_reaches_a_port(statement) for statement in outside):
        reaching.add("<module>")
    return frozenset(reaching)


def route_port_problems(project_root: Path) -> tuple[str, ...]:
    """Which calls still reach a port, read against the declared map."""
    problems: list[str] = []
    for module_path in sorted((project_root / ROUTE_PACKAGE).glob("*.py")):
        name = module_path.stem
        reaching = _calls_reaching_ports(_parsed(module_path))
        declared = ROUTE_CALLS_STILL_HOLDING_PORTS.get(name, frozenset())
        for call in sorted(reaching - declared):
            problems.append(
                f"{ROUTE_PACKAGE}/{name}.py: {call} reaches a port; a route reads "
                "the use-case record the composition bound for it"
            )
        for call in sorted(declared - reaching):
            problems.append(
                f"{ROUTE_PACKAGE}/{name}.py: {call} reaches no port any more; "
                "remove it from ROUTE_CALLS_STILL_HOLDING_PORTS"
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
        f"{sum(len(calls) for calls in ROUTE_CALLS_STILL_HOLDING_PORTS.values())} route calls still holding ports",
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
