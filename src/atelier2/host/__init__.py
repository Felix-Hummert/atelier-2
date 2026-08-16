"""The operator's command line: serve the product, or run one workflow on it."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

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
from atelier2.host.address import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_SERVICE_URL
from atelier2.host.run_command import (
    AgentBindingSource,
    RunCommandRefusal,
    RunOrder,
    describe_receipt,
    execute_run,
)
from atelier2.host.serving import HostSettings, serve

RUN_DESCRIPTION = """\
Run one workflow document on a served Atelier API and wait for its end. Every
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

Not supported yet, and refused rather than faked:

  --input       run input follows issue #38. Until it lands, the job travels
                inside the workflow document, which burns one published
                revision per distinct input.
  a name        starting a workflow by name follows issue #22, so --workflow
                takes the document itself.

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
    parser.error("a command is required")


def _serve(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> int:
    try:
        settings = HostSettings(
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
            claude_subscription=_claude_subscription_settings(parser, parsed),
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
    order = RunOrder(
        service_url=parsed.service,
        workflow_document=_file_bytes(parser, parsed.workflow),
        bindings=tuple(
            _binding_source(parser, declared) for declared in parsed.binding
        ),
        run_id=parsed.run_id,
    )
    try:
        report = execute_run(order)
    except RunCommandRefusal as refusal:
        print(refusal, file=sys.stderr)
        return 1
    for output in report.outputs:
        sys.stdout.buffer.write(output.output)
    sys.stdout.buffer.flush()
    print(describe_receipt(report), file=sys.stderr)
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


def _file_bytes(parser: argparse.ArgumentParser, path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as unreadable:
        parser.error(f"cannot read {path}: {unreadable.strerror}")


def _claude_subscription_settings(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> ClaudeSubscriptionSettings | None:
    """Compose the Claude subscription executor only when fully declared."""

    declared = (
        parsed.claude_executable,
        parsed.claude_workspace,
        parsed.claude_credential_directory,
    )
    if all(value is None for value in declared):
        return None
    if any(value is None for value in declared):
        parser.error(
            "serving Claude subscription agents requires --claude-executable, "
            "--claude-workspace and --claude-credential-directory together"
        )
    search_path = os.environ.get("PATH")
    if search_path is None:
        parser.error(
            "serving Claude subscription agents requires PATH in the server "
            "environment, because the launched provider inherits nothing else"
        )
    settings = ClaudeSubscriptionSettings(
        parsed.claude_executable,
        parsed.claude_workspace,
        parsed.claude_credential_directory,
        search_path,
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


def _codex_subscription_settings(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> CodexSubscriptionSettings | None:
    """Compose the Codex subscription executor only when fully declared."""

    declared = (
        parsed.codex_executable,
        parsed.codex_workspace,
        parsed.codex_credential_directory,
    )
    if all(value is None for value in declared):
        return None
    if any(value is None for value in declared):
        parser.error(
            "serving Codex subscription agents requires --codex-executable, "
            "--codex-workspace and --codex-credential-directory together"
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
            parsed.codex_workspace,
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
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--claude-executable", type=Path)
    serve_parser.add_argument("--claude-workspace", type=Path)
    serve_parser.add_argument("--claude-credential-directory", type=Path)
    serve_parser.add_argument("--codex-executable", type=Path)
    serve_parser.add_argument("--codex-workspace", type=Path)
    serve_parser.add_argument("--codex-credential-directory", type=Path)
    serve_parser.add_argument(
        "--codex-sandbox",
        choices=tuple(mode.value for mode in CodexSandboxMode),
        default=CodexSandboxMode.READ_ONLY.value,
    )
    run_parser = commands.add_parser(
        "run",
        help="run one workflow document on a served Atelier API",
        description=RUN_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_parser.add_argument(
        "--workflow",
        type=Path,
        required=True,
        metavar="DOCUMENT.yaml",
        help="the workflow document to publish and run",
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
