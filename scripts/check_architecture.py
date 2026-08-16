from __future__ import annotations

import ast
import importlib
import sys
import typing
from collections import abc
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from importlinter.api import read_configuration
from importlinter.cli import lint_imports

EXPECTED_SOURCE_MODULE_FLOOR = 109
EXPECTED_CONTRACT_NAMES = {
    "layers": "Atelier package layers",
    "root-facade": "Root facade cannot bypass ports",
    "dbos-owner": "DBOS and SQLAlchemy stay inside their adapter",
    "wire-projection-split": "Wire schemas name no port type",
    "route-vocabulary": "Routes name no port type",
    "schema-owner": "JSON Schema evaluation stays inside one profile owner",
}
ROOT_PACKAGE = "atelier2"
PORT_PACKAGE = "atelier2.ports"
APPLICATION_PACKAGE = "atelier2.application"
USE_CASE_RECORD_IMPORT = "atelier2.api.context"
USE_CASE_RECORD_MODULE = "src/atelier2/api/context.py"
USE_CASE_RECORD_NAME = "ApiUseCases"
PORTS_RECORD_NAME = "ApiPorts"


class _UnresolvedOutcome:
    """An outcome whose fields could not be read, so nothing about it is proven."""


_UNRESOLVED_OUTCOME = _UnresolvedOutcome()


PORT_PACKAGE_DIRECTORY = "src/atelier2/ports"
HTTP_SENTENCE_MARKERS = ("API limits", "HTTP", "status code")
ROUTE_PACKAGE = "src/atelier2/api/routes"
ROUTE_CALLS_STILL_HOLDING_PORTS = {
    "runs": {"cancel_agent_attempt_route": ("agent_attempt_canceller",)},
}
"""Every port a route still reaches, named down to the single access.

The unit of the exception is the unit of the work, and the work is one access at a
time. A module is not translated at once, and neither is a call: an allowlisted
call that grows a
*second* port read has taken back a decision it had already given up. So the
declaration names which ports each call reaches, and the check compares the whole
list rather than asking whether the call reaches one at all.

Read in both directions: an access this map does not name is red, and an access it
names that no longer happens is red too. A stale entry is a failure rather than a
comfortable lie. It shrinks to empty, and with the last entry it deletes itself.

One entry is left, and it is left for a reason that is not scope: rewriting
`cancel_agent_attempt` means rewriting the module a live change (#70) is already
rewriting, and two heads editing one file is how a rewrite loses. It is the last
pass-through use-case in the tree — it hands its port union straight to a route —
so the day #70 lands, this entry and this map go together.
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


def _is_a_port_capability(candidate: Any) -> bool:
    """Whether this type is a store a holder could call, rather than data it read.

    The two live side by side under `atelier2.ports`, and only one of them is the
    danger. `RunQueries` is a protocol: whoever holds it can ask the store
    anything. `RunProjection` is a frozen record the store already answered with —
    a route is *supposed* to hold that, because rendering it is the route's job.

    So the discriminator is the protocol, not the package. This is the reading the
    sentence always had — a port is a capability — and not a narrowing to make a
    finding go away: the mutation this rule exists for hands over `RunQueries`.
    That two kinds of thing share one package is a real smell, reported to #87
    rather than fixed here.
    """
    return getattr(candidate, "_is_protocol", False) and _owned_by(
        getattr(candidate, "__module__", ""), PORT_PACKAGE
    )


def _readable_fields(outcome: type) -> dict[str, Any] | None:
    """This type's annotated fields, or `None` when nothing can resolve them.

    A generic declared with PEP 695 type parameters carries them in a lexical
    scope rather than in its module, so a subclass's annotations name something
    `get_type_hints` cannot see from the outside. The parameters are therefore
    offered back from the whole inheritance chain, bound to what they were bound
    to — which is what the annotation meant in the first place.
    """
    parameters: dict[str, Any] = {}
    for base in getattr(outcome, "__mro__", ()):
        for parameter in getattr(base, "__type_params__", ()) or ():
            parameters[parameter.__name__] = (
                getattr(parameter, "__bound__", None) or object
            )
    try:
        return typing.get_type_hints(outcome, localns=parameters)
    except (NameError, TypeError, AttributeError):
        return None


def _carried_by(outcome: Any, seen: set[int]) -> Iterator[Any]:
    """Every type a value of this outcome could carry, however deep it sits.

    Naming an outcome of this application is not enough: the application layer may
    read the ports, so an outcome is free to carry one as a payload and hand it on
    unread by any rule that stops at the outer type. The closure is walked instead
    — union members, generic arguments and the annotated fields of every type
    reached — so a port inside the answer is a port the route was handed.
    """
    if id(outcome) in seen:
        return
    seen.add(id(outcome))
    yield outcome
    for argument in typing.get_args(outcome):
        if isinstance(argument, list):
            for element in argument:
                yield from _carried_by(element, seen)
        else:
            yield from _carried_by(argument, seen)
    value = getattr(outcome, "__value__", None)
    if value is not None:
        yield from _carried_by(value, seen)
    if isinstance(outcome, type) and _owned_by(
        getattr(outcome, "__module__", ""), ROOT_PACKAGE
    ):
        fields = _readable_fields(outcome)
        if fields is None:
            # A type whose own fields cannot be read cannot be shown to be free of
            # ports, and the safe answer to that is no. It is refused wherever it
            # lives: an outcome nobody can read is exactly where one would hide.
            yield _UNRESOLVED_OUTCOME
            return
        for field in fields.values():
            yield from _carried_by(field, seen)


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
            continue
        carried = tuple(_carried_by(outcome, set()))
        capabilities = [
            carrier for carrier in carried if _is_a_port_capability(carrier)
        ]
        if capabilities:
            problems.append(
                f"{stated}, whose outcome carries {capabilities[0]} inside it; an "
                "answer that hands a port on is the port the route was handed"
            )
        elif any(carrier is _UNRESOLVED_OUTCOME for carrier in carried):
            problems.append(
                f"{stated}, whose outcome carries a type this check could not "
                "read, so it cannot be shown to be free of ports"
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


PORTS_RECORD_FIELD = "ports"


def _binds_the_ports_record(node: ast.AST) -> bool:
    """Whether this expression hands over the whole record of ports."""
    return isinstance(node, ast.Attribute) and node.attr == PORTS_RECORD_FIELD


def _port_reached_at(node: ast.AST, aliases: frozenset[str]) -> str | None:
    """The port this one node reaches, if it is the access itself.

    One node, never its subtree: an access nested inside another expression must
    count once, not once per level it sits under.

    An access is recognised by what it reaches, not by how it is spelled. Reading
    `context.ports.run_queries` and binding `ports = context.ports` to read
    `ports.run_queries` off the name are the same reach, so the names a function
    binds the record to are resolved first and counted the same way. Otherwise the
    check measures a spelling, and a spelling is exactly what a caller can change
    without changing what it holds.
    """
    if isinstance(node, ast.Name) and node.id == PORTS_RECORD_NAME:
        return PORTS_RECORD_NAME
    if isinstance(node, ast.Attribute):
        if _binds_the_ports_record(node):
            return PORTS_RECORD_FIELD
        if isinstance(node.value, ast.Attribute) and _binds_the_ports_record(
            node.value
        ):
            return node.attr
        if isinstance(node.value, ast.Name) and node.value.id in aliases:
            return node.attr
    return None


def _ports_record_aliases(nodes: Iterable[ast.AST]) -> frozenset[str]:
    """Every local name these nodes bind the whole record of ports to."""
    bound: set[str] = set()
    for node in nodes:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None or not any(
            _binds_the_ports_record(inner) for inner in ast.walk(value)
        ):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        bound.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(bound)


def _ports_reached_in(nodes: Iterable[ast.AST]) -> tuple[str, ...]:
    """Every port these nodes reach, one entry per access.

    A repeated access is a repeated entry: two reads of the same port are two
    decisions, and collapsing them would let one hide behind the other.
    """
    own = list(nodes)
    aliases = _ports_record_aliases(own)
    # `context.ports.run_queries` is one access, not two: the record it reads
    # through is part of the same expression, so only the outermost node counts.
    consumed = {
        id(node.value)
        for node in own
        if isinstance(node, ast.Attribute) and _binds_the_ports_record(node.value)
    }
    return tuple(
        sorted(
            reached
            for node in own
            if id(node) not in consumed
            and (reached := _port_reached_at(node, aliases)) is not None
        )
    )


def _calls_reaching_ports(module: ast.Module) -> dict[str, tuple[str, ...]]:
    """Which ports each named call of one route module reaches, innermost one wins."""
    reaching: dict[str, tuple[str, ...]] = {}
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
        reached = _ports_reached_in(own)
        if reached:
            reaching[definition.name] = reached
    outside = [
        statement
        for statement in module.body
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    outer = _ports_reached_in(
        child for statement in outside for child in ast.walk(statement)
    )
    if outer:
        reaching["<module>"] = outer
    return reaching


def port_sentence_problems(project_root: Path) -> tuple[str, ...]:
    """Ports that mention one of HTTP_SENTENCE_MARKERS: API limits, HTTP, status code.

    The check is that three-marker heuristic, not every sentence a port could
    write. A plain-English refusal without those markers is outside this gate.
    A port still must not explain a decision it does not make; review catches
    what the markers miss.
    """
    problems: list[str] = []
    for module_path in sorted((project_root / PORT_PACKAGE_DIRECTORY).rglob("*.py")):
        module = _parsed(module_path)
        for node in ast.walk(module):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if any(marker in node.value for marker in HTTP_SENTENCE_MARKERS):
                problems.append(
                    f"{PORT_PACKAGE_DIRECTORY}/{module_path.name} words an answer "
                    f"for the layer above it: {node.value!r}"
                )
    return tuple(problems)


def route_port_problems(project_root: Path) -> tuple[str, ...]:
    """Which calls still reach a port, read against the declared map."""
    problems: list[str] = []
    for module_path in sorted((project_root / ROUTE_PACKAGE).glob("*.py")):
        name = module_path.stem
        reaching = _calls_reaching_ports(_parsed(module_path))
        declared = ROUTE_CALLS_STILL_HOLDING_PORTS.get(name, {})
        for call in sorted(set(reaching) | set(declared)):
            reached = reaching.get(call, ())
            allowed = tuple(sorted(declared.get(call, ())))
            if reached == allowed:
                continue
            problems.append(
                f"{ROUTE_PACKAGE}/{name}.py: {call} reaches {reached or 'no port'}, "
                f"and this head declares {allowed or 'none'}; a route reads the "
                "use-case record the composition bound for it"
            )
    return tuple(problems)


def architecture_preflight(project_root: Path) -> ArchitectureConfiguration:
    source_count = source_module_count(project_root / "src/atelier2")
    if source_count < EXPECTED_SOURCE_MODULE_FLOOR:
        raise ArchitecturePreflightError(
            f"found {source_count} source modules; expected at least {EXPECTED_SOURCE_MODULE_FLOOR}"
        )
    problems = (
        port_sentence_problems(project_root)
        + use_case_record_problems(project_root)
        + route_port_problems(project_root)
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
        f"{sum(len(ports) for calls in ROUTE_CALLS_STILL_HOLDING_PORTS.values() for ports in calls.values())} route port reaches still declared",
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
