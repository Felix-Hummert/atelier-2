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

from atelier2.contracts.workflows import WorkflowGraph


class InvalidWorkflowDocument(ValueError):
    """The exact workflow bytes are not one safe, closed YAML-v1 graph."""


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


def parse_workflow_document(document: bytes) -> WorkflowGraph:
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
        return WorkflowGraph.model_validate(loaded, strict=True)
    except InvalidWorkflowDocument:
        raise
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
