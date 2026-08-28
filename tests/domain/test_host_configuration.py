"""The host configuration channel's own records and named refusals."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from atelier2.api.references import (
    decode_public_project_reference,
    encode_public_project_reference,
)
from atelier2.contracts.agents import AgentConfigurationRevisionHash, ProviderId
from atelier2.contracts.host_configuration import (
    MAXIMUM_MODEL_REGISTRY_ENTRIES,
    MAXIMUM_PROJECT_ID_CHARACTERS,
    PROJECT_UNKNOWN,
    ModelRegistryEntry,
    ModelRegistryEntrySource,
    ModelRegistryRevision,
    ProjectId,
    ProjectModelDefault,
    ProjectModelDefaultsRevision,
    ProjectRootRevision,
    ProjectSourceConnectionRevision,
    ProjectUnknown,
    ProviderModelCheck,
)
from atelier2.contracts.workflows_v3 import RoleDifficulty


def test_a_project_id_is_the_exact_characters_it_was_given() -> None:
    assert ProjectId("studio").value == "studio"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("x" * (MAXIMUM_PROJECT_ID_CHARACTERS + 1), id="too long"),
    ],
)
def test_a_bad_project_id_is_refused_as_project_unknown(value: str) -> None:
    with pytest.raises(ProjectUnknown, match=PROJECT_UNKNOWN):
        ProjectId(value)


@pytest.mark.parametrize("value", ["\ud800", "studio\udfff"])
@pytest.mark.proves("a-project-id-is-exact-utf8-before-it-enters-configuration")
def test_a_project_id_that_is_not_unicode_scalar_text_is_refused_before_hashing(
    value: str,
) -> None:
    with pytest.raises(ProjectUnknown, match=PROJECT_UNKNOWN):
        ProjectId(value)


@pytest.mark.proves("a-project-id-is-exact-utf8-before-it-enters-configuration")
def test_the_widest_maximum_project_id_round_trips_every_configuration_boundary(
    tmp_path: Path,
) -> None:
    project_id = ProjectId("\U0010ffff" * MAXIMUM_PROJECT_ID_CHARACTERS)
    revision = ProjectRootRevision(project_id, 1, tmp_path)

    assert revision.project_id == project_id
    assert (
        decode_public_project_reference(encode_public_project_reference(project_id))
        == project_id
    )


def test_the_same_project_root_revision_is_the_same_hash(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    first = ProjectRootRevision(ProjectId("studio"), 1, root)
    second = ProjectRootRevision(ProjectId("studio"), 1, root)

    assert first.revision_hash == second.revision_hash
    assert first.root_path == root.resolve()


def test_every_project_source_revision_must_explicitly_name_its_lifecycle_and_time() -> (
    None
):
    parameters = inspect.signature(ProjectSourceConnectionRevision).parameters

    assert parameters["lifecycle"].default is inspect.Parameter.empty
    assert parameters["connected_at"].default is inspect.Parameter.empty
    assert parameters["source_ref"].default is inspect.Parameter.empty


def test_a_later_revision_or_another_project_is_a_different_hash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    first = ProjectRootRevision(ProjectId("studio"), 1, root)
    later = ProjectRootRevision(ProjectId("studio"), 2, root)
    other = ProjectRootRevision(ProjectId("other"), 1, root)

    assert first.revision_hash != later.revision_hash
    assert first.revision_hash != other.revision_hash


def _registry_entry(
    model_id: str,
    configuration_hash: str,
    source: ModelRegistryEntrySource = ModelRegistryEntrySource.OPERATOR,
    provider_check: ProviderModelCheck = ProviderModelCheck.CHECKED,
) -> ModelRegistryEntry:
    return ModelRegistryEntry(
        model_id,
        AgentConfigurationRevisionHash(configuration_hash),
        source,
        provider_check,
    )


def _registry(
    *entries: ModelRegistryEntry,
    provider: str = "anthropic",
    revision_number: int = 1,
) -> ModelRegistryRevision:
    return ModelRegistryRevision(
        ProviderId(provider),
        revision_number,
        entries,
    )


def test_model_registry_entries_are_exact_and_canonical_by_model_id() -> None:
    opus = _registry_entry("claude-opus-5", "cd" * 32)
    sonnet = _registry_entry(
        "claude-sonnet-4-6", "ef" * 32, ModelRegistryEntrySource.DISCOVERED
    )

    first = _registry(opus, sonnet)
    second = _registry(sonnet, opus)

    assert first.revision_hash == second.revision_hash
    assert first.entries == (opus, sonnet)


def test_provider_check_is_part_of_the_immutable_registry_fact() -> None:
    unchecked = _registry(
        _registry_entry(
            "claude-opus-5",
            "cd" * 32,
            provider_check=ProviderModelCheck.NOT_CHECKED,
        )
    )
    checked = _registry(
        _registry_entry(
            "claude-opus-5",
            "cd" * 32,
            provider_check=ProviderModelCheck.CHECKED,
        )
    )

    assert unchecked.entries[0].provider_check is ProviderModelCheck.NOT_CHECKED
    assert checked.entries[0].provider_check is ProviderModelCheck.CHECKED
    assert unchecked.revision_hash != checked.revision_hash


@pytest.mark.parametrize("model_id", ["", "newest opus", "\tmodel"])
def test_a_registry_refuses_a_non_exact_model_id(model_id: str) -> None:
    with pytest.raises(ValueError, match="model id"):
        _registry_entry(model_id, "cd" * 32)


def test_duplicate_model_ids_in_one_provider_revision_are_refused() -> None:
    with pytest.raises(ValueError, match="unique"):
        _registry(
            _registry_entry("claude-opus-5", "cd" * 32),
            _registry_entry("claude-opus-5", "ef" * 32),
        )


def test_registry_entries_are_bounded_before_they_are_hashed() -> None:
    allowed = tuple(
        _registry_entry(f"model-{index}", f"{index:064x}")
        for index in range(MAXIMUM_MODEL_REGISTRY_ENTRIES)
    )

    assert len(_registry(*allowed).entries) == MAXIMUM_MODEL_REGISTRY_ENTRIES
    with pytest.raises(ValueError, match="at most"):
        _registry(*allowed, _registry_entry("one-too-many", "f" * 64))


def _defaults(
    *defaults: ProjectModelDefault,
    project: str = "studio",
    revision_number: int = 1,
) -> ProjectModelDefaultsRevision:
    return ProjectModelDefaultsRevision(ProjectId(project), revision_number, defaults)


def _default(
    difficulty: RoleDifficulty,
    registry: ModelRegistryRevision,
    entry: ModelRegistryEntry,
) -> ProjectModelDefault:
    return ProjectModelDefault(
        difficulty,
        registry.revision_hash,
        registry.provider_id,
        entry.model_id,
        entry.agent_configuration_revision_hash,
    )


def test_project_defaults_are_three_operator_chosen_registry_references() -> None:
    easy = _registry_entry("claude-haiku-4-5", "11" * 32)
    standard = _registry_entry("claude-sonnet-4-6", "22" * 32)
    hard = _registry_entry("claude-opus-5", "33" * 32)
    registry = _registry(easy, standard, hard)
    defaults = (
        _default(3, registry, hard),
        _default(1, registry, easy),
        _default(2, registry, standard),
    )

    first = _defaults(*defaults)
    second = _defaults(*reversed(defaults))

    assert first.revision_hash == second.revision_hash
    assert tuple(item.difficulty for item in first.defaults) == (
        1,
        2,
        3,
    )


def test_a_project_default_difficulty_is_unique() -> None:
    entry = _registry_entry("claude-opus-5", "33" * 32)
    registry = _registry(entry)

    with pytest.raises(ValueError, match="unique"):
        _defaults(
            _default(2, registry, entry),
            _default(2, registry, entry),
        )
