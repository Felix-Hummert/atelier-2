from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
CONTRACT_VIEW_START = "<!-- architecture-contract-view:start -->"
CONTRACT_VIEW_END = "<!-- architecture-contract-view:end -->"


def copied_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", project / "pyproject.toml")
    shutil.copytree(PROJECT_ROOT / "src", project / "src")
    (project / "scripts").mkdir()
    shutil.copy2(
        PROJECT_ROOT / "scripts" / "check_architecture.py",
        project / "scripts" / "check_architecture.py",
    )
    return project


def run_gate(
    project: Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/check_architecture.py"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def add_contract_to_host_import(project: Path) -> None:
    (project / "src/atelier2/contracts/violation.py").write_text(
        "from atelier2.host import main\n",
        encoding="utf-8",
    )


def add_root_to_application_package_import(project: Path) -> None:
    with (project / "src/atelier2/__init__.py").open("a", encoding="utf-8") as package:
        package.write("import atelier2.application\n")


def add_root_to_application_leaf_import(project: Path) -> None:
    with (project / "src/atelier2/__init__.py").open("a", encoding="utf-8") as package:
        package.write(
            "from atelier2.application.answer_wait import answer_wait_result\n"
        )


def add_api_to_dbos_import(project: Path) -> None:
    (project / "src/atelier2/api/violation.py").write_text(
        "from dbos import DBOS\n",
        encoding="utf-8",
    )


def add_empty_rogue_package(project: Path) -> None:
    rogue = project / "src/atelier2/rogue"
    rogue.mkdir()
    (rogue / "__init__.py").touch()


def add_wire_to_port_import(project: Path) -> None:
    (project / "src/atelier2/api/wire/violation.py").write_text(
        "from atelier2.ports.run_queries import RunProjection\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "violation",
    [
        add_contract_to_host_import,
        add_root_to_application_package_import,
        add_root_to_application_leaf_import,
        add_api_to_dbos_import,
        add_empty_rogue_package,
    ],
    ids=[
        "contracts-to-host",
        "root-to-application-package",
        "root-to-application-deep-leaf",
        "api-to-dbos",
        "empty-rogue",
    ],
)
def test_forbidden_inward_and_dbos_owner_imports_fail(
    tmp_path: Path, violation: Callable[[Path], None]
) -> None:
    project = copied_project(tmp_path)
    violation(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert one_broken_contract() in result.stdout


@pytest.mark.proves("wire-schemas-name-no-port-type")
def test_a_wire_schema_module_that_names_a_port_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    add_wire_to_port_import(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert one_broken_contract() in result.stdout


def test_green_gate_reports_positive_source_contract_layer_and_native_graph_counts(
    tmp_path: Path,
) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    preflight_counts = re.search(
        r"Architecture preflight: (\d+) source modules, (\d+) contracts, (\d+) layer members",
        result.stdout,
    )
    assert preflight_counts is not None
    source_count, contract_count, layer_count = map(int, preflight_counts.groups())
    script = load_architecture_script()
    assert source_count == script.source_module_count(PROJECT_ROOT / "src/atelier2")
    assert source_count >= script.EXPECTED_SOURCE_MODULE_FLOOR
    assert (contract_count, layer_count) == (len(script.EXPECTED_CONTRACT_NAMES), 7)

    native_counts = re.search(
        r"Analyzed (\d+) files, (\d+) dependencies\.", result.stdout
    )
    assert native_counts is not None
    assert all(count > 0 for count in map(int, native_counts.groups()))
    reviewed = len(script.EXPECTED_CONTRACT_NAMES)
    assert f"Contracts: {reviewed} kept, 0 broken." in result.stdout


def empty_source_scan(project: Path) -> None:
    for source in (project / "src/atelier2").rglob("*.py"):
        source.unlink()


def shrink_source_scan(project: Path) -> None:
    (project / "src/atelier2/contracts/hashing.py").unlink()


def remove_contract(project: Path) -> None:
    configuration = project / "pyproject.toml"
    text = configuration.read_text(encoding="utf-8")
    start = text.index('id = "dbos-owner"')
    block_start = text.rfind("[[tool.importlinter.contracts]]", 0, start)
    configuration.write_text(text[:block_start], encoding="utf-8")


def change_layer(project: Path) -> None:
    configuration = project / "pyproject.toml"
    text = configuration.read_text(encoding="utf-8")
    configuration.write_text(
        text.replace('"application",\n    "ports",', '"ports",\n    "application",'),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "change",
    [empty_source_scan, shrink_source_scan, remove_contract, change_layer],
    ids=["empty", "below-floor", "missing-contract", "changed-layer"],
)
def test_empty_shrunken_or_changed_contract_scan_fails(
    tmp_path: Path, change: Callable[[Path], None]
) -> None:
    project = copied_project(tmp_path)
    change(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Architecture preflight refused:" in result.stderr


def one_broken_contract() -> str:
    """What the gate prints when exactly one reviewed contract is broken.

    Derived from the reviewed set rather than spelled out, so adding a contract
    cannot leave these assertions quietly describing an older gate.
    """
    reviewed = len(load_architecture_script().EXPECTED_CONTRACT_NAMES)
    return f"Contracts: {reviewed - 1} kept, 1 broken."


def load_architecture_script() -> ModuleType:
    script_path = PROJECT_ROOT / "scripts/check_architecture.py"
    specification = importlib.util.spec_from_file_location(
        "check_architecture", script_path
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_architecture_decision_and_executable_contract_share_the_exact_layers_and_owners(
    tmp_path: Path,
) -> None:
    result = run_gate(copied_project(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    decision = (
        PROJECT_ROOT / "docs/decisions/0005-enforced-package-boundaries.md"
    ).read_text(encoding="utf-8")
    documented_view = decision.split(CONTRACT_VIEW_START, 1)[1].split(
        CONTRACT_VIEW_END, 1
    )[0]
    script = load_architecture_script()

    expected_view = script.render_contract_view(
        script.read_architecture_configuration(PROJECT_ROOT / "pyproject.toml")
    )

    assert documented_view.strip() == expected_view.strip()


def test_gate_runs_with_minimal_environment_and_no_network_or_service(
    tmp_path: Path,
) -> None:
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONIOENCODING": "utf-8",
    }

    result = run_gate(copied_project(tmp_path), environment)

    assert result.returncode == 0, result.stdout + result.stderr


def replace_use_case_field(project: Path, field: str, imported: str = "") -> None:
    """Add one field to the record, with the import a real evasion would need.

    The field is appended after the record's own fields, because a field carrying
    a default in front of them is a dataclass error and would end the run before
    the rule under test is ever reached. Writing the import too is what makes
    these cases the evasions they claim to be rather than typos.
    """
    context = project / "src/atelier2/api/context.py"
    source = context.read_text(encoding="utf-8")
    marker = "class ApiUseCases:\n"
    assert source.count(marker) == 1
    if imported:
        source = source.replace(
            "from atelier2.api.limits import ApiLimits\n",
            f"{imported}\nfrom atelier2.api.limits import ApiLimits\n",
            1,
        )
    body_start = source.index(marker) + len(marker)
    body_end = source.index("\n\n\n", body_start)
    context.write_text(
        source[:body_end] + f"\n    {field}" + source[body_end:],
        encoding="utf-8",
    )


def rename_the_use_case_record(project: Path) -> None:
    context = project / "src/atelier2/api/context.py"
    context.write_text(
        context.read_text(encoding="utf-8").replace("ApiUseCases", "ApiCalls"),
        encoding="utf-8",
    )


def add_route_reaching_a_port(project: Path) -> None:
    (project / "src/atelier2/api/routes/health.py").write_text(
        "from atelier2.api.context import ApiPorts\n\n\n"
        "def leak(ports: ApiPorts) -> object:\n    return ports\n",
        encoding="utf-8",
    )


def empty_a_route_module_that_is_declared_to_hold_ports(project: Path) -> None:
    (project / "src/atelier2/api/routes/events.py").write_text(
        "router = None\n", encoding="utf-8"
    )


RUN_QUERIES_IMPORT = "from atelier2.ports.run_queries import RunQueries"
CANCELLATION_IMPORT = (
    "from atelier2.ports.agent_attempts import AgentAttemptCancellationResult"
)
QUARANTINED_IMPORT = (
    "from typing import TYPE_CHECKING\n"
    "\nif TYPE_CHECKING:\n"
    "    from atelier2.ports.run_queries import RunQueries"
)


@pytest.mark.parametrize(
    ("field", "imported"),
    [
        ("run_queries: RunQueries", RUN_QUERIES_IMPORT),
        ('run_queries: "RunQueries"', RUN_QUERIES_IMPORT),
        ("run_queries: ports.RunQueries", ""),
        ("run_queries: RunQueries", QUARANTINED_IMPORT),
        (
            "cancel: Callable[[str], AgentAttemptCancellationResult]",
            CANCELLATION_IMPORT,
        ),
        ("run_queries = None", ""),
    ],
    ids=[
        "plain-port-type",
        "forward-reference",
        "dotted-port-path",
        "quarantined-under-type-checking",
        "port-union-behind-a-bound-call",
        "unannotated",
    ],
)
@pytest.mark.proves("the-use-case-record-cannot-hand-a-route-a-port")
def test_a_use_case_field_that_resolves_to_a_port_fails(
    tmp_path: Path, field: str, imported: str
) -> None:
    project = copied_project(tmp_path)
    replace_use_case_field(project, field, imported)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "a route can still reach a port" in result.stderr


@pytest.mark.proves("the-use-case-record-cannot-hand-a-route-a-port")
def test_a_use_case_record_that_disappears_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    rename_the_use_case_record(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "declares no ApiUseCases" in result.stderr


@pytest.mark.proves("an-untranslated-route-is-named-in-both-directions")
def test_a_translated_route_module_that_reaches_a_port_again_fails(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    add_route_reaching_a_port(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "health.py: leak reaches a port" in result.stderr


@pytest.mark.proves("an-untranslated-route-is-named-in-both-directions")
def test_a_stale_entry_in_the_untranslated_route_list_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    empty_a_route_module_that_is_declared_to_hold_ports(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "event_stream_route reaches no port any more" in result.stderr


ALIAS_IMPORT = (
    "from atelier2.ports.run_queries import RunQueries\nPortAlias = RunQueries"
)
REEXPORT_MODULE = "src/atelier2/application/port_reexport.py"
REEXPORT_IMPORT = "from atelier2.application.port_reexport import RunPage"


def add_port_reexport_module(project: Path) -> None:
    """A module of the application layer that re-exports a durable port verbatim.

    Every layer contract stays green: `application` may read `ports`, and `api`
    may read `application`. The re-exported name is deliberately one `context.py`
    does not import itself, so this case cannot pass for the older rule's reason —
    only a check that resolves what the field really names can see the port.
    """
    (project / REEXPORT_MODULE).write_text(
        "from atelier2.ports.run_queries import RunPage as RunPage\n",
        encoding="utf-8",
    )


def reach_a_port_from_a_read_of_an_allowlisted_module(project: Path) -> None:
    """Hand the port back inside a read of a module allowlisted for another call.

    Behaviour-preserving and type-correct: the composition lambda and this call
    take the same arguments. `events` legitimately keeps a port for its stream, so
    a check that records one boolean per module cannot see that this *read* stopped
    going through its use-case.
    """
    events = project / "src/atelier2/api/routes/events.py"
    source = events.read_text(encoding="utf-8")
    replaced = source.replace(
        "        lambda: context.use_cases.prepare_run_events(run_id, after_sequence),",
        "        lambda: prepare_run_events(\n"
        "            run_id, after_sequence, context.ports.run_event_queries\n"
        "        ),",
    )
    assert replaced != source
    events.write_text(
        replaced.replace(
            "from atelier2.application.prepare_run_events import (",
            "from atelier2.application.prepare_run_events import (\n    prepare_run_events,",
            1,
        ),
        encoding="utf-8",
    )


@pytest.mark.proves("the-use-case-record-cannot-hand-a-route-a-port")
def test_a_use_case_field_behind_a_local_alias_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    replace_use_case_field(
        project, "leaked_port: PortAlias | None = None", ALIAS_IMPORT
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "a route can still reach a port" in result.stderr


@pytest.mark.proves("the-use-case-record-cannot-hand-a-route-a-port")
def test_a_use_case_field_behind_a_re_export_of_another_layer_fails(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    add_port_reexport_module(project)
    replace_use_case_field(project, "leaked: RunPage | None = None", REEXPORT_IMPORT)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "a route can still reach a port" in result.stderr


@pytest.mark.proves("an-untranslated-route-is-named-in-both-directions")
def test_a_read_that_reaches_a_port_inside_an_allowlisted_module_fails(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    reach_a_port_from_a_read_of_an_allowlisted_module(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "prepare_events" in result.stderr
