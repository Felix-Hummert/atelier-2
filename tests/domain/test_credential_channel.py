from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

from pydantic import BaseModel

from atelier2.adapters.dbos import schema
from atelier2.api import wire
from atelier2.api.wire.resources import ApiModel

FORBIDDEN_CHANNEL_NAMES = frozenset(
    {
        "secret",
        "secret_value",
        "credential",
        "credentials",
        "credential_handle",
        "credential_ref",
        "credential_path",
        "api_key_value",
        "key_name",
        "lookup_key",
        "private_credential_registry",
    }
)
"""The names a credential channel would carry if one were ever opened.

The subscription credential is a directory the operator hands the process at
launch. Nothing about it may reach a caller or the database, so no field, no
column and no serialized key may be named for one.
"""

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "atelier2"


def test_no_public_resource_field_or_alias_names_a_credential_channel() -> None:
    published = {
        name.casefold()
        for model in _api_resources()
        for name in _serialized_names(model)
    }

    assert published
    assert published.isdisjoint(FORBIDDEN_CHANNEL_NAMES)


def test_no_durable_column_names_a_credential_channel() -> None:
    columns = {
        column.name.casefold()
        for table in schema.metadata.tables.values()
        for column in table.columns
    }

    assert columns
    assert columns.isdisjoint(FORBIDDEN_CHANNEL_NAMES)


def test_no_source_name_or_literal_key_names_a_credential_channel() -> None:
    """Lint every source name and exact string literal, and claim only that.

    The two canaries above read the representations that actually leave this
    process: the serialized field names of the public resources and the columns
    of the durable schema. They cannot see a channel that exists but is not yet
    wired into either -- a helper, a constant, an untyped payload key -- so this
    is the wider, weaker net: it catches a credential channel by the name it is
    given, in any source file, whether that name is an identifier or a literal
    dictionary key. It proves no representation; it proves nobody has written
    one of these names down.
    """

    scanned = sorted(SOURCE_ROOT.rglob("*.py"))

    assert scanned
    for source_path in scanned:
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), source_path.as_posix()
        )
        assert _written_names(tree).isdisjoint(FORBIDDEN_CHANNEL_NAMES), source_path


def _api_resources() -> tuple[type[BaseModel], ...]:
    """Every schema the wire package publishes, however its modules are cut.

    Read from the package rather than from a listed set of modules, so a wire
    module added later joins this canary instead of silently escaping it.
    """
    found: dict[type[BaseModel], None] = {}
    for module in pkgutil.iter_modules(wire.__path__):
        published = importlib.import_module(f"{wire.__name__}.{module.name}")
        found.update(
            dict.fromkeys(
                value
                for value in vars(published).values()
                if isinstance(value, type)
                and issubclass(value, ApiModel)
                and value is not ApiModel
            )
        )
    return tuple(found)


def _serialized_names(model: type[BaseModel]) -> set[str]:
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        names.update(
            alias
            for alias in (field.alias, field.serialization_alias)
            if isinstance(alias, str)
        )
    return names


def _written_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name.casefold())
        elif isinstance(node, ast.Name):
            names.add(node.id.casefold())
        elif isinstance(node, ast.arg):
            names.add(node.arg.casefold())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.casefold())
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value.casefold())
    return names
