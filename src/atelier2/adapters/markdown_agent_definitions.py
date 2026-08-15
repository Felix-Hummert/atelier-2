from __future__ import annotations

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from atelier2.contracts.agent_definitions import (
    REQUIRED_AGENT_DEFINITION_FIELDS,
    AgentDefinition,
    AgentDefinitionField,
    AgentDefinitionRefusal,
    AgentDefinitionRefused,
    AgentToolDeclaration,
    AgentToolName,
    DeclaredTools,
    UnrestrictedTools,
)

FRONTMATTER_DELIMITER = "---"
TOOL_SEPARATOR = ","
_YAML_TEXT_TAG = "tag:yaml.org,2002:str"
# A rendered scalar is never folded, because folding rewrites the spacing the
# author chose and a preview would then show a document nobody wrote.
_UNFOLDED_LINE_WIDTH = 2**31 - 1


def parse_agent_definition(document: bytes) -> AgentDefinition:
    """Read one authored markdown agent definition, or refuse it by name."""

    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AgentDefinitionRefused(
            AgentDefinitionRefusal.DOCUMENT_NOT_UTF8
        ) from error
    frontmatter, system_prompt = _split_frontmatter(text)
    fields = _frontmatter_fields(frontmatter)
    for required in REQUIRED_AGENT_DEFINITION_FIELDS:
        if required.value not in fields:
            raise AgentDefinitionRefused(
                AgentDefinitionRefusal.FIELD_MISSING, required.value
            )
    return AgentDefinition(
        _authored_text(
            fields[AgentDefinitionField.NAME.value], AgentDefinitionField.NAME
        ),
        _authored_text(
            fields[AgentDefinitionField.DESCRIPTION.value],
            AgentDefinitionField.DESCRIPTION,
        ),
        _authored_model(fields),
        _tool_declaration(fields),
        system_prompt,
    )


def render_agent_definition(definition: AgentDefinition) -> bytes:
    """Write the canonical document of one definition.

    Reading this back yields the same definition, so a held definition can be
    shown to the human who authored it without a second, divergent format. The
    canonical spelling is not the only accepted one: an author may separate
    tools by comma as they do today, and this writes the sequence that spelling
    cannot always express.
    """

    frontmatter: dict[str, str | list[str]] = {
        AgentDefinitionField.NAME.value: definition.name,
        AgentDefinitionField.DESCRIPTION.value: definition.description,
    }
    if definition.model is not None:
        frontmatter[AgentDefinitionField.MODEL.value] = definition.model
    if isinstance(definition.tools, DeclaredTools):
        # The sequence spelling is the canonical one because it is the only
        # lossless one: a tool name may itself contain the comma separator, and
        # the comma spelling would read that single name back as two.
        frontmatter[AgentDefinitionField.TOOLS.value] = [
            name.value for name in definition.tools.names
        ]
    document = (
        f"{FRONTMATTER_DELIMITER}\n"
        + yaml.safe_dump(
            frontmatter,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=_UNFOLDED_LINE_WIDTH,
        )
        + f"{FRONTMATTER_DELIMITER}\n"
        + definition.system_prompt
    )
    return document.encode("utf-8")


def _split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0] != f"{FRONTMATTER_DELIMITER}\n":
        raise AgentDefinitionRefused(AgentDefinitionRefusal.FRONTMATTER_MISSING)
    for index, line in enumerate(lines[1:], start=1):
        if line in (f"{FRONTMATTER_DELIMITER}\n", FRONTMATTER_DELIMITER):
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise AgentDefinitionRefused(AgentDefinitionRefusal.FRONTMATTER_UNTERMINATED)


def _frontmatter_fields(frontmatter: str) -> dict[str, Node]:
    try:
        root = yaml.compose(frontmatter, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError) as error:
        # A document nested deeper than the composer can recurse is a document
        # this parser cannot read, and an external file must never reach a
        # caller as a bare interpreter error instead of a named refusal.
        raise AgentDefinitionRefused(
            AgentDefinitionRefusal.FRONTMATTER_UNPARSABLE
        ) from error
    if not isinstance(root, MappingNode):
        raise AgentDefinitionRefused(AgentDefinitionRefusal.FRONTMATTER_NOT_A_MAPPING)
    known = {field.value for field in AgentDefinitionField}
    fields: dict[str, Node] = {}
    for key_node, value_node in root.value:
        if not isinstance(key_node, ScalarNode):
            raise AgentDefinitionRefused(
                AgentDefinitionRefusal.FRONTMATTER_NOT_A_MAPPING
            )
        key = str(key_node.value)
        if key not in known:
            raise AgentDefinitionRefused(AgentDefinitionRefusal.FIELD_UNKNOWN, key)
        if key in fields:
            raise AgentDefinitionRefused(AgentDefinitionRefusal.FIELD_DUPLICATED, key)
        fields[key] = value_node
    return fields


def _authored_text(node: Node, field: AgentDefinitionField) -> str:
    if not isinstance(node, ScalarNode) or node.tag != _YAML_TEXT_TAG:
        raise AgentDefinitionRefused(
            AgentDefinitionRefusal.FIELD_TYPE_UNEXPECTED, field.value
        )
    return str(node.value)


def _authored_model(fields: dict[str, Node]) -> str | None:
    node = fields.get(AgentDefinitionField.MODEL.value)
    if node is None:
        return None
    return _authored_text(node, AgentDefinitionField.MODEL)


def _tool_declaration(fields: dict[str, Node]) -> AgentToolDeclaration:
    node = fields.get(AgentDefinitionField.TOOLS.value)
    if node is None:
        return UnrestrictedTools()
    if isinstance(node, SequenceNode):
        return DeclaredTools(
            tuple(
                AgentToolName(_authored_text(entry, AgentDefinitionField.TOOLS))
                for entry in node.value
            )
        )
    declared = _authored_text(node, AgentDefinitionField.TOOLS).strip()
    if not declared:
        return DeclaredTools(())
    # The separator's surrounding whitespace belongs to the spelling, not to any
    # tool name the author meant.
    return DeclaredTools(
        tuple(AgentToolName(name.strip()) for name in declared.split(TOOL_SEPARATOR))
    )
