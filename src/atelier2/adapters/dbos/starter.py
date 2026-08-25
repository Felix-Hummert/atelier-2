from __future__ import annotations

from dataclasses import dataclass, replace
from typing import assert_never

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.advancer import first_agent_platform_effect_node
from atelier2.adapters.dbos.agent_catalog import (
    agent_configuration_from_record,
    auth_profile_from_record,
)
from atelier2.adapters.dbos.artifact_store import read_stored_artifact
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore, revision_owner
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.instants import record_run_started
from atelier2.adapters.dbos.names import QUEUE_NAME, WORKFLOW_NAME
from atelier2.adapters.dbos.node_records import persist_bound_node_executions
from atelier2.adapters.dbos.run_store import entry_node_of
from atelier2.adapters.dbos.run_transitions import (
    run_from_record,
    run_from_record_with_bindings,
)
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_configuration_revisions,
    auth_profile_revisions,
    published_revisions,
    run_agent_bindings,
    run_configuration_revisions,
    run_inputs_v3,
    runs,
    workflow_revisions,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow_ids import bootstrap_workflow_id_for
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.evaluate_executability import (
    DocumentNotExecutable,
    ExecutableDocument,
    evaluate_executability,
)
from atelier2.application.refusals import DurableStateCorrupt as RegistryCorrupt
from atelier2.application.refusals import ReadUnavailable as RegistryUnavailable
from atelier2.application.resolve_start_bindings import (
    AuthProfileMissingForConfiguration,
    agent_role_completeness_refusal,
    cast_unbound_roles,
    declared_agent_roles,
    resolve_start_bindings,
)
from atelier2.contracts.agents import (
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionHash,
    AuthProfileRevision,
    ResolvedAgentBinding,
)
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import OccupancyRevision
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.orders import ArtifactOrderValue, InlineOrderValue
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_bindings import RunV2, RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevision
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.schemas_v3 import (
    MAXIMUM_INSTANCE_DOCUMENT_BYTES,
    InstanceRefused,
    SchemaRefused,
    read_instance_document,
    read_schema_document,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflows import (
    WorkflowGraph,
    WorkflowGraphV2,
)
from atelier2.contracts.workflows_v3 import (
    AnyWorkflowDocument,
    WorkflowGraphV3,
)
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    AuthoredOrder,
    DurableAgentPlatformEffectUnreconcilable,
    DurableInvalidAgentBindings,
    DurablePublishedRunResult,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunFormatNotExecutable,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableStateCorrupt,
    DurableV3StartInputRefused,
    DurableWriteUnavailable,
    StartPublishedRunRequest,
    StartPublishedRunRequestV2,
    StartPublishedRunRequestV3,
    V3InputRefusal,
)
from atelier2.ports.host_configuration import HostConfigurationReadUnavailable
from atelier2.ports.workflow_revisions import (
    DurableRevisionCollision,
    DurableRevisionCreated,
    DurableRevisionExisting,
    DurableRevisionPublicationResult,
)


def _supplied_orders(request: AnyStartPublishedRunRequest) -> tuple[RunInput, ...]:
    """The orders this start carries, for a request shape that can carry any."""
    return request.run_inputs if isinstance(request, StartPublishedRunRequestV3) else ()


def _authored_orders(request: AnyStartPublishedRunRequest) -> tuple[AuthoredOrder, ...]:
    """The name-and-bytes form a caller can honestly supply."""
    return request.orders if isinstance(request, StartPublishedRunRequestV3) else ()


def _pin_authored_orders(
    connection: Connection,
    graph: WorkflowGraphV3,
    run_configuration: RunConfigurationRevision,
    authored: tuple[AuthoredOrder, ...],
) -> tuple[RunInput, ...] | DurableV3StartInputRefused:
    """Bind each authored order to the schema the document pinned, and to its bytes.

    A caller does not name a schema hash. Repeating the pin would make a typo
    a SCHEMA_MISMATCH instead of the order the operator meant.

    This is also where an order that names an artifact becomes an order that has
    bytes. It resolves here rather than anywhere later for the reason every other
    reference resolves at the start: what a run promised its author is settled
    before the run exists, so an address nobody published refuses the start by
    name instead of failing an attempt nobody could have foreseen.
    """
    declared = {entry.name for entry in graph.graph_inputs}
    names = [order.name for order in authored]
    if len(set(names)) != len(names):
        duplicated = next(
            name for index, name in enumerate(names) if name in names[:index]
        )
        return DurableV3StartInputRefused(
            duplicated,
            V3InputRefusal.DUPLICATED,
            "one name answers one order, and this start supplied it twice",
        )
    pinned_orders: list[RunInput] = []
    for order in authored:
        if order.name not in declared:
            return DurableV3StartInputRefused(order.name, V3InputRefusal.UNDECLARED)
        pinned = _resolved_graph_input_schema(run_configuration, order.name)
        if pinned is None:
            return DurableV3StartInputRefused(
                order.name,
                V3InputRefusal.SCHEMA_MISMATCH,
                "the document pinned nothing",
            )
        value = _order_value_bytes(connection, order)
        if isinstance(value, DurableV3StartInputRefused):
            return value
        pinned_orders.append(RunInput(order.name, pinned, value))
    return tuple(pinned_orders)


def _order_value_bytes(
    connection: Connection, order: AuthoredOrder
) -> bytes | DurableV3StartInputRefused:
    """The exact bytes one authored order is, whichever way it was supplied.

    The inline bound bites here rather than at the schema reading below, because
    it is a property of the route and not of the value: the same bytes are
    admitted when they arrive as an artifact somebody published, and the refusal
    an operator gets should say which door they were at.
    """
    match order.value:
        case InlineOrderValue(content):
            if len(content) > MAXIMUM_INSTANCE_DOCUMENT_BYTES:
                return DurableV3StartInputRefused(
                    order.name,
                    V3InputRefusal.VALUE_REFUSED,
                    f"{len(content)} inline bytes exceeds "
                    f"{MAXIMUM_INSTANCE_DOCUMENT_BYTES}; publish material this "
                    "large as an artifact and order its address",
                )
            return content
        case ArtifactOrderValue(artifact_hash):
            stored = read_stored_artifact(connection, artifact_hash)
            if stored is None:
                return DurableV3StartInputRefused(
                    order.name,
                    V3InputRefusal.UNKNOWN_ARTIFACT,
                    f"no artifact carries the address {artifact_hash.value}",
                )
            return stored.content
        case _ as unreachable:
            assert_never(unreachable)


def _refused_order(
    connection: Connection,
    graph: WorkflowGraphV3,
    run_configuration: RunConfigurationRevision,
    orders: tuple[RunInput, ...],
) -> DurableV3StartInputRefused | None:
    """The first order this start cannot honour, named by the input it is about.

    ADR 0006 binds a root run to every `graph_input` its document declares, and a
    missing one refuses the start naming the input. The same door refuses an order
    the document never declared, one whose schema is not the schema the document
    pinned, and a value that schema does not admit -- each before any row exists,
    because an order nobody could read is not a run to clean up.

    A schema that is not readable as a schema is not answered here. The reference
    that pins it was already resolved to build the configuration this reads, and
    that resolution refuses unusable schema bytes at the document -- so reaching
    this point means the pinned schema is one this product enforces.
    """
    declared = {entry.name: entry for entry in graph.graph_inputs}
    supplied = {order.name: order for order in orders}
    if len(supplied) != len(orders):
        duplicated = next(
            order.name
            for index, order in enumerate(orders)
            if order.name in {other.name for other in orders[:index]}
        )
        return DurableV3StartInputRefused(
            duplicated,
            V3InputRefusal.DUPLICATED,
            "one name answers one order, and this start supplied it twice",
        )
    for name in declared:
        if name not in supplied:
            return DurableV3StartInputRefused(name, V3InputRefusal.MISSING)
    for name, order in supplied.items():
        if name not in declared:
            return DurableV3StartInputRefused(name, V3InputRefusal.UNDECLARED)
        pinned = _resolved_graph_input_schema(run_configuration, name)
        if pinned != order.schema_revision:
            return DurableV3StartInputRefused(
                name,
                V3InputRefusal.SCHEMA_MISMATCH,
                f"the document pinned {'nothing' if pinned is None else pinned.value}",
            )
        document = connection.scalar(
            sa.select(published_revisions.c.document).where(
                published_revisions.c.kind == RevisionKind.SCHEMA.value,
                published_revisions.c.revision_hash == order.schema_revision.value,
            )
        )
        if document is None:
            raise RuntimeError("a resolved schema revision is absent from the store")
        match read_schema_document(bytes(document)):
            case SchemaRefused() as unreadable:
                # The reference that pins this schema resolved before the run
                # configuration was built, and that resolution reads the bytes.
                # Reaching this means the store answered differently twice.
                raise RuntimeError(
                    f"a resolved schema revision is not one: {unreadable}"
                )
            case schema:
                # The value is judged as it will be read, which for an ordered
                # artifact is its full content: the route it arrived by already
                # bounded it, and refusing it a second time under the inline
                # bound would refuse what the artifact door admitted.
                verdict = read_instance_document(
                    order.value, schema, MAXIMUM_ARTIFACT_BYTES
                )
        if isinstance(verdict, InstanceRefused):
            return DurableV3StartInputRefused(
                name, V3InputRefusal.VALUE_REFUSED, str(verdict), verdict.violation
            )
    return None


def _requested_orders(orders: tuple[RunInput, ...]) -> tuple[tuple[str, str, str], ...]:
    """The named order set a start asks for, as durable identity reads it.

    Keyed by name rather than by arrival, because the caller's sequence is not
    what the run keeps: `run_inputs_v3` has no position column on purpose, so two
    starts that supply the same orders in different sequences are the same run.
    """
    return tuple(
        sorted(
            (order.name, order.schema_revision.value, order.value_hash.value)
            for order in orders
        )
    )


def _stored_orders(
    connection: Connection, run_id: RunId
) -> tuple[tuple[str, str, str], ...]:
    """The named order set this run already carries, read the same way."""
    return tuple(
        sorted(
            (
                str(record["name"]),
                str(record["schema_revision_hash"]),
                str(record["value_hash"]),
            )
            for record in connection.execute(
                sa.select(run_inputs_v3).where(run_inputs_v3.c.run_id == run_id.value)
            ).mappings()
        )
    )


def _resolved_graph_input_schema(
    run_configuration: RunConfigurationRevision, name: str
) -> PublishedRevisionHash | None:
    """The schema revision the document's own resolution pinned for one order."""
    for resolved in run_configuration.resolutions:
        site = resolved.site
        if site.field == "graph_inputs.schema" and site.entry == name:
            return resolved.revision_hash
    return None


@dataclass(frozen=True)
class _TransactionAgentConfigurationReads:
    """Binding reads through the write transaction's own open connection.

    `resolve_start_bindings` never opens a connection: a start's binding
    decision reads through the exact connection its serialized write
    transaction already holds -- the same locks, the same snapshot -- rather
    than a second read path with its own visibility. The row mappers are the
    same ones `DbosAgentConfigurationCatalog` reads with; only the connection
    differs.
    """

    connection: Connection

    def agent_configuration_revision(
        self, revision_hash: AgentConfigurationRevisionHash
    ) -> tuple[AgentConfigurationRevision, AuthProfileRevision] | None:
        configuration_record = (
            self.connection.execute(
                sa.select(agent_configuration_revisions).where(
                    agent_configuration_revisions.c.revision_hash == revision_hash.value
                )
            )
            .mappings()
            .one_or_none()
        )
        if configuration_record is None:
            return None
        configuration = agent_configuration_from_record(configuration_record)
        auth_record = (
            self.connection.execute(
                sa.select(auth_profile_revisions).where(
                    auth_profile_revisions.c.revision_hash
                    == configuration.auth_profile_revision_hash.value
                )
            )
            .mappings()
            .one_or_none()
        )
        if auth_record is None:
            raise AuthProfileMissingForConfiguration(
                configuration.auth_profile_revision_hash
            )
        return configuration, auth_profile_from_record(auth_record)


class DbosDurableRunStarter:
    def __init__(
        self,
        engine: Engine,
        settings: DbosRuntimeSettings,
        agent_executor_registry: AgentExecutorRegistry,
        effect_adapter_proves_absence: bool,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._agent_executor_registry = agent_executor_registry
        self._published_revisions = DbosCatalogStore(engine)
        self._host_configuration = DbosHostConfigurationChannel(engine)
        # Whether the deployment's composed effect adapter answers a not-found
        # readback with an authoritative absence. When it cannot, an agent-
        # authored `open-pr` grant has no safe reconciliation path and is
        # refused at admission (`#430`/`#431`); the Action path is unaffected.
        #
        # This is required, not defaulted: every construction site must pass the
        # composed adapter's own answer, so no start path -- the API route or the
        # queue auto-start -- can begin a run against a non-absence-proving adapter
        # without the admission guard reading the real value.
        self._effect_adapter_proves_absence = effect_adapter_proves_absence

    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult:
        """Start one published revision the runtime can execute end to end.

        A V3 revision starts here like any other, because the runtime now drives
        one: the attempt path binds a V3 agent node (#194 H1c), the terminal
        condition belongs to the run rather than a subworkflow node (H1b), and a
        node hands on to the heir its author declared (H2). While none of that
        existed, this seam refused V3 by document family and a separate internal
        foundation seam wrote the run without enqueueing anything; both are gone,
        because a second door is only honest while the first one cannot open.

        What refuses an unexecutable V3 document is one layer down and always
        was: `parse_executable_workflow_document` names what the document still
        waits for -- an uninterpreted node kind, a branch nothing chooses between
        -- before this seam is consulted, so admitting the family here does not
        admit a graph the driver would stall on.
        """
        return self._start(request)

    def _cast_against_occupancy(
        self, request: AnyStartPublishedRunRequest, graph: AnyWorkflowDocument
    ) -> AnyStartPublishedRunRequest | DurableWriteUnavailable | DurableStateCorrupt:
        """The same start, with roles nobody bound filled from the served project.

        The occupancy is what the operator cast in the console, and until now
        only the manual start page read it: a caller that names no binding --
        the conductor's start, the queue's auto-start -- was refused for a
        matrix the project had already answered. It is decided here rather than
        at each caller because this is where the document's roles are known and
        where the run's binding-set hash is about to be frozen. A deployment
        serving no project reads nothing and starts exactly what it was handed.
        """
        project_id = self._settings.project_id
        if project_id is None or not isinstance(
            graph, (WorkflowGraphV2, WorkflowGraphV3)
        ):
            return request
        requested = (
            request.agent_bindings
            if isinstance(
                request, (StartPublishedRunRequestV2, StartPublishedRunRequestV3)
            )
            else AgentBindingSet(())
        )
        bound = {binding.role.value for binding in requested.bindings}
        if declared_agent_roles(graph) <= bound:
            return request
        with self._engine.connect() as connection:
            lineage_id = revision_owner(
                connection,
                RevisionKind.WORKFLOW,
                PublishedRevisionHash(request.revision_hash.value),
            )
        if lineage_id is None:
            return request
        latest = self._host_configuration.latest_occupancy_revision(
            project_id, lineage_id
        )
        match latest:
            case OccupancyRevision() | None:
                cast = cast_unbound_roles(graph, requested, latest)
            case HostConfigurationReadUnavailable():
                return DurableWriteUnavailable()
            case DurableStateCorrupt():
                return DurableStateCorrupt()
            case _ as unreachable:
                assert_never(unreachable)
        if cast == requested:
            return request
        if isinstance(request, StartPublishedRunRequest):
            return StartPublishedRunRequestV2(
                request.run_id, request.revision_hash, cast
            )
        return replace(request, agent_bindings=cast)

    def _start(
        self,
        request: AnyStartPublishedRunRequest,
    ) -> DurablePublishedRunResult:
        try:
            with self._engine.connect() as read_connection:
                document = read_connection.scalar(
                    sa.select(workflow_revisions.c.document).where(
                        workflow_revisions.c.revision_hash
                        == request.revision_hash.value
                    )
                )
            if document is None:
                return DurableRunRevisionMissing()
            revision_document = bytes(document)
            revision = WorkflowRevision(revision_document)
            if revision.revision_hash != request.revision_hash:
                return DurableStateCorrupt()
            graph = parse_workflow_document(revision.document)
            executability = evaluate_executability(graph, self._published_revisions)
            match executability:
                case ExecutableDocument():
                    pass
                case DocumentNotExecutable():
                    return DurableRunFormatNotExecutable()
                case RegistryUnavailable():
                    return DurableWriteUnavailable()
                case RegistryCorrupt():
                    return DurableStateCorrupt()
                case _ as unreachable:
                    assert_never(unreachable)
            cast = self._cast_against_occupancy(request, graph)
            if isinstance(cast, (DurableWriteUnavailable, DurableStateCorrupt)):
                return cast
            request = cast
            run_configuration: RunConfigurationRevision | None = None
            if isinstance(graph, WorkflowGraphV3):
                if not isinstance(
                    request, (StartPublishedRunRequestV2, StartPublishedRunRequestV3)
                ):
                    return DurableInvalidAgentBindings()
                run_configuration = RunConfigurationRevision(
                    WorkflowRevisionHash(revision.revision_hash.value),
                    request.agent_bindings.binding_set_hash,
                    executability.resolutions,
                )
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

        client: DBOSClient | None = None
        try:
            client = DBOSClient(
                system_database_engine=self._engine, use_listen_notify=False
            )
            with canonical_write_transaction(self._engine) as connection:
                stored_document = connection.scalar(
                    sa.select(workflow_revisions.c.document).where(
                        workflow_revisions.c.revision_hash
                        == request.revision_hash.value
                    )
                )
                if (
                    stored_document is None
                    or bytes(stored_document) != revision_document
                ):
                    raise RuntimeError(
                        "published revision changed between parse and serialized start"
                    )
                stored_revision = WorkflowRevision(bytes(stored_document))
                if stored_revision.revision_hash != request.revision_hash:
                    raise RuntimeError(
                        "published revision bytes disagree with their hash"
                    )
                # Before any row: a graph whose agent node carries an `open-pr`
                # grant cannot be admitted against an effect adapter that cannot
                # prove absence, because that redemption runs after the attempt
                # already succeeded and has no Action-only WAITING_RECONCILIATION
                # resting place (`#430`/`#431`). Read through this serialized
                # connection so it sees the same tool revisions every other
                # admission read does.
                if not self._effect_adapter_proves_absence:
                    unreconcilable = first_agent_platform_effect_node(connection, graph)
                    if unreconcilable is not None:
                        return DurableAgentPlatformEffectUnreconcilable(unreconcilable)
                if isinstance(graph, WorkflowGraph):
                    if not isinstance(request, StartPublishedRunRequest):
                        return DurableInvalidAgentBindings()
                    resolved_bindings: tuple[ResolvedAgentBinding, ...] = ()
                    binding_set: AgentBindingSet | None = None
                elif isinstance(graph, (WorkflowGraphV2, WorkflowGraphV3)):
                    # V3's Agent kind binds its role exactly as V2's does, so the
                    # resolution below is shared rather than copied. What differs
                    # is only where the run starts, which `entry_node_of` answers.
                    if not isinstance(
                        request,
                        (StartPublishedRunRequestV2, StartPublishedRunRequestV3),
                    ):
                        return DurableInvalidAgentBindings()
                    # The role check runs before the retry check below reads
                    # anything, so a request whose roles are wrong is refused
                    # by that alone -- the same precedence an existing but
                    # mismatched run gets from the retry check that follows.
                    role_refusal = agent_role_completeness_refusal(
                        graph, request.agent_bindings
                    )
                    if role_refusal is not None:
                        return role_refusal
                    binding_set = request.agent_bindings
                    existing_record = (
                        connection.execute(
                            sa.select(runs).where(runs.c.run_id == request.run_id.value)
                        )
                        .mappings()
                        .one_or_none()
                    )
                    # Authored orders are not `run_inputs` yet. Comparing them
                    # here would treat every honest retry as a different order.
                    # Those starts fall through, pin, and use the compare below.
                    if existing_record is not None and not _authored_orders(request):
                        if (
                            str(existing_record["revision_hash"])
                            != request.revision_hash.value
                            or WorkflowFormatVersion(
                                int(existing_record["workflow_format_version"])
                            )
                            != graph.format_version
                            or str(existing_record["agent_binding_set_hash"])
                            != binding_set.binding_set_hash.value
                            or _stored_orders(connection, request.run_id)
                            != _requested_orders(_supplied_orders(request))
                        ):
                            return DurableRunIdentityConflict()
                        return DurableRunExisting(
                            run_from_record_with_bindings(connection, existing_record)
                        )
                    bindings_result = resolve_start_bindings(
                        graph,
                        binding_set,
                        _TransactionAgentConfigurationReads(connection),
                        self._agent_executor_registry,
                    )
                    if not isinstance(bindings_result, tuple):
                        return bindings_result
                    resolved_bindings = bindings_result
                else:
                    assert_never(graph)

                authored = _authored_orders(request)
                stored = _supplied_orders(request)
                if authored and stored:
                    raise RuntimeError("a start names its orders once")
                if (
                    authored
                    and run_configuration is not None
                    and isinstance(graph, WorkflowGraphV3)
                ):
                    pinned = _pin_authored_orders(
                        connection, graph, run_configuration, authored
                    )
                    if isinstance(pinned, DurableV3StartInputRefused):
                        return pinned
                    orders = pinned
                else:
                    orders = stored
                if run_configuration is not None and isinstance(graph, WorkflowGraphV3):
                    # Inside the serialized transaction and before the first row,
                    # so a refused order leaves no run, no configuration and no
                    # enqueue behind rather than a start to clean up.
                    refused = _refused_order(
                        connection, graph, run_configuration, orders
                    )
                    if refused is not None:
                        return refused
                elif orders:
                    return DurableInvalidAgentBindings()

                workflow_id = bootstrap_workflow_id_for(request.run_id)
                if run_configuration is not None:
                    connection.execute(
                        run_configuration_revisions.insert()
                        .prefix_with("OR IGNORE")
                        .values(
                            revision_hash=run_configuration.revision_hash.value,
                            preimage=run_configuration.preimage,
                        )
                    )
                inserted = connection.execute(
                    runs.insert()
                    .prefix_with("OR IGNORE")
                    .values(
                        run_id=request.run_id.value,
                        bootstrap_workflow_id=workflow_id,
                        revision_hash=request.revision_hash.value,
                        workflow_format_version=graph.format_version,
                        agent_binding_set_hash=(
                            None
                            if binding_set is None
                            else binding_set.binding_set_hash.value
                        ),
                        current_node_id=entry_node_of(graph),
                        current_round_ordinal=FIRST_ROUND_ORDINAL,
                        state=RunState.STARTED.value,
                        state_version=0,
                        last_event_sequence=0,
                        terminal_hash=None,
                        run_configuration_revision_hash=(
                            None
                            if run_configuration is None
                            else run_configuration.revision_hash.value
                        ),
                    )
                )
                existing_record = (
                    connection.execute(
                        sa.select(runs).where(runs.c.run_id == request.run_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing_record is None:
                    raise RuntimeError("inserted run is not readable")
                if inserted.rowcount == 1:
                    record_run_started(connection, request.run_id.value)
                if isinstance(graph, WorkflowGraph):
                    run = run_from_record(existing_record)
                else:
                    # The shape follows the exact graph version, the same rule
                    # `run_from_record_with_bindings` applies when a retry reads
                    # this row back. Constructing `RunV2` for every bound graph
                    # meant a first start answered V2 while a retry answered V3
                    # for one row. It is built here rather than read back because
                    # the binding rows below are not written yet.
                    assert binding_set is not None
                    terminal_hash = existing_record["terminal_hash"]
                    head = (
                        request.run_id,
                        request.revision_hash,
                        binding_set.binding_set_hash,
                        resolved_bindings,
                        RunState(str(existing_record["state"])),
                        str(existing_record["current_node_id"]),
                        int(existing_record["state_version"]),
                        int(existing_record["last_event_sequence"]),
                    )
                    ended = (
                        None
                        if terminal_hash is None
                        else Sha256Hash(str(terminal_hash))
                    )
                    if isinstance(graph, WorkflowGraphV2):
                        run = RunV2(*head, ended)
                    else:
                        # A V3 graph reached this seam, so the configuration was
                        # bound above; the type refuses a V3 run without it.
                        assert run_configuration is not None
                        run = RunV3(*head, run_configuration.revision_hash, ended)
                if inserted.rowcount == 0:
                    existing_set = existing_record["agent_binding_set_hash"]
                    requested_set = (
                        None
                        if binding_set is None
                        else binding_set.binding_set_hash.value
                    )
                    if (
                        run.revision_hash != request.revision_hash
                        or WorkflowFormatVersion(
                            int(existing_record["workflow_format_version"])
                        )
                        != graph.format_version
                        or existing_set != requested_set
                        or _stored_orders(connection, request.run_id)
                        != _requested_orders(orders)
                    ):
                        return DurableRunIdentityConflict()
                    return DurableRunExisting(run)
                if binding_set is not None and binding_set.bindings:
                    connection.execute(
                        run_agent_bindings.insert(),
                        [
                            {
                                "run_id": request.run_id.value,
                                "revision_hash": request.revision_hash.value,
                                "binding_set_hash": binding_set.binding_set_hash.value,
                                "role": binding.role.value,
                                "agent_configuration_revision_hash": (
                                    binding.agent_configuration_revision_hash.value
                                ),
                            }
                            for binding in binding_set.bindings
                        ],
                    )
                if orders:
                    # Written beside the run rather than into it: the same
                    # published revision serves every order, so the order belongs
                    # to this run and the document belongs to all of them.
                    connection.execute(
                        run_inputs_v3.insert(),
                        [
                            {
                                "run_id": request.run_id.value,
                                "name": order.name,
                                "schema_revision_hash": order.schema_revision.value,
                                "value": order.value,
                                "value_hash": order.value_hash.value,
                            }
                            for order in orders
                        ],
                    )
                if run_configuration is not None and isinstance(graph, WorkflowGraphV3):
                    # After the orders, because an order this run carries is a
                    # member of the package that binds it -- the content hash a
                    # declared reference cannot produce and material can.
                    persist_bound_node_executions(
                        connection,
                        request.run_id,
                        WorkflowRevisionHash(request.revision_hash.value),
                        graph,
                        run_configuration,
                        orders,
                    )
                options: EnqueueOptions = {
                    "workflow_name": WORKFLOW_NAME,
                    "queue_name": QUEUE_NAME,
                    "workflow_id": workflow_id,
                    "app_version": self._settings.application_version,
                }
                client.enqueue_in_transaction(
                    connection,
                    options,
                    request.run_id.value,
                    request.revision_hash.value,
                )
                return DurableRunCreated(run)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (
            AuthProfileMissingForConfiguration,
            ValueError,
            RuntimeError,
            DatabaseError,
        ):
            return DurableStateCorrupt()
        finally:
            if client is not None:
                client.destroy()


class DbosWorkflowRevisionPublisher:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def publish(self, revision: WorkflowRevision) -> DurableRevisionPublicationResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                inserted = connection.execute(
                    workflow_revisions.insert()
                    .prefix_with("OR IGNORE")
                    .values(
                        revision_hash=revision.revision_hash.value,
                        document=revision.document,
                    )
                )
                stored = connection.scalar(
                    sa.select(workflow_revisions.c.document).where(
                        workflow_revisions.c.revision_hash
                        == revision.revision_hash.value
                    )
                )
                if stored is None:
                    raise RuntimeError("inserted workflow revision is not readable")
                durable = WorkflowRevision(bytes(stored))
                if durable.revision_hash != revision.revision_hash:
                    return DurableStateCorrupt()
                if durable.document != revision.document:
                    return DurableRevisionCollision()
                if inserted.rowcount == 1:
                    return DurableRevisionCreated(durable)
                return DurableRevisionExisting(durable)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
