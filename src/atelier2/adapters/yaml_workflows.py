from __future__ import annotations

from typing import Any

import yaml
from pydantic import ValidationError
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode
from yaml.tokens import (
    AliasToken,
    AnchorToken,
    DocumentStartToken,
    StreamStartToken,
    TagToken,
)

from atelier2.contracts.workflow_refusals import (
    WorkflowDocumentRefused,
    WorkflowRefusal,
)
from atelier2.contracts.workflows import (
    AnyWorkflowGraph,
    WorkflowGraph,
    WorkflowGraphV2,
)
from atelier2.contracts.workflows_v3 import (
    AnyWorkflowDocument,
    WorkflowGraphV3,
    validate_workflow_graph_v3,
)

FORMAT_V3_NOT_EXECUTABLE = (
    "workflow format version 3 parses, but no runtime executes it yet"
)


class InvalidWorkflowDocument(ValueError):
    """The exact workflow bytes are not one safe, closed YAML graph."""

    def __init__(self, detail: str, refusal: WorkflowRefusal | None = None) -> None:
        super().__init__(detail)
        self.refusal = refusal


class StrictWorkflowLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise ComposerError(
                None, None, "aliases are forbidden", self.peek_event().start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise ConstructorError(None, None, "expected a mapping", node.start_mark)
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge" or key_node.value == "<<":
                raise ConstructorError(
                    None, None, "merge keys are forbidden", key_node.start_mark
                )
            key = self.construct_object(key_node, deep=False)
            try:
                duplicate = key in seen
            except TypeError as error:
                raise ConstructorError(
                    None, None, "mapping keys must be scalar", key_node.start_mark
                ) from error
            if duplicate:
                raise ConstructorError(
                    None, None, "duplicate mapping key", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_workflow_document(document: bytes) -> AnyWorkflowDocument:
    if not document or document.startswith(b"\xef\xbb\xbf"):
        raise InvalidWorkflowDocument("workflow must be nonempty UTF-8 without BOM")
    try:
        decoded = document.decode("utf-8", errors="strict")
        tokens = tuple(yaml.scan(decoded))
        if any(
            isinstance(token, (AliasToken, AnchorToken, TagToken)) for token in tokens
        ):
            raise InvalidWorkflowDocument(
                "anchors, aliases, and explicit tags are forbidden"
            )
        if sum(isinstance(token, DocumentStartToken) for token in tokens) > 1:
            raise InvalidWorkflowDocument("multiple YAML documents are forbidden")
        if not tokens or not isinstance(tokens[0], StreamStartToken):
            raise InvalidWorkflowDocument("workflow is not a YAML stream")
        loaded = yaml.load(decoded, Loader=StrictWorkflowLoader)
        if loaded is None:
            raise InvalidWorkflowDocument("workflow document is empty")
        if isinstance(loaded, dict) and isinstance(loaded.get("nodes"), list):
            nodes = loaded["nodes"]
            for node in nodes:
                if isinstance(node, dict) and isinstance(node.get("operands"), list):
                    node["operands"] = tuple(node["operands"])
            loaded["nodes"] = tuple(nodes)
        if (
            not isinstance(loaded, dict)
            or type(loaded.get("format_version")) is not int
        ):
            raise InvalidWorkflowDocument(
                "workflow format version must be a strict integer"
            )
        version = loaded["format_version"]
        if version == 1:
            return WorkflowGraph.model_validate(loaded, strict=True)
        if version == 2:
            return WorkflowGraphV2.model_validate(loaded, strict=True)
        if version == 3:
            return validate_workflow_graph_v3(loaded)
        raise InvalidWorkflowDocument("workflow format version is unsupported")
    except InvalidWorkflowDocument:
        raise
    except WorkflowDocumentRefused as refused:
        raise InvalidWorkflowDocument(str(refused), refused.refusal) from refused
    except (
        UnicodeDecodeError,
        yaml.YAMLError,
        ValidationError,
        TypeError,
        ValueError,
    ) as error:
        raise InvalidWorkflowDocument(
            "workflow document violates safe YAML v1"
        ) from error


def parse_executable_workflow_document(document: bytes) -> AnyWorkflowGraph:
    """Parse one document the current runtime can execute, refusing later formats."""
    graph = parse_workflow_document(document)
    if isinstance(graph, WorkflowGraphV3):
        raise InvalidWorkflowDocument(FORMAT_V3_NOT_EXECUTABLE)
    return graph
