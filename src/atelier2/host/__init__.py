"""The operator's command line: serve, run, resolve, or migrate a store."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from atelier2.adapters.agent_workspaces import (
    AgentScratchRootRefused,
    LocalAgentAttemptWorkspaceOwner,
)
from atelier2.adapters.claude_subscription import (
    MANAGED_POLICY_ROOTS,
    ClaudeExecutableUnsupported,
    ClaudeManagedPolicyPresent,
    ClaudeSubscriptionSettings,
    attest_no_managed_policy,
    verify_claude_capability,
)
from atelier2.adapters.codex_subscription import (
    CodexContainmentUnattested,
    CodexExecutableUnsupported,
    CodexSandboxMode,
    CodexSubscriptionSettings,
    attest_codex_containment,
    verify_codex_capability,
)
from atelier2.adapters.dbos.schema import StoreMigrationRefused
from atelier2.adapters.grok_subscription import (
    GrokExecutableUnsupported,
    GrokSubscriptionSettings,
    verify_grok_capability,
)
from atelier2.host.address import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_SERVICE_URL
from atelier2.host.migrate_command import describe_migration, execute_migrate
from atelier2.host.run_command import (
    DEFAULT_CATALOG_POSITION,
    AgentBindingSource,
    NamedRunOrder,
    NameOrder,
    RunCommandRefusal,
    RunOrder,
    SuppliedOrder,
    describe_receipt,
    describe_resolution,
    execute_named_run,
    execute_run,
    resolve_published_name,
)
from atelier2.host.serving import (
    HostSettings,
    api_limits,
    event_poll_backoff,
    serve,
)

MIGRATE_DESCRIPTION = """\
Raise an existing canonical store to the current product schema.

This command is offline. It does not start a server, does not open a runtime,
and does not create a store. Stop the process that owns the file first. A
write lock the command can see is refused; an idle reader is not always
visible, so stopping the serve is the operator's gate, not this process's.

The file is inspected, then raised one published step at a time. Each step
ends with the fingerprint ADR 0001 names. Any doubt rolls the transaction
back, so a failed hop leaves the predecessor unaltered. Today the only
built step is schema version 13 to 14. Older published predecessors, and
unknown or future versions, are refused by name.

A store already on the current schema is left unaltered and said to be
already current.
"""


RESOLVE_DESCRIPTION = """\
Ask a served Atelier which published revision a workflow name holds, and print
the lineage, the member number and the exact revision hash.

This command starts nothing, which is the whole of what separates it from `run
--name`: that one asks this same question and then runs the answer, so use this
one to look before you leap. Every refusal is the service's own - an unadmitted
name, a retired lineage, a position the lineage does not hold - and each one ends
this command unsuccessfully, there and in `run --name` alike.
"""

RUN_DESCRIPTION = """\
Run a workflow on a served Atelier API and wait for its end -- either the
document named by --workflow, or the one a catalog name holds via --name. Every
agent output the run produced is written to standard output, as the exact bytes
its hash covers and with no separator added, so a piped output is the output;
the run, its revision, its terminal hash and one hash per output are written to
standard error. The exit code is 0 only for a run whose whole event history this
command read and whose terminal event it saw.

The command owns nothing. It publishes the workflow document and each binding
file through the public API of the service named by --service, and starts the
run there, exactly as any other client would. All three publications are
idempotent, and the run identity is derived from the published hashes, so the
same command run twice reports one run instead of paying for two.

With --name nothing is published for the workflow: the service is asked which
revision the name holds -- the same question `resolve` asks, and its refusals are
handed on unchanged -- and that revision is what starts. --position picks the
member of the lineage, so a name can be run at an exact revision rather than
only at its head.

--input NAME=VALUE and --input-file NAME=PATH fill the graph_inputs the
workflow declared. VALUE and the file are exact JSON text; the command
publishes nothing for them and hands the bytes to POST /runs. A name the
document never declared, a declared name that is missing, and a value that
is not valid JSON for the schema the document pinned are each refused by
name. A typed 422 from the service is handed on in the service's own words.

Not supported yet, and refused rather than faked:

  a wait        a run that stops for a human ends this command unsuccessfully
                and says which capability is missing; answering a wait from here
                is not built.

There is no verdict exit code: output contracts (issue #57) do not exist yet,
so the exit code reports the run's disposition and nothing more.
"""

BINDING_SEPARATOR = "="


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    parsed = parser.parse_args(arguments)
    if parsed.command == "serve":
        return _serve(parser, parsed)
    if parsed.command == "run":
        return _run(parser, parsed)
    if parsed.command == "resolve":
        return _resolve(parser, parsed)
    if parsed.command == "migrate":
        return _migrate(parsed)
    parser.error("a command is required")


def _given[ValueT](**flags: ValueT | None) -> dict[str, ValueT]:
    """Only the answers the operator actually gave.

    A flag nobody passed is not an answer, so it is left out and the field's own
    default stands. That keeps one named place for every default instead of
    repeating each of them here as `or DEFAULT`.
    """

    return {name: value for name, value in flags.items() if value is not None}


def _serve(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> int:
    try:
        limits = api_limits(
            **_given(
                event_page_size=parsed.event_page_size,
                maximum_control_queries=parsed.maximum_control_queries,
                maximum_event_poll_queries=parsed.maximum_event_poll_queries,
                maximum_query_admission_wait_milliseconds=(
                    parsed.query_admission_wait_milliseconds
                ),
            )
        )
        backoff = event_poll_backoff(
            **_given(
                initial_delay_seconds=parsed.initial_event_poll_delay_seconds,
                maximum_delay_seconds=parsed.maximum_event_poll_delay_seconds,
                multiplier=parsed.event_poll_delay_multiplier,
            )
        )
        settings = HostSettings(
            limits=limits,
            event_poll_backoff=backoff,
            **_given(
                sqlite_lock_timeout_seconds=parsed.sqlite_lock_timeout_seconds,
                agent_termination_grace_seconds=(
                    parsed.agent_termination_grace_seconds
                ),
            ),
            database_path=parsed.database,
            effect_store_path=parsed.effect_store,
            effect_adapter_revision=parsed.effect_adapter_revision,
            effect_destination=parsed.effect_destination,
            application_version=parsed.application_version,
            source_commit=parsed.source_commit,
            source_tree=parsed.source_tree,
            frontend_dist=parsed.frontend_dist,
            host=parsed.host,
            port=parsed.port,
            agent_scratch_root=_attested_agent_scratch_root(parser, parsed),
            claude_subscription=_claude_subscription_settings(parser, parsed),
            grok_subscription=_grok_subscription_settings(parser, parsed),
            codex_subscription=_codex_subscription_settings(parser, parsed),
        )
    except ValueError as refusal:
        parser.error(str(refusal))
    try:
        serve(settings)
    except KeyboardInterrupt:
        return 0
    return 0


def _run(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> int:
    bindings = tuple(_binding_source(parser, declared) for declared in parsed.binding)
    orders = _supplied_orders(parser, parsed)
    if parsed.position is not None and parsed.name is None:
        # A position without a name would be read and then ignored, which is the
        # quietest way for a command to disagree with the operator.
        parser.error("--position selects a member of --name, so it needs one")
    try:
        if parsed.name is not None:
            report = execute_named_run(
                NamedRunOrder(
                    service_url=parsed.service,
                    name=parsed.name,
                    bindings=bindings,
                    run_id=parsed.run_id,
                    position=parsed.position or DEFAULT_CATALOG_POSITION,
                    orders=orders,
                )
            )
        else:
            report = execute_run(
                RunOrder(
                    service_url=parsed.service,
                    workflow_document=_file_bytes(parser, parsed.workflow),
                    bindings=bindings,
                    run_id=parsed.run_id,
                    orders=orders,
                )
            )
    except RunCommandRefusal as refusal:
        print(refusal, file=sys.stderr)
        return 1
    for output in report.outputs:
        sys.stdout.buffer.write(output.output)
    sys.stdout.buffer.flush()
    print(describe_receipt(report), file=sys.stderr)
    return 0


def _migrate(parsed: argparse.Namespace) -> int:
    try:
        report = execute_migrate(parsed.database)
    except StoreMigrationRefused as refusal:
        print(refusal, file=sys.stderr)
        return 1
    print(describe_migration(report))
    return 0


def _resolve(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> int:
    """Answer which revision a name holds. Start nothing, and say nothing else."""

    del parser
    order = NameOrder(
        service_url=parsed.service, name=parsed.name, position=parsed.position
    )
    try:
        resolution = resolve_published_name(order)
    except RunCommandRefusal as refusal:
        print(refusal, file=sys.stderr)
        return 1
    print(describe_resolution(resolution))
    return 0


def _binding_source(
    parser: argparse.ArgumentParser, declared: str
) -> AgentBindingSource:
    role, separator, path = declared.partition(BINDING_SEPARATOR)
    if not separator or not role or not path:
        parser.error(
            f"--binding takes role{BINDING_SEPARATOR}agent-file.json, not {declared!r}"
        )
    return AgentBindingSource(role, _file_bytes(parser, Path(path)))


def _named_assignment(
    parser: argparse.ArgumentParser, flag: str, declared: str
) -> tuple[str, str]:
    name, separator, value = declared.partition(BINDING_SEPARATOR)
    if not separator or not name or not value:
        parser.error(f"{flag} takes NAME{BINDING_SEPARATOR}VALUE, not {declared!r}")
    return name, value


def _supplied_orders(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> tuple[SuppliedOrder, ...]:
    collected: list[tuple[str, bytes]] = []
    for declared in parsed.input:
        name, text = _named_assignment(parser, "--input", declared)
        collected.append((name, text.encode()))
    for declared in parsed.input_file:
        name, path = _named_assignment(parser, "--input-file", declared)
        collected.append((name, _file_bytes(parser, Path(path))))
    seen: set[str] = set()
    orders: list[SuppliedOrder] = []
    for name, value in collected:
        if name in seen:
            parser.error(f"input {name!r} was supplied twice")
        seen.add(name)
        try:
            json.loads(value)
        except json.JSONDecodeError:
            parser.error(f"input {name!r} is not valid JSON for the pinned schema")
        orders.append(SuppliedOrder(name, value))
    return tuple(orders)


def _file_bytes(parser: argparse.ArgumentParser, path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as unreadable:
        parser.error(f"cannot read {path}: {unreadable.strerror}")


def _attested_agent_scratch_root(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> Path | None:
    """Refuse an unusable scratch root before the server exists.

    A root that is a git worktree, is shared, or belongs to somebody else is
    refused here rather than at the first run, where the refusal would cost a
    started run and reach nobody but a log.
    """

    root: Path | None = parsed.agent_scratch_root
    if root is None:
        return None
    try:
        LocalAgentAttemptWorkspaceOwner(root).close()
    except AgentScratchRootRefused as refusal:
        parser.error(str(refusal))
    return root


def _claude_subscription_settings(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> ClaudeSubscriptionSettings | None:
    """Compose the Claude subscription executor only when fully declared."""

    declared = (parsed.claude_executable, parsed.claude_credential_directory)
    if all(value is None for value in declared):
        return None
    if any(value is None for value in declared):
        parser.error(
            "serving Claude subscription agents requires --claude-executable "
            "and --claude-credential-directory together"
        )
    search_path = os.environ.get("PATH")
    if search_path is None:
        parser.error(
            "serving Claude subscription agents requires PATH in the server "
            "environment, because the launched provider inherits nothing else"
        )
    settings = ClaudeSubscriptionSettings(
        parsed.claude_executable, parsed.claude_credential_directory, search_path
    )
    # The containment this executor claims belongs to a measured release on a
    # host no administrator policy can act on, so the deployment asks the named
    # executable which one it is and attests that policy's absence before the
    # server exists at all -- never at invocation time, where a refusal costs a
    # run.
    try:
        attest_no_managed_policy(settings.credential_directory, MANAGED_POLICY_ROOTS)
        verify_claude_capability(settings.executable)
    except (ClaudeExecutableUnsupported, ClaudeManagedPolicyPresent) as error:
        parser.error(str(error))
    return settings


def _grok_subscription_settings(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> GrokSubscriptionSettings | None:
    """Compose the Grok subscription executor only when fully declared."""

    declared = (
        parsed.grok_executable,
        parsed.grok_workspace,
        parsed.grok_credential_directory,
    )
    if all(value is None for value in declared):
        return None
    if any(value is None for value in declared):
        parser.error(
            "serving Grok subscription agents requires --grok-executable, "
            "--grok-workspace and --grok-credential-directory together"
        )
    search_path = os.environ.get("PATH")
    if search_path is None:
        parser.error(
            "serving Grok subscription agents requires PATH in the server "
            "environment, because the launched provider inherits nothing else"
        )
    settings = GrokSubscriptionSettings(
        parsed.grok_executable,
        parsed.grok_workspace,
        parsed.grok_credential_directory,
        search_path,
    )
    try:
        verify_grok_capability(settings.executable)
    except GrokExecutableUnsupported as error:
        parser.error(str(error))
    return settings


def _codex_subscription_settings(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> CodexSubscriptionSettings | None:
    """Compose the Codex subscription executor only when fully declared."""

    declared = (parsed.codex_executable, parsed.codex_credential_directory)
    if all(value is None for value in declared):
        return None
    if any(value is None for value in declared):
        parser.error(
            "serving Codex subscription agents requires --codex-executable "
            "and --codex-credential-directory together"
        )
    search_path = os.environ.get("PATH")
    if search_path is None:
        parser.error(
            "serving Codex subscription agents requires PATH in the server "
            "environment, because the launched provider inherits nothing else"
        )
    try:
        settings = CodexSubscriptionSettings(
            parsed.codex_executable,
            parsed.codex_credential_directory,
            search_path,
            CodexSandboxMode(parsed.codex_sandbox),
        )
    except ValueError as refusal:
        parser.error(str(refusal))
    # A sandbox the host cannot actually start, and a profile that would load
    # the operator's own Codex trust, are both refused before the server exists
    # rather than at invocation time, where a refusal costs a run.
    try:
        verify_codex_capability(settings.executable, settings.search_path)
        attest_codex_containment(settings)
    except (CodexExecutableUnsupported, CodexContainmentUnattested) as error:
        parser.error(str(error))
    return settings


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atelier2")
    commands = parser.add_subparsers(dest="command")
    serve_parser = commands.add_parser("serve", help="serve the local cockpit")
    serve_parser.add_argument("--database", type=Path, required=True)
    serve_parser.add_argument("--effect-store", type=Path, required=True)
    serve_parser.add_argument("--effect-adapter-revision", required=True)
    serve_parser.add_argument("--effect-destination", required=True)
    serve_parser.add_argument("--application-version", required=True)
    serve_parser.add_argument("--source-commit", required=True)
    serve_parser.add_argument("--source-tree", required=True)
    serve_parser.add_argument("--frontend-dist", type=Path, required=True)
    # The instance's own answers. Everything above this line says which store,
    # which port, which executable; these say how this instance behaves once
    # those are settled, and they are the values a second machine honestly wants
    # differently. Each is refused by its owner when it is out of range.
    serve_parser.add_argument("--event-page-size", type=int)
    serve_parser.add_argument("--maximum-control-queries", type=int)
    serve_parser.add_argument("--maximum-event-poll-queries", type=int)
    serve_parser.add_argument("--query-admission-wait-milliseconds", type=int)
    serve_parser.add_argument("--initial-event-poll-delay-seconds", type=float)
    serve_parser.add_argument("--maximum-event-poll-delay-seconds", type=float)
    serve_parser.add_argument("--event-poll-delay-multiplier", type=float)
    serve_parser.add_argument("--sqlite-lock-timeout-seconds", type=float)
    serve_parser.add_argument("--agent-termination-grace-seconds", type=float)
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--agent-scratch-root", type=Path)
    serve_parser.add_argument("--claude-executable", type=Path)
    serve_parser.add_argument("--claude-credential-directory", type=Path)
    serve_parser.add_argument("--grok-executable", type=Path)
    serve_parser.add_argument("--grok-workspace", type=Path)
    serve_parser.add_argument("--grok-credential-directory", type=Path)
    serve_parser.add_argument("--codex-executable", type=Path)
    serve_parser.add_argument("--codex-credential-directory", type=Path)
    serve_parser.add_argument(
        "--codex-sandbox",
        choices=tuple(mode.value for mode in CodexSandboxMode),
        default=CodexSandboxMode.READ_ONLY.value,
    )
    migrate_parser = commands.add_parser(
        "migrate",
        help="raise an existing store to the current schema, offline",
        description=MIGRATE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    migrate_parser.add_argument("--database", type=Path, required=True)
    resolve_parser = commands.add_parser(
        "resolve",
        help="ask a served Atelier which revision a workflow name holds",
        description=RESOLVE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    resolve_parser.add_argument(
        "--name",
        required=True,
        metavar="NAME",
        help="the catalog name to resolve, or a 64-hex lineage id",
    )
    resolve_parser.add_argument(
        "--position",
        default=DEFAULT_CATALOG_POSITION,
        metavar="head|N",
        help=(
            "which member of the lineage to answer with: head, or an exact "
            f"member number (default {DEFAULT_CATALOG_POSITION})"
        ),
    )
    resolve_parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE_URL,
        help=f"the served Atelier API to ask (default {DEFAULT_SERVICE_URL})",
    )
    run_parser = commands.add_parser(
        "run",
        help="run one workflow document on a served Atelier API",
        description=RUN_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Exactly one source for the revision. Two would leave the operator guessing
    # which one the run used; none would leave the command with nothing to run.
    run_source = run_parser.add_mutually_exclusive_group(required=True)
    run_source.add_argument(
        "--workflow",
        type=Path,
        metavar="DOCUMENT.yaml",
        help="the workflow document to publish and run",
    )
    run_source.add_argument(
        "--name",
        metavar="NAME",
        help=(
            "run the workflow this catalog name holds, instead of publishing a "
            "document; the name is resolved by the service before anything starts"
        ),
    )
    run_parser.add_argument(
        "--position",
        default=None,
        metavar="head|N",
        help=(
            "which member of the named lineage to run: head, or an exact member "
            f"number (default {DEFAULT_CATALOG_POSITION}); only with --name"
        ),
    )
    run_parser.add_argument(
        "--binding",
        action="append",
        default=[],
        metavar="ROLE=AGENT.json",
        help=(
            "bind one agent role of a format-2 workflow to the agent described "
            "by that file; repeatable"
        ),
    )
    run_parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "one graph_input the workflow declared, as exact JSON text; "
            "repeatable; the command publishes nothing for it and hands the "
            "bytes to POST /runs"
        ),
    )
    run_parser.add_argument(
        "--input-file",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=(
            "one graph_input the workflow declared, read as exact JSON bytes "
            "from that file; repeatable"
        ),
    )
    run_parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE_URL,
        help=f"the served Atelier API to run this on (default {DEFAULT_SERVICE_URL})",
    )
    run_parser.add_argument(
        "--run-id",
        help=(
            "this run's own identity; without it the identity is derived from "
            "the published workflow and bindings, so repeating the command "
            "reports the same run instead of starting another"
        ),
    )
    return parser
