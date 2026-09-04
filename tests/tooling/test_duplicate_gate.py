"""The duplicate ratchet: copied code is red unless the baseline already names it.

The check reads a source tree and a baseline file and answers with the problems
it found, so it is driven here over scratch trees of a few functions rather than
over the real package -- the sentence under test is what counts as a copy, and a
real tree would only ever restate today's baseline.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
SOURCE_PACKAGE = Path("src") / "atelier2"


def load_architecture_script() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "check_architecture", PROJECT_ROOT / "scripts/check_architecture.py"
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def a_summing_function(
    name: str = "summed", counter: str = "total", refusal: str = "negative"
) -> str:
    """A function long enough to be recognised again, spelled as asked."""
    return f"""
def {name}(values: list[int]) -> int:
    {counter} = 0
    for value in values:
        if value < 0:
            raise ValueError("{refusal}")
        {counter} = {counter} + value * 2
    return {counter}
"""


A_SHORT_FUNCTION = """
def doubled(value: int) -> int:
    return value * 2
"""
A_DIFFERENT_FUNCTION = """
def averaged(values: list[float]) -> float:
    if not values:
        raise ValueError("no values")
    total = 0.0
    for value in values:
        total = total + value
    return total / len(values)
"""


def scratch_project(
    tmp_path: Path, modules: dict[str, str], baseline: str = ""
) -> Path:
    project = tmp_path / "project"
    (project / SOURCE_PACKAGE).mkdir(parents=True)
    for module, source in modules.items():
        (project / SOURCE_PACKAGE / f"{module}.py").write_text(source, encoding="utf-8")
    (project / "duplicate_baseline.toml").write_text(baseline, encoding="utf-8")
    return project


def a_baseline_of(*pairs: tuple[str, str]) -> str:
    return "\n".join(
        f'[[pair]]\nleft = "{left}"\nright = "{right}"\n' for left, right in pairs
    )


def problems_of(project: Path) -> tuple[str, ...]:
    return load_architecture_script().duplicate_problems(project)


def test_a_copy_the_baseline_does_not_name_is_refused_with_both_locations(
    tmp_path: Path,
) -> None:
    project = scratch_project(
        tmp_path, {"first": a_summing_function(), "second": a_summing_function()}
    )

    problems = problems_of(project)

    assert len(problems) == 1
    assert "atelier2.first.summed" in problems[0]
    assert "atelier2.second.summed" in problems[0]
    assert str(SOURCE_PACKAGE / "first.py") in problems[0]
    assert str(SOURCE_PACKAGE / "second.py") in problems[0]


def test_a_copy_the_baseline_names_keeps_the_gate_quiet(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {"first": a_summing_function(), "second": a_summing_function()},
        baseline=a_baseline_of(("atelier2.first.summed", "atelier2.second.summed")),
    )

    assert problems_of(project) == ()


def test_a_baseline_entry_written_the_other_way_round_names_the_same_pair(
    tmp_path: Path,
) -> None:
    project = scratch_project(
        tmp_path,
        {"first": a_summing_function(), "second": a_summing_function()},
        baseline=a_baseline_of(("atelier2.second.summed", "atelier2.first.summed")),
    )

    assert problems_of(project) == ()


def test_a_baseline_entry_whose_copy_is_gone_is_refused(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {"first": a_summing_function(), "second": A_DIFFERENT_FUNCTION},
        baseline=a_baseline_of(("atelier2.first.summed", "atelier2.second.averaged")),
    )

    problems = problems_of(project)

    assert len(problems) == 1
    assert "orphan baseline entry, remove it" in problems[0]
    assert "atelier2.second.averaged" in problems[0]


@pytest.mark.parametrize(
    "second_module",
    [
        pytest.param(
            a_summing_function(name="added", counter="carried", refusal="below zero"),
            id="renamed, with other locals and other messages",
        ),
        pytest.param(a_summing_function(), id="copied verbatim"),
    ],
)
def test_a_copy_stays_a_copy_however_its_own_names_are_spelled(
    tmp_path: Path, second_module: str
) -> None:
    project = scratch_project(
        tmp_path, {"first": a_summing_function(), "second": second_module}
    )

    assert len(problems_of(project)) == 1


@pytest.mark.parametrize(
    ("modules", "why"),
    [
        pytest.param(
            {"first": a_summing_function(), "second": A_DIFFERENT_FUNCTION},
            "two functions that do different work",
            id="different work",
        ),
        pytest.param(
            {"first": A_SHORT_FUNCTION, "second": A_SHORT_FUNCTION},
            "two functions too short to recognise again",
            id="below the minimum length",
        ),
    ],
)
def test_what_is_not_a_copy_keeps_the_gate_quiet(
    tmp_path: Path, modules: dict[str, str], why: str
) -> None:
    project = scratch_project(tmp_path, modules)

    assert problems_of(project) == (), why


def test_a_baseline_entry_missing_a_name_is_refused(tmp_path: Path) -> None:
    script = load_architecture_script()
    project = scratch_project(
        tmp_path,
        {"first": a_summing_function()},
        baseline='[[pair]]\nleft = "atelier2.first.summed"\n',
    )

    with pytest.raises(script.ArchitecturePreflightError):
        script.duplicate_problems(project)


def test_two_definitions_sharing_a_qualified_name_are_refused(tmp_path: Path) -> None:
    script = load_architecture_script()
    project = scratch_project(
        tmp_path, {"first": a_summing_function() + a_summing_function()}
    )

    with pytest.raises(script.ArchitecturePreflightError):
        script.duplicate_problems(project)


def test_the_duplicate_check_runs_as_part_of_the_architecture_gate() -> None:
    script = load_architecture_script()

    registered = dict(script.ARCHITECTURE_PREFLIGHTS)

    assert registered["duplicate-problems"] is script.duplicate_problems
