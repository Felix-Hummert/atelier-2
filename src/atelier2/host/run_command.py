"""Run one workflow document on a served Atelier API with a single command.

This module is a client of the product, not a second way into it. Every effect
it has travels through the published HTTP API of a service someone is already
serving, and every shape it writes or reads is the wire contract that API
publishes, so this command can neither do nor claim anything an operator could
not obtain from the same service by hand.

The API exposes no lookup for a published auth profile or agent configuration,
so `run` publishes each of them from the operator's own files on every
invocation. A workflow revision can be looked up by the name its lineage
carries, which is what `resolve` asks and what a later `run --name` will use;
`run --workflow` still publishes the document it was handed. All three publications are idempotent: identical
bytes answer with the same hash and change nothing. The run identity is derived
from those hashes for the same reason, so repeating the command reports the
first run again instead of starting - and paying for - a second one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import IO, Final
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import decode_canonical_base64
from atelier2.api.wire.events import (
    ActionReconciliationRequiredEventResource,
    ActionReconciliationRequiredEventResourceV2,
    AgentCompletedEventResource,
    AgentCompletedEventResourceV2,
    AgentFailedEventResourceV2,
    WaitingInputEventResource,
    WaitingInputEventResourceV2,
)
from atelier2.api.wire.requests import (
    PublishAgentConfigurationRevisionRequestResource,
    PublishAuthProfileRevisionRequestResource,
    StartRunAgentBindingResourceV2,
    StartRunRequestResource,
    StartRunRequestResourceV2,
)
from atelier2.api.wire.resources import (
    AgentConfigurationRevisionResource,
    AnyRunResource,
    AuthProfileRevisionResource,
    CatalogNameResolutionResource,
    ProblemResource,
    StreamFailureResource,
    WorkflowRevisionDetailResource,
)
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import RunState
from atelier2.host.address import DEFAULT_SERVICE_URL

REQUEST_TIMEOUT_SECONDS = 30.0

AUTH_PROFILE_PATH = "/auth-profile-revisions"
AGENT_CONFIGURATION_PATH = "/agent-configuration-revisions"
WORKFLOW_REVISION_PATH = "/workflow-revisions"
RUN_PATH = "/runs"
DEFAULT_CATALOG_POSITION: Final = "head"

JSON_MEDIA_TYPE = "application/json"
YAML_MEDIA_TYPE = "application/yaml"
EVENT_STREAM_MEDIA_TYPE = "text/event-stream"
ADDRESSABLE_SCHEMES = frozenset({"http", "https"})

RUN_IDENTITY_DOMAIN = "atelier2-command-line-run"

ActedEventResource = (
    AgentCompletedEventResource
    | AgentCompletedEventResourceV2
    | AgentFailedEventResourceV2
    | WaitingInputEventResource
    | WaitingInputEventResourceV2
    | ActionReconciliationRequiredEventResource
    | ActionReconciliationRequiredEventResourceV2
)
ACTED_EVENT_NAMES = frozenset(
    {
        RunEventKind.AGENT_COMPLETED,
        RunEventKind.AGENT_FAILED,
        RunEventKind.WAITING_INPUT,
        RunEventKind.ACTION_RECONCILIATION_REQUIRED,
    }
)

STREAM_FAILURE_NAME: Final[str] = StreamFailureResource.model_fields["event"].default
"""The name of the stream's own problem frame, read from the model that owns it."""

_auth_profile_resource = TypeAdapter(AuthProfileRevisionResource)
_agent_configuration_resource = TypeAdapter(AgentConfigurationRevisionResource)
_workflow_revision_resource = TypeAdapter(WorkflowRevisionDetailResource)
_catalog_name_resolution_resource = TypeAdapter(CatalogNameResolutionResource)
_run_resource = TypeAdapter[AnyRunResource](AnyRunResource)
_acted_event_resource = TypeAdapter[ActedEventResource](ActedEventResource)
_stream_failure_resource = TypeAdapter(StreamFailureResource)


class RunCommandRefusal(Exception):
    """This command cannot honestly report a run that ended."""


class UnusableRunOrder(RunCommandRefusal):
    """The order names something the public API could never accept."""


class ServiceUnreachable(RunCommandRefusal):
    """No Atelier service answered at the named address."""


class ServiceRefused(RunCommandRefusal):
    """The service answered a typed problem instead of the resource asked for."""


class UnreadableServiceAnswer(RunCommandRefusal):
    """The service answered something this command cannot read as its contract."""


class RunNeedsAnotherActor(RunCommandRefusal):
    """The run stopped on a decision this command is not allowed to make."""


class RunUnfinished(RunCommandRefusal):
    """The event history ended while the run had not."""


class AgentBindingDocument(BaseModel):
    """One operator file naming the agent an unbound workflow role needs.

    It carries what the two publication routes ask for, because this command
    publishes the pair rather than looking up hashes the API cannot serve. What
    each field may hold is the publication request's own business, so this
    document declares no bound of its own and hands the values to the resource
    that owns them.

    A refusal quotes the failing value, which is what makes it usable. This file
    may therefore never carry a credential: the published request resources it
    is made of carry none today, and a field that ever does belongs in a secret
    channel rather than here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    auth_profile: PublishAuthProfileRevisionRequestResource
    model: str
    executor_revision: str

    def publication(
        self, auth_profile_revision_hash: str
    ) -> PublishAgentConfigurationRevisionRequestResource:
        return PublishAgentConfigurationRevisionRequestResource(
            model=self.model,
            auth_profile_revision_hash=auth_profile_revision_hash,
            executor_revision=self.executor_revision,
        )


@dataclass(frozen=True)
class CarriedEvent:
    """What one stream frame says it is, before it is read as its own resource."""

    kind: str
    cursor: str | None


@dataclass(frozen=True)
class AgentBindingSource:
    role: str
    document: bytes


@dataclass(frozen=True)
class AgentRoleBinding:
    role: str
    agent_configuration_revision_hash: str


@dataclass(frozen=True)
class AgentOutput:
    node_id: str
    output: bytes
    output_hash: str
    attempt_id: str | None


@dataclass(frozen=True)
class RunOrder:
    service_url: str
    workflow_document: bytes
    bindings: tuple[AgentBindingSource, ...]
    run_id: str | None


@dataclass(frozen=True)
class RunHistory:
    """What one reading of a run's event stream saw, and how far it got."""

    outputs: tuple[AgentOutput, ...]
    last_cursor: str | None


@dataclass(frozen=True)
class RunReport:
    run_id: str
    public_run_reference: str
    workflow_revision_hash: str
    terminal_hash: str
    outputs: tuple[AgentOutput, ...]
    resolved_name: NameResolution | None = None
    """The name this run was started by, where a name is how it was asked for.

    A run started from a document has none, and the receipt says nothing about
    one rather than inventing a label for bytes the operator handed over.
    """


@dataclass(frozen=True)
class NamedRunOrder:
    """Run the workflow a catalog name holds, without naming a document at all."""

    service_url: str
    name: str
    bindings: tuple[AgentBindingSource, ...]
    run_id: str | None
    position: str = "head"


@dataclass(frozen=True)
class NameOrder:
    """One question for the catalog: which revision does this name hold?"""

    service_url: str
    name: str
    position: str = "head"


@dataclass(frozen=True)
class NameResolution:
    display_name: str
    lineage_id: str
    revision_hash: str
    revision_number: int


def resolve_published_name(order: NameOrder) -> NameResolution:
    """Ask the service which revision one catalog name holds. Start nothing.

    The service owns the answer and every refusal in it: an unadmitted name, a
    retired lineage, a position it does not hold. This command adds no judgment
    of its own -- it asks, and hands the service's own words on.
    """

    api = _api_url(order.service_url)
    asked = f"{api}{WORKFLOW_REVISION_PATH}/by-name/{quote(order.name, safe='')}"
    if order.position != DEFAULT_CATALOG_POSITION:
        asked = f"{asked}?position={quote(order.position, safe='')}"
    resolved = _decoded(
        _catalog_name_resolution_resource, _get(asked), "a catalog name"
    )
    return NameResolution(
        resolved.display_name,
        resolved.lineage_id,
        resolved.revision_hash,
        resolved.revision_number,
    )


def describe_resolution(resolution: NameResolution) -> str:
    """What the name resolved to, in the terms the operator asked in."""

    return (
        f"{resolution.display_name} is revision {resolution.revision_number} "
        f"of lineage {resolution.lineage_id}: {resolution.revision_hash}"
    )


def execute_run(order: RunOrder) -> RunReport:
    """Publish what the run binds, start it, and report the run that ended."""

    api = _api_url(order.service_url)
    bindings = tuple(_published_binding(api, source) for source in order.bindings)
    revision_hash = _published_workflow_revision(api, order.workflow_document)
    return _run_published_revision(api, revision_hash, bindings, order.run_id)


def execute_named_run(order: NamedRunOrder) -> RunReport:
    """Run the workflow a name holds: ask the catalog, then start what it answered.

    The name is resolved before anything is written, and by the same question
    `resolve` asks -- one owner for what a name means, so this command cannot
    disagree with that one about which revision an operator meant. Nothing is
    published: the revision the name holds is already in the store, and
    republishing it would mint a second identity for the same bytes.
    """

    api = _api_url(order.service_url)
    resolution = resolve_published_name(
        NameOrder(order.service_url, order.name, order.position)
    )
    bindings = tuple(_published_binding(api, source) for source in order.bindings)
    report = _run_published_revision(
        api, resolution.revision_hash, bindings, order.run_id
    )
    return replace(report, resolved_name=resolution)


def _run_published_revision(
    api: str,
    revision_hash: str,
    bindings: tuple[AgentRoleBinding, ...],
    asked_run_id: str | None,
) -> RunReport:
    """Start one published revision and report the run that ended.

    Both ways in share this: a run is the same thing whether its revision was
    published from a document or resolved from a name, and one owner of the start,
    the reading and the refusals is what keeps the two from drifting apart.
    """

    run_id = asked_run_id or derived_run_id(revision_hash, bindings)
    started = _decoded(
        _run_resource,
        _post(api + RUN_PATH, _start_request(run_id, revision_hash, bindings)),
        "a run",
    )
    reference = started.public_run_reference
    history = _read_history(api, reference)
    ended = _decoded(_run_resource, _get(f"{api}{RUN_PATH}/{reference}"), "a run")
    if ended.state != RunState.COMPLETED or ended.terminal_hash is None:
        raise RunUnfinished(
            f"the event history of run {reference} ended while the run was still "
            f"{ended.state}"
        )
    if history.last_cursor != ended.latest_event_cursor:
        raise RunUnfinished(
            f"the event history of run {reference} broke off at "
            f"{history.last_cursor or 'no event at all'} while the run's own "
            f"latest event is {ended.latest_event_cursor}, so what this command "
            "read is not the whole output; nothing was started twice, so running "
            "this command again is safe"
        )
    return RunReport(
        run_id=run_id,
        public_run_reference=reference,
        workflow_revision_hash=ended.workflow_revision_hash,
        terminal_hash=ended.terminal_hash,
        outputs=history.outputs,
    )


def derived_run_id(revision_hash: str, bindings: tuple[AgentRoleBinding, ...]) -> str:
    """Derive the identity that makes repeating the same command harmless.

    `POST /runs` is idempotent over the caller's own run identity, so an
    identity derived from everything the run is made of turns a repeated
    command into a second report of the first run instead of a second run.
    """

    bound = (
        binding.role.encode()
        + b"="
        + binding.agent_configuration_revision_hash.encode()
        for binding in sorted(bindings, key=lambda binding: binding.role)
    )
    preimage = frame(RUN_IDENTITY_DOMAIN, revision_hash.encode(), *bound)
    return Sha256Hash.of(preimage).value


def describe_receipt(report: RunReport) -> str:
    """Render what binds the printed output to the run that produced it."""

    lines = [
        f"run: {report.public_run_reference}",
        *(
            []
            if report.resolved_name is None
            else [f"name: {describe_resolution(report.resolved_name)}"]
        ),
        f"run identity: {report.run_id}",
        f"workflow revision: {report.workflow_revision_hash}",
        f"terminal hash: {report.terminal_hash}",
    ]
    lines.extend(
        f"output of node {output.node_id}: {output.output_hash}"
        f"{'' if output.attempt_id is None else ' from attempt ' + output.attempt_id}"
        for output in report.outputs
    )
    return "\n".join(lines)


def _api_url(service_url: str) -> str:
    address = urlsplit(service_url)
    if address.scheme not in ADDRESSABLE_SCHEMES or not address.netloc:
        raise UnusableRunOrder(
            f"{service_url!r} is not the address of a served Atelier API; "
            f"name one as {DEFAULT_SERVICE_URL!r}"
        )
    return service_url.rstrip("/") + API_PREFIX


def _published_binding(api: str, source: AgentBindingSource) -> AgentRoleBinding:
    try:
        document = AgentBindingDocument.model_validate_json(source.document)
    except ValidationError as error:
        raise _unpublishable_binding(source.role, error) from error
    profile = _decoded(
        _auth_profile_resource,
        _post(
            api + AUTH_PROFILE_PATH,
            document.auth_profile.model_dump_json().encode(),
        ),
        "an auth profile revision",
    )
    try:
        publication = document.publication(profile.auth_profile_revision_hash)
    except ValidationError as error:
        raise _unpublishable_binding(source.role, error) from error
    configuration = _decoded(
        _agent_configuration_resource,
        _post(api + AGENT_CONFIGURATION_PATH, publication.model_dump_json().encode()),
        "an agent configuration revision",
    )
    return AgentRoleBinding(
        source.role, configuration.agent_configuration_revision_hash
    )


def _unpublishable_binding(role: str, error: ValidationError) -> UnusableRunOrder:
    return UnusableRunOrder(
        f"the binding of role {role} is not an agent this API could publish: {error}"
    )


def _published_workflow_revision(api: str, document: bytes) -> str:
    revision = _decoded(
        _workflow_revision_resource,
        _post(api + WORKFLOW_REVISION_PATH, document, media_type=YAML_MEDIA_TYPE),
        "a workflow revision",
    )
    return revision.revision_hash


def _start_request(
    run_id: str, revision_hash: str, bindings: tuple[AgentRoleBinding, ...]
) -> bytes:
    if bindings:
        requested = StartRunRequestResourceV2(
            workflow_format_version=2,
            run_id=run_id,
            workflow_revision_hash=revision_hash,
            agent_bindings=tuple(
                StartRunAgentBindingResourceV2(
                    role=binding.role,
                    agent_configuration_revision_hash=(
                        binding.agent_configuration_revision_hash
                    ),
                )
                for binding in bindings
            ),
        )
    else:
        requested = StartRunRequestResource(
            run_id=run_id, workflow_revision_hash=revision_hash
        )
    return requested.model_dump_json().encode()


def _read_history(api: str, public_run_reference: str) -> RunHistory:
    """Read the run's own event history, which is where its output lives.

    The stream ends itself when the run reaches its terminal event, so this
    waits exactly as long as the run takes. A run that stops on a decision this
    command cannot make would otherwise keep it waiting forever, so those
    events are refusals rather than silence.

    The stream may also end early - it says so with a failure frame, or by
    simply stopping when the service wants the client to reconnect. Neither is
    an ended run, so this reports how far the reading got and lets the caller
    weigh that against the run's own latest event.
    """

    url = f"{api}{RUN_PATH}/{public_run_reference}/events"
    request = Request(url, method="GET", headers={"accept": EVENT_STREAM_MEDIA_TYPE})
    outputs: list[AgentOutput] = []
    last_cursor: str | None = None
    with _open(request, timeout=None) as stream:
        for data in _server_sent_data(stream):
            carried = _carried_event(data)
            if carried.kind == STREAM_FAILURE_NAME:
                raise ServiceRefused(_failed_stream(url, data))
            if carried.cursor is not None:
                last_cursor = carried.cursor
            if carried.kind not in ACTED_EVENT_NAMES:
                continue
            event = _decoded(_acted_event_resource, data.encode(), "a run event")
            match event:
                case AgentCompletedEventResource():
                    outputs.append(
                        AgentOutput(
                            node_id=event.node_id,
                            output=event.output.encode(),
                            output_hash=event.payload_hash,
                            attempt_id=None,
                        )
                    )
                case AgentCompletedEventResourceV2():
                    outputs.append(
                        AgentOutput(
                            node_id=event.node_id,
                            output=_decoded_output(event.output_base64),
                            output_hash=event.output_hash,
                            attempt_id=event.attempt_id,
                        )
                    )
                case AgentFailedEventResourceV2():
                    raise RunNeedsAnotherActor(
                        f"agent attempt {event.attempt_id} of node {event.node_id} "
                        f"failed with {event.failure_code}; only an operator "
                        "replacement continues this run"
                    )
                case WaitingInputEventResource() | WaitingInputEventResourceV2():
                    raise RunNeedsAnotherActor(
                        f"node {event.node_id} is waiting for an "
                        f"{event.answer_type} answer; this command carries no "
                        "answer, and run input follows issue #38"
                    )
                case _:
                    raise RunNeedsAnotherActor(
                        f"node {event.node_id} reached an unknown outcome; only an "
                        "accountable operator determination resolves it, and this "
                        "command makes none"
                    )
    return RunHistory(tuple(outputs), last_cursor)


def _decoded_output(output_base64: str) -> bytes:
    try:
        return decode_canonical_base64(output_base64)
    except ValueError as error:
        raise UnreadableServiceAnswer(
            f"an agent output is not canonical base64: {error}"
        ) from error


def _carried_event(data: str) -> CarriedEvent:
    """Read which event this is, and where it sits, from the event itself.

    Every run event resource carries its own kind and its own cursor, and those
    fields are the published contract; the stream frame's optional name field is
    not, and the served API writes none, so the payload is what this command
    reads. The failure frame carries a kind but no cursor, which is why the
    cursor is optional here and nowhere else.
    """

    try:
        carried = json.loads(data)
    except json.JSONDecodeError as error:
        raise UnreadableServiceAnswer(
            f"the event stream carried something that is not JSON: {error}"
        ) from error
    if not isinstance(carried, dict):
        raise UnreadableServiceAnswer("the event stream carried no run event")
    cursor = carried.get("cursor")
    return CarriedEvent(
        kind=str(carried.get("event", "")),
        cursor=cursor if isinstance(cursor, str) else None,
    )


def _failed_stream(url: str, data: str) -> str:
    failure = _decoded(_stream_failure_resource, data.encode(), "a stream failure")
    return _refusal_sentence(url, failure.problem)


def _server_sent_data(stream: IO[bytes]) -> Iterator[str]:
    data_lines: list[str] = []
    for raw_line in stream:
        try:
            line = raw_line.decode().rstrip("\r\n")
        except UnicodeDecodeError as error:
            raise UnreadableServiceAnswer(
                "the event stream carried bytes that are not UTF-8 text"
            ) from error
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
            data_lines = []
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data_lines.append(value.removeprefix(" "))
    if data_lines:
        yield "\n".join(data_lines)


def _post(url: str, payload: bytes, media_type: str = JSON_MEDIA_TYPE) -> bytes:
    return _read(
        Request(
            url,
            data=payload,
            method="POST",
            headers={"content-type": media_type, "accept": JSON_MEDIA_TYPE},
        )
    )


def _get(url: str) -> bytes:
    return _read(Request(url, method="GET", headers={"accept": JSON_MEDIA_TYPE}))


def _read(request: Request) -> bytes:
    with _open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.read()


def _open(request: Request, timeout: float | None) -> IO[bytes]:
    try:
        return urlopen(request, timeout=timeout)
    except HTTPError as refusal:
        raise ServiceRefused(_described_problem(refusal)) from refusal
    except URLError as unreachable:
        raise ServiceUnreachable(
            f"no Atelier service answered at {request.full_url}: {unreachable.reason}"
        ) from unreachable


def _described_problem(refusal: HTTPError) -> str:
    """Hand the service's own typed refusal on, without inventing prose for it."""

    answered = refusal.read()
    try:
        problem = ProblemResource.model_validate_json(answered)
    except ValidationError:
        return f"{refusal.url} answered {refusal.status} {refusal.reason}"
    return _refusal_sentence(refusal.url, problem)


def _refusal_sentence(url: str, problem: ProblemResource) -> str:
    """One shape for a typed problem, whichever channel the service wrote it on."""

    return (
        f"{url} refused this: {problem.status} {problem.title} "
        f"[{problem.type}] {problem.detail}"
    )


def _decoded[Resource](
    adapter: TypeAdapter[Resource], answered: bytes, subject: str
) -> Resource:
    try:
        return adapter.validate_json(answered)
    except ValidationError as error:
        raise UnreadableServiceAnswer(
            f"the service answered something this command cannot read as "
            f"{subject}: {error}"
        ) from error
