<script lang="ts">
  import { onMount, tick } from "svelte";
  import { SvelteSet } from "svelte/reactivity";

  import {
    CockpitRequestError,
    decodeCanonicalBase64,
    type AgentConfigurationRevisionListItem,
    type AuthProfileInput,
    type AuthProfileRevision,
    type CockpitApi,
    type InvalidField,
    type ModelRegistryRevision,
    type ProjectModelResolution,
    type Problem,
    type WorkflowRevisionDetail,
    type WorkflowRevisionSummary
  } from "../api/client";
  import BackLink from "../components/BackLink.svelte";
  import { WORKSHOP_DESTINATION } from "../lib/workshop";
  import InfoHint from "../components/InfoHint.svelte";
  import OrderEditor from "../components/OrderEditor.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import ProofAnchor from "../components/ProofAnchor.svelte";
  import { shortFingerprint } from "../lib/fingerprint";
  import ReadState from "../components/ReadState.svelte";
  import WorkflowGraphDrawing from "../components/WorkflowGraphDrawing.svelte";
  import {
    encodeSingleFieldOrder,
    preValidateOrderValue,
    summarizeOrderSchema,
    type OrderSchemaReadFailure,
    type OrderSchemaResource
  } from "../lib/orderSchema";
  import {
    MutationJournal,
    createRunId as makeRunId,
    publicationMutation,
    requestedStartAgentBindings,
    startMutation,
    startMutationV2,
    startMutationV3,
    type JournalEntry,
    type PublishMutation,
    type StartMutation
  } from "../lib/mutationJournal";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    type RetainedRead
  } from "../lib/readResource";
  import {
    readEveryAgentConfiguration,
    readEveryAuthProfile,
    readEveryRevision
  } from "../lib/runPages";
  import { cannotBeStarted, humanErrorMessage } from "../lib/humanRefusal";
  import {
    admitPublishedRevision,
    catalogActivatedAt,
    COCKPIT_CATALOG_ACTOR
  } from "../lib/catalogAdmission";
  import {
    catalogNameStateOf,
    catalogHeadsOf,
    problemCode,
    type CatalogNameState
  } from "../lib/catalogName";
  import {
    groupSavedWorkflows,
    agentRolesOf,
    revisionChoiceLabel,
    selectedRevisionOf,
    type SavedWorkflowRow
  } from "../lib/savedWorkflows";

  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let navigate: (path: string) => void;
  export let createRunId: () => string = makeRunId;

  /** Starting is the Catalog's own act now that the Workflows room is gone. */
  const catalogRoom = WORKSHOP_DESTINATION.catalog;

  type BindingSource = "looking" | "project" | "next-higher" | "workflow" | "chosen" | "choose" | "unavailable";

  type ModelOverride = { role: string; agent_configuration_revision_hash: string };

  interface BindingDraft {
    role: string;
    selectedHash: string;
    source: BindingSource;
    manualOverrideHash: string | null;
    expertOverrideDrafted: boolean;
    profileId: string;
    revisionNumber: string;
    providerId: string;
    authMode: "" | AuthProfileInput["auth_mode"];
    model: string;
    executorRevision: string;
    resolutionHint: string | null;
    error: string | null;
  }

  interface OrderDraft {
    name: string;
    schema: { ref: string; revision: string };
    value: string;
    error: string | null;
    fieldErrors: readonly InvalidField[];
    schemaRead: RetainedRead<OrderSchemaResource, OrderSchemaReadFailure>;
  }

  interface RunDraft {
    revision: WorkflowRevisionDetail;
    lineageId: string | null;
    runId: string;
    bindings: BindingDraft[];
    orders: OrderDraft[];
  }

  interface SavedWorkflowSnapshot {
    items: WorkflowRevisionSummary[];
    newestByName: Record<string, string>;
    catalogByName: Record<string, CatalogNameState>;
  }

  type WorkflowDetailIntent =
    | { kind: "details"; rowKey: string; revisionHash: string }
    | { kind: "edit"; rowKey: string; revisionHash: string }
    | {
        kind: "select";
        rowKey: string;
        revisionHash: string;
        chooseRow: boolean;
        lineageId: string | null;
      };

  interface WorkflowDetailResource {
    read: RetainedRead<WorkflowRevisionDetail, ReadFailure>;
    intent: WorkflowDetailIntent;
  }

  type ReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  interface ProjectModelSnapshot {
    workflowRevisionHash: string;
    overrideKey: string;
    resolution: ProjectModelResolution;
    bindings: Map<string, ProjectModelResolution["resolutions"][number]>;
  }

  /** A selectable model is the registry's exact model id attached to its account. */
  interface RegisteredConfiguration {
    configuration: AgentConfigurationRevisionListItem;
    modelId: string;
    accountId: string;
  }

  let revisions: RetainedRead<SavedWorkflowSnapshot, ReadFailure> =
    retainedRead<SavedWorkflowSnapshot, ReadFailure>();
  let configurations: RetainedRead<AgentConfigurationRevisionListItem[], ReadFailure> =
    retainedRead<AgentConfigurationRevisionListItem[], ReadFailure>();
  let projectModels: RetainedRead<ProjectModelSnapshot, ReadFailure> =
    retainedRead<ProjectModelSnapshot, ReadFailure>();
  let activeModelWorkflowRevisionHash: string | null = null;
  let modelDraftGeneration = 0;
  let publishedConfigurations: AgentConfigurationRevisionListItem[] = [];
  let registeredConfigurations: RegisteredConfiguration[] = [];
  let mode: "saved" | "publish" = "saved";
  let exactYaml = "";
  let draft: RunDraft | null = null;
  let failureMessage: string | null = null;
  let publicationOpen = false;
  let publicationTrigger: HTMLButtonElement;
  let publicationDialog: HTMLDivElement;
  let pending: JournalEntry[] = [];
  let operation: "publish" | "start" | "retry" | null = null;
  $: busy = operation !== null;
  let selectedHashByKey: Record<string, string> = {};
  let chosenRowKey: string | null = null;
  $: newestByName = revisions.confirmed?.newestByName ?? {};
  $: catalogByName = revisions.confirmed?.catalogByName ?? {};
  $: savedRows = groupSavedWorkflows(revisions.confirmed?.items ?? [], newestByName);
  $: publishedConfigurations = configurations.confirmed ?? [];
  $: draftHasUnavailableBinding =
    draft !== null && draft.bindings.some((binding) => bindingHasUnavailableExecutor(binding));
  $: correctingOrder = draft?.orders.some(
    (order) => order.error !== null || order.fieldErrors.length > 0
  ) ?? false;
  $: visibleRows =
    chosenRowKey === null
      ? savedRows
      : savedRows.filter((row) => row.key === chosenRowKey);

  /**
   * Whether this cockpit can carry a run of that revision.
   *
   * It asks one thing: can the server execute this document. The picker used to
   * ask a second -- whether this cockpit could draw the run -- and refused every
   * version 3 revision on that ground. The run page reads one now, so the extra
   * condition is gone and this reads what it enforces.
   */
  function cockpitCanShow(revision: WorkflowRevisionSummary): boolean {
    return revision.executable;
  }

  function setRowRevision(row: SavedWorkflowRow, revisionHash: string): void {
    const revision = row.revisions.find(
      (candidate) => candidate.workflow_revision_hash === revisionHash
    );
    void requestWorkflowDetail({
      kind: "select",
      rowKey: row.key,
      revisionHash,
      chooseRow: chosenRowKey === row.key,
      lineageId: revision === undefined ? null : catalogLineageOf(revision)
    });
  }

  function catalogLineageOf(revision: WorkflowRevisionSummary): string | null {
    if (revision.name === null) return null;
    const state = catalogByName[revision.name];
    return state?.kind === "admitted" ? state.lineageId : null;
  }

  async function resolveCatalogNames(
    items: readonly WorkflowRevisionSummary[]
  ): Promise<{
    newestByName: Record<string, string>;
    catalogByName: Record<string, CatalogNameState>;
  } | null> {
    const states: Record<string, CatalogNameState> = {};
    const names = [
      ...new Set(
        items.flatMap((item) => (item.name === null ? [] : [item.name]))
      )
    ];
    const catalog = await Promise.all(
      names.map(async (name) => {
        const state = await catalogNameStateOf(name, (asked) =>
          cockpitApi.getRevisionByName(asked)
        );
        return { name, state };
      })
    );
    for (const { name, state } of catalog) {
      states[name] = state;
    }
    const newestByName = catalogHeadsOf(items, states);
    return newestByName === null ? null : { newestByName, catalogByName: states };
  }

  function catalogStateLabel(state: CatalogNameState | undefined): string | null {
    if (state === undefined || state.kind === "admitted") return null;
    if (state.kind === "unlisted") return "Unlisted";
    if (state.kind === "unnamable") return "Unnamable";
    return "Retired";
  }

  function catalogStateHint(state: CatalogNameState | undefined): string | null {
    if (state === undefined || state.kind === "admitted") return null;
    if (state.kind === "unlisted") return "This published name is not a catalog member.";
    if (state.kind === "unnamable") {
      return "This published title cannot be a catalog name.";
    }
    return "This catalog name was retired.";
  }

  function catalogFormOf(
    revision: WorkflowRevisionSummary,
    state: CatalogNameState | undefined
  ): "ready" | "unlisted" | "unnamable" | "retired" | "refused" {
    if (!cockpitCanShow(revision)) return "refused";
    if (state === undefined || state.kind === "admitted") return "ready";
    return state.kind;
  }

  function changeChosenWorkflow(): void {
    chosenRowKey = null;
    draft = null;
    clearProjectModels();
    activeWorkflowDetailHash = null;
    failureMessage = null;
    editingHash = null;
    editYaml = null;
  }

  function changeWorkflowSource(): void {
    activeWorkflowDetailHash = null;
    draft = null;
    clearProjectModels();
    failureMessage = null;
    chosenRowKey = null;
    editingHash = null;
    editYaml = null;
  }

  onMount(async () => {
    await Promise.all([loadRevisions(), loadConfigurations(), loadPending()]);
  });

  async function loadRevisions(): Promise<void> {
    const begun = beginRead(revisions);
    revisions = begun.read;
    try {
      const reading = await readEveryRevision((after) => cockpitApi.listWorkflowRevisions(after));
      if (!reading.complete) {
        revisions = failRead(revisions, begun.generation, {
          kind: "incomplete",
          title: "Saved workflows incomplete"
        });
        return;
      }
      const catalog = await resolveCatalogNames(reading.revisions);
      if (catalog === null) {
        revisions = failRead(revisions, begun.generation, {
          kind: "unavailable",
          title: "Saved workflows unavailable"
        });
        return;
      }
      revisions = confirmRead(revisions, begun.generation, {
        items: reading.revisions,
        newestByName: catalog.newestByName,
        catalogByName: catalog.catalogByName
      });
    } catch {
      revisions = failRead(revisions, begun.generation, {
        kind: "unavailable",
        title: "Saved workflows unavailable"
      });
    }
  }

  async function loadConfigurations(): Promise<void> {
    const begun = beginRead(configurations);
    configurations = begun.read;
    registeredConfigurations = [];
    applyBindingRecommendations();
    try {
      const reading = await readEveryAgentConfiguration((after) =>
        cockpitApi.listAgentConfigurationRevisions(after)
      );
      if (!reading.complete) {
        configurations = failRead(configurations, begun.generation, {
          kind: "incomplete",
          title: "Published agents incomplete"
        });
        applyBindingRecommendations();
        return;
      }
      registeredConfigurations = await registeredConfigurationsOf(reading.configurations);
      const confirmed = confirmRead(configurations, begun.generation, reading.configurations);
      configurations = confirmed;
      if (confirmed.generation === begun.generation) {
        applyBindingRecommendations();
      }
    } catch {
      registeredConfigurations = [];
      configurations = failRead(configurations, begun.generation, {
        kind: "unavailable",
        title: "Published agents unavailable"
      });
      applyBindingRecommendations();
    }
  }

  async function registeredConfigurationsOf(
    available: readonly AgentConfigurationRevisionListItem[]
  ): Promise<RegisteredConfiguration[]> {
    if (available.length === 0) return [];
    const profileReading = await readEveryAuthProfile((after) =>
      cockpitApi.listAuthProfileRevisions(after)
    );
    if (!profileReading.complete) throw new Error("account listing incomplete");
    const registries = await registriesFor(available);
    return registeredConfigurationsFrom(available, profileReading.profiles, registries);
  }

  async function registriesFor(
    available: readonly AgentConfigurationRevisionListItem[]
  ): Promise<ModelRegistryRevision[]> {
    const providers = [...new Set(available.map((configuration) => configuration.provider_id))]
      .sort();
    const registries = await Promise.all(providers.map(async (providerId) => {
      try {
        return await cockpitApi.getModelRegistry(providerId);
      } catch (error) {
        if (problemCode(error) === "model-registry-missing") return null;
        throw error;
      }
    }));
    return registries.filter((registry): registry is ModelRegistryRevision => registry !== null);
  }

  function registeredConfigurationsFrom(
    available: readonly AgentConfigurationRevisionListItem[],
    profiles: readonly AuthProfileRevision[],
    registries: readonly ModelRegistryRevision[]
  ): RegisteredConfiguration[] {
    const selected: RegisteredConfiguration[] = [];
    const included = new SvelteSet<string>();
    for (const registry of registries) {
      for (const entry of registry.entries) {
        if (entry.provider_check !== "checked") continue;
        const configuration = available.find(
          (candidate) =>
            candidate.agent_configuration_revision_hash === entry.agent_configuration_revision_hash
        );
        const profile = configuration === undefined
          ? undefined
          : profiles.find(
              (candidate) =>
                candidate.auth_profile_revision_hash === configuration.auth_profile_revision_hash
            );
        if (configuration === undefined || profile === undefined || included.has(
          configuration.agent_configuration_revision_hash
        )) continue;
        included.add(configuration.agent_configuration_revision_hash);
        selected.push({
          configuration,
          modelId: entry.model_id,
          accountId: profile.profile_id
        });
      }
    }
    return selected;
  }

  async function loadPending(): Promise<void> {
    try {
      pending = (await mutationJournal.entries()).filter(
        (entry) => entry.kind === "publish" || entry.kind === "start"
      );
    } catch (error) {
      showFailure(error, "The saved exact requests could not be read.");
    }
  }

  async function reviewPublication(document = exactYaml): Promise<void> {
    failureMessage = null;
    if (document.length === 0) {
      failureMessage = "Enter the exact workflow YAML before publishing.";
      return;
    }
    publicationDocument = document;
    publicationOpen = true;
    await tick();
    publicationDialog.focus();
  }

  async function closePublication(): Promise<void> {
    publicationOpen = false;
    await tick();
    publicationTrigger?.focus();
  }

  async function confirmPublication(): Promise<void> {
    publicationOpen = false;
    operation = "publish";
    failureMessage = null;
    let prepared: PublishMutation | null = null;
    try {
      prepared = await publicationMutation(publicationDocument);
      await mutationJournal.prepare(prepared);
      await deliverPublication(prepared);
    } catch (error) {
      if (prepared !== null) await recordDeliveryFailure(prepared.mutation_id, error);
      showFailure(error, "The workflow could not be published.");
    } finally {
      operation = null;
      await loadPending();
    }
  }

  /**
   * Whether a run of this revision is started by binding agent roles.
   *
   * Version 2 and version 3 both are, through the same bound start request; a
   * version 1 document names no roles at all. Asking that rather than asking for
   * a version number is what let version 3 through here: the three places below
   * were each written as "is this version 2", and each was really asking this.
   */
  function bindsAgentRoles(graph: WorkflowRevisionDetail["graph"]): boolean {
    return graph.workflow_format_version === 2 || graph.workflow_format_version === 3;
  }

  /**
   * Every agent role this revision declares, once each.
   *
   * A version 3 revision answers with them directly; a version 2 one is read out
   * of the nodes it puts on the wire; a version 1 one declares none. This is a
   * different question from `bindsAgentRoles` above and stays its own: a version 2
   * document with no agent node still starts through the bound request, so "which
   * roles" and "which start request" must not be collapsed into one answer.
   */
  let workflowDetails: Record<string, WorkflowDetailResource> = {};
  let activeWorkflowDetailHash: string | null = null;
  let editingHash: string | null = null;
  let editYaml: string | null = null;
  let publicationDocument = "";

  function publishedNodeCount(graph: WorkflowRevisionDetail["graph"] | undefined): number | null {
    return graph?.workflow_format_version === 3 ? graph.node_count : null;
  }

  function publishedNodePreviews(
    graph: WorkflowRevisionDetail["graph"] | undefined
  ): Extract<WorkflowRevisionDetail["graph"], { workflow_format_version: 3 }>["node_previews"] | null {
    return graph?.workflow_format_version === 3 ? graph.node_previews : null;
  }

  function publishedLoops(
    graph: WorkflowRevisionDetail["graph"] | undefined
  ): Extract<WorkflowRevisionDetail["graph"], { workflow_format_version: 3 }>["loops"] {
    return graph?.workflow_format_version === 3 ? graph.loops : [];
  }

  function publishedAgentRoles(graph: WorkflowRevisionDetail["graph"] | undefined): string[] | null {
    return graph?.workflow_format_version === 3 ? [...graph.agent_roles] : null;
  }

  function publishedOrders(
    graph: WorkflowRevisionDetail["graph"] | undefined
  ): Extract<WorkflowRevisionDetail["graph"], { workflow_format_version: 3 }>["orders"] | null {
    return graph?.workflow_format_version === 3 ? graph.orders : null;
  }

  function yamlOfPublishedDocument(detail: WorkflowRevisionDetail): string | null {
    const bytes = decodeCanonicalBase64(detail.document_base64);
    if (bytes === null) return null;
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      return null;
    }
  }

  function publishedRevisionFacts(
    revision: WorkflowRevisionSummary,
    graph: WorkflowRevisionDetail["graph"] | undefined
  ): string {
    const parts = [`format ${revision.workflow_format_version}`];
    const nodeCount = publishedNodeCount(graph);
    const roles = publishedAgentRoles(graph);
    if (nodeCount !== null) parts.push(`${nodeCount} nodes`);
    if (roles !== null) {
      parts.push(`roles: ${roles.length === 0 ? "none" : roles.join(", ")}`);
    }
    if (revision.executable) parts.push("executable");
    else parts.push(cannotBeStarted(revision.not_executable_reason));
    return parts.join(" · ");
  }

  function applyWorkflowDetailIntent(
    detail: WorkflowRevisionDetail,
    intent: WorkflowDetailIntent
  ): void {
    if (intent.kind === "details") return;
    if (intent.kind === "edit") {
      editingHash = intent.revisionHash;
      editYaml = yamlOfPublishedDocument(detail);
      if (editYaml === null) {
        showFailure(
          new Error("The published document is not UTF-8."),
          "The published document could not be read."
        );
      }
      return;
    }
    selectedHashByKey = { ...selectedHashByKey, [intent.rowKey]: intent.revisionHash };
    editingHash = null;
    editYaml = null;
    if (intent.chooseRow) {
      chosenRowKey = intent.rowKey;
      prepareDraft(detail, intent.lineageId);
    }
  }

  async function requestWorkflowDetail(
    intent: WorkflowDetailIntent,
    retry = false
  ): Promise<void> {
    const current = workflowDetails[intent.revisionHash] ?? {
      read: retainedRead<WorkflowRevisionDetail, ReadFailure>(),
      intent
    };
    activeWorkflowDetailHash = intent.revisionHash;
    workflowDetails = { ...workflowDetails, [intent.revisionHash]: { ...current, intent } };
    if (current.read.confirmed !== null) {
      applyWorkflowDetailIntent(current.read.confirmed, intent);
      return;
    }
    if (current.read.request.state === "loading" ||
        (current.read.request.state === "failed" && !retry)) return;
    const begun = beginRead(current.read);
    workflowDetails = {
      ...workflowDetails,
      [intent.revisionHash]: { read: begun.read, intent }
    };
    try {
      const detail = await cockpitApi.getWorkflowRevision(intent.revisionHash);
      const owned = workflowDetails[intent.revisionHash];
      if (owned === undefined) return;
      if (detail.workflow_revision_hash !== intent.revisionHash) {
        workflowDetails = {
          ...workflowDetails,
          [intent.revisionHash]: {
            ...owned,
            read: failRead(owned.read, begun.generation, {
              kind: "unavailable",
              title: "Workflow detail unavailable"
            })
          }
        };
        return;
      }
      const read = confirmRead(owned.read, begun.generation, detail);
      workflowDetails = { ...workflowDetails, [intent.revisionHash]: { ...owned, read } };
      if (activeWorkflowDetailHash === intent.revisionHash && read.confirmed !== null) {
        applyWorkflowDetailIntent(read.confirmed, owned.intent);
      }
    } catch {
      const owned = workflowDetails[intent.revisionHash];
      if (owned === undefined) return;
      workflowDetails = {
        ...workflowDetails,
        [intent.revisionHash]: {
          ...owned,
          read: failRead(owned.read, begun.generation, {
            kind: "unavailable",
            title: "Workflow detail unavailable"
          })
        }
      };
    }
  }

  function retryWorkflowDetail(revisionHash: string): void {
    const resource = workflowDetails[revisionHash];
    if (resource !== undefined) void requestWorkflowDetail(resource.intent, true);
  }

  function declaredOrdersOf(graph: WorkflowRevisionDetail["graph"]): OrderDraft[] {
    if (graph.workflow_format_version !== 3) return [];
    return graph.orders.map((order) => ({
      name: order.name,
      schema: order.schema,
      value: "",
      error: null,
      fieldErrors: [],
      schemaRead: retainedRead<OrderSchemaResource, OrderSchemaReadFailure>()
    }));
  }

  function missingOrderRefusal(name: string): string {
    return `input '${name}' was refused: missing`;
  }

  /**
   * Whether a typed order is fit to send, judged as far as the browser can
   * judge it without a round trip.
   *
   * A schema whose required set is exactly one string field asks for plain
   * text (`OrderEditor`'s human editor), and any non-empty text satisfies it
   * once wrapped -- there is nothing else in the schema to fail. Every other
   * shape asks for JSON, pre-checked against the published schema document
   * (`preValidateOrderValue`) so an obviously wrong shape is named before the
   * server is asked, while a shape that check cannot see still reaches it.
   */
  function validateOrders(orders: OrderDraft[]): boolean {
    let valid = true;
    for (const order of orders) {
      order.fieldErrors = [];
      const typed = order.value.trim();
      if (typed.length === 0) {
        order.error = missingOrderRefusal(order.name);
        valid = false;
        continue;
      }
      const resource = order.schemaRead.confirmed;
      if (resource === null || resource.summary.singleRequiredStringField !== null) {
        order.error = null;
        continue;
      }
      const verdict = preValidateOrderValue(order.value, resource.document);
      if (verdict.kind === "invalid") {
        order.error = verdict.message;
        order.fieldErrors = verdict.fields;
        valid = false;
      } else {
        order.error = null;
      }
    }
    draft = draft === null ? null : { ...draft, orders: [...orders] };
    return valid;
  }

  /** The exact bytes an order sends: the human editor's text, wrapped into the one field its schema names, or the typed JSON verbatim. */
  function wireOrderValue(order: OrderDraft): string {
    const field = order.schemaRead.confirmed?.summary.singleRequiredStringField;
    return field === null || field === undefined ? order.value : encodeSingleFieldOrder(field, order.value);
  }

  async function loadOrderSchema(order: OrderDraft): Promise<void> {
    const begun = beginRead(order.schemaRead);
    updateOrderSchemaRead(order.name, begun.read);
    try {
      const document = await cockpitApi.getSchemaRevision(order.schema.revision);
      const read = confirmRead(begun.read, begun.generation, {
        summary: summarizeOrderSchema(document),
        document
      });
      updateOrderSchemaRead(order.name, read);
    } catch {
      updateOrderSchemaRead(
        order.name,
        failRead(begun.read, begun.generation, {
          kind: "unavailable",
          title: "Order schema unavailable"
        })
      );
    }
  }

  function updateOrderSchemaRead(
    name: string,
    read: RetainedRead<OrderSchemaResource, OrderSchemaReadFailure>
  ): void {
    if (draft === null) return;
    draft = {
      ...draft,
      orders: draft.orders.map((order) => (order.name === name ? { ...order, schemaRead: read } : order))
    };
  }

  function retryOrderSchema(name: string): void {
    const order = draft?.orders.find((candidate) => candidate.name === name);
    if (order !== undefined) void loadOrderSchema(order);
  }

  /**
   * Which declared order a `run-input-refused` names, so its field pointers
   * (`invalid_fields`) land beside the order that carried them rather than
   * only in the top banner's prose. The wire names one order per refusal in
   * `detail` (`input '<name>' was refused: …`), never structurally -- with one
   * order declared this is unambiguous without reading it; with more than
   * one, the sentence is matched against every declared name so a refusal for
   * an order nobody typed is never guessed onto the wrong field.
   */
  function refusedOrderName(problem: Problem, orderNames: readonly string[]): string | null {
    const [only] = orderNames;
    if (orderNames.length === 1) return only ?? null;
    return orderNames.find((name) => problem.detail.startsWith(`input '${name}' was refused`)) ?? null;
  }

  function applyOrderRefusal(error: unknown): void {
    if (draft === null) return;
    if (!(error instanceof CockpitRequestError) || error.problem === null) return;
    const problem = error.problem;
    // `Problem` is a fifty-variant discriminated union; only two of its
    // members declare `invalid_fields`, and every one that does is proven by
    // `problemSchema` (client.ts) to carry exactly `InvalidField[] | undefined`
    // there, so reading it this way is a narrowing workaround, not a new claim.
    const fields = (problem as { invalid_fields?: readonly InvalidField[] }).invalid_fields;
    if (fields === undefined || fields.length === 0) return;
    const name = refusedOrderName(problem, draft.orders.map((order) => order.name));
    if (name === null) return;
    draft = {
      ...draft,
      orders: draft.orders.map((order) => (order.name === name ? { ...order, fieldErrors: fields } : order))
    };
  }

  async function startDraft(): Promise<void> {
    if (draft === null) return;
    const selected = draft;
    let mutation: StartMutation | null = null;
    if (selected.orders.length > 0 && !validateOrders(selected.orders)) return;
    if (bindsAgentRoles(selected.revision.graph)) {
      if (!validateExpertOverrides(selected.bindings)) return;
      operation = "start";
      failureMessage = null;
      const bindings = selected.bindings.map((binding) => ({ ...binding }));
      const overrides = await publishManualOverrides(bindings);
      if (overrides === null) {
        operation = null;
        return;
      }
      let bound: ModelOverride[] | null = overrides;
      let resolved: ModelOverride[] | null = null;
      if (selected.revision.graph.workflow_format_version === 3) {
        modelDraftGeneration += 1;
        const resolution = await loadProjectModels(
          selected.revision.workflow_revision_hash,
          overrides,
          true,
          modelDraftGeneration
        );
        resolved = resolution === null ? null : resolvedBindingsOf(resolution, selected.bindings);
      }
      if (
        bound === null ||
        (selected.revision.graph.workflow_format_version === 3
          ? resolved === null
          : bound.length !== selected.bindings.length)
      ) {
        operation = null;
        return;
      }
      const startabilityBindings = resolved ?? bound;
      if (startabilityBindings.some((binding) => {
        const configuration = publishedConfigurations.find(
          (item) => item.agent_configuration_revision_hash === binding.agent_configuration_revision_hash
        );
        return configuration?.startable === false;
      })) {
        operation = null;
        return;
      }
      mutation =
        selected.orders.length > 0
          ? startMutationV3(
              selected.runId,
              selected.revision.workflow_revision_hash,
              bound,
              selected.orders.map((order) => ({ name: order.name, value: wireOrderValue(order) }))
            )
          : startMutationV2(selected.runId, selected.revision.workflow_revision_hash, bound);
    } else {
      mutation = startMutation(selected.runId, selected.revision.workflow_revision_hash);
    }
    operation = "start";
    failureMessage = null;
    let prepared = false;
    try {
      await mutationJournal.prepare(mutation);
      prepared = true;
      await deliverStart(mutation);
    } catch (error) {
      if (prepared) await recordDeliveryFailure(mutation.mutation_id, error);
      applyOrderRefusal(error);
      showFailure(error, "The run start could not be confirmed.");
    } finally {
      operation = null;
      await loadPending();
    }
  }

  async function retry(entry: JournalEntry): Promise<void> {
    operation = "retry";
    failureMessage = null;
    try {
      if (entry.kind === "publish") await deliverPublication(entry);
      if (entry.kind === "start") await deliverStart(entry);
    } catch (error) {
      await recordDeliveryFailure(entry.mutation_id, error);
      showFailure(error, "The exact retry could not be confirmed.");
    } finally {
      operation = null;
      await loadPending();
    }
  }

  async function discard(mutationId: string): Promise<void> {
    await mutationJournal.discard(mutationId);
    await loadPending();
  }

  async function deliverPublication(mutation: PublishMutation): Promise<void> {
    const result = await cockpitApi.publish(mutation);
    await admitPublishedRevision(
      cockpitApi,
      result.value,
      COCKPIT_CATALOG_ACTOR,
      catalogActivatedAt()
    );
    const resolved = await mutationJournal.resolve(mutation.mutation_id, {
      type: "publication_response",
      status: result.status,
      target: mutation.target,
      request_body_base64: mutation.body_base64,
      revision_hash: result.value.workflow_revision_hash,
      document_base64: result.value.document_base64
    });
    if (!resolved) throw new Error("The publication response did not prove the exact request.");
    activeWorkflowDetailHash = null;
    editingHash = null;
    editYaml = null;
    await loadRevisions();
    const name = result.value.graph.workflow_format_version === 3 ? result.value.graph.name : null;
    const state = name === null ? undefined : revisions.confirmed?.catalogByName[name];
    prepareDraft(result.value, state?.kind === "admitted" ? state.lineageId : null);
    if (name !== null) {
      const key = `named:${name}`;
      selectedHashByKey = { ...selectedHashByKey, [key]: result.value.workflow_revision_hash };
      chosenRowKey = key;
    }
  }

  function prepareDraft(revision: WorkflowRevisionDetail, lineageId: string | null): void {
    const roles = agentRolesOf(revision.graph);
    const resolvesProjectModels = revision.graph.workflow_format_version === 3;
    draft = {
      revision,
      lineageId,
      runId: createRunId(),
      bindings: roles.map((role) => ({
        role,
        selectedHash: "",
        source: resolvesProjectModels ? "looking" : "choose",
        manualOverrideHash: null,
        expertOverrideDrafted: !resolvesProjectModels,
        profileId: "",
        revisionNumber: "",
        providerId: "",
        authMode: "",
        model: "",
        executorRevision: "",
        resolutionHint: null,
        error: null
      })),
      orders: declaredOrdersOf(revision.graph)
    };
    for (const order of draft.orders) void loadOrderSchema(order);
    if (roles.length === 0) {
      clearProjectModels();
      applyBindingRecommendations();
      return;
    }
    if (!resolvesProjectModels) {
      clearProjectModels();
      return;
    }
    modelDraftGeneration += 1;
    void loadProjectModels(
      revision.workflow_revision_hash,
      manualOverridesOf(draft.bindings),
      false,
      modelDraftGeneration
    );
  }

  function clearProjectModels(): void {
    modelDraftGeneration += 1;
    activeModelWorkflowRevisionHash = null;
    projectModels = {
      ...projectModels,
      generation: projectModels.generation + 1,
      request: { state: "idle" }
    };
  }

  function modelSnapshotOf(
    workflowRevisionHash: string,
    overrides: readonly ModelOverride[],
    resolution: ProjectModelResolution
  ): ProjectModelSnapshot {
    return {
      workflowRevisionHash,
      overrideKey: overrideKeyOf(overrides),
      resolution,
      bindings: new Map(resolution.resolutions.map((item) => [item.role, item]))
    };
  }

  function resolvedBindingsOf(
    snapshot: ProjectModelSnapshot,
    bindings: readonly BindingDraft[]
  ): ModelOverride[] | null {
    if (snapshot.resolution.resolutions.length !== bindings.length) return null;
    const expectedRoles = new Set(bindings.map((binding) => binding.role));
    if (snapshot.resolution.resolutions.some((item) => !expectedRoles.has(item.role))) return null;
    const resolved = snapshot.resolution.resolutions.flatMap((item) =>
      item.agent_configuration_revision_hash === null
        ? []
        : [{ role: item.role, agent_configuration_revision_hash: item.agent_configuration_revision_hash }]
    );
    return resolved.length === bindings.length ? resolved : null;
  }

  async function loadProjectModels(
    workflowRevisionHash: string,
    overrides: readonly ModelOverride[],
    retry = false,
    draftGeneration = modelDraftGeneration
  ): Promise<ProjectModelSnapshot | null> {
    if (
      draft === null ||
      draft.revision.workflow_revision_hash !== workflowRevisionHash ||
      draft.bindings.length === 0
    ) return null;
    if (activeModelWorkflowRevisionHash !== workflowRevisionHash) {
      activeModelWorkflowRevisionHash = workflowRevisionHash;
      projectModels = projectModels.confirmed?.workflowRevisionHash === workflowRevisionHash
        ? { ...projectModels, request: { state: "idle" } }
        : retainedRead<ProjectModelSnapshot, ReadFailure>();
    } else if (projectModels.request.state === "failed" && !retry) {
      applyBindingRecommendations();
      return null;
    }
    const begun = beginRead(projectModels);
    projectModels = begun.read;
    applyBindingRecommendations();
    try {
      const projects = await cockpitApi.listProjects();
      const project = projects.items[0];
      if (project === undefined) throw new Error("project missing");
      const resolution = await cockpitApi.resolveProjectModels(
        project.public_project_reference,
        workflowRevisionHash,
        [...overrides]
      );
      const snapshot = modelSnapshotOf(workflowRevisionHash, overrides, resolution);
      if (
        activeModelWorkflowRevisionHash !== workflowRevisionHash ||
        modelDraftGeneration !== draftGeneration
      ) return null;
      projectModels = confirmRead(projectModels, begun.generation, snapshot);
      if (draft?.revision.workflow_revision_hash === workflowRevisionHash) {
        applyBindingRecommendations();
      }
      return snapshot;
    } catch {
      if (
        activeModelWorkflowRevisionHash !== workflowRevisionHash ||
        modelDraftGeneration !== draftGeneration
      ) return null;
      projectModels = failRead(projectModels, begun.generation, {
        kind: "unavailable",
        title: "Model setup unavailable"
      });
      if (draft?.revision.workflow_revision_hash === workflowRevisionHash) {
        applyBindingRecommendations();
      }
      return null;
    }
  }

  function retryProjectModels(): void {
    if (draft !== null) {
      void loadProjectModels(
        draft.revision.workflow_revision_hash,
        manualOverridesOf(draft.bindings),
        true
      );
    }
  }

  function bindingSourceLabel(source: BindingSource): string {
    if (source === "looking") return "Looking…";
    if (source === "project") return "From project";
    if (source === "next-higher") return "Next higher";
    if (source === "workflow") return "Pinned in workflow";
    if (source === "chosen") return "Chosen now";
    if (source === "unavailable") return "Unavailable";
    return "Choose";
  }

  function bindingSourceShape(source: BindingSource): string {
    if (source === "project") return "◆";
    if (source === "next-higher") return "↑";
    if (source === "workflow") return "■";
    if (source === "chosen") return "●";
    if (source === "looking") return "↻";
    if (source === "unavailable") return "◇";
    return "○";
  }

  function resolutionHint(
    resolution: ProjectModelResolution["resolutions"][number]
  ): string | null {
    if (resolution.uncast_reason === "override-not-registered") return "Override not registered.";
    if (resolution.uncast_reason === "workflow-model-not-registered") return "Workflow model not registered.";
    if (resolution.uncast_reason === "workflow-model-ambiguous") return "Workflow model ambiguous.";
    if (resolution.uncast_reason === "no-project-default") return null;
    if (resolution.uncast_reason === "family-difference-unavailable") {
      return resolution.family_differs_from === null
        ? "Family difference unavailable."
        : `Family difference from ${resolution.family_differs_from} unavailable.`;
    }
    return null;
  }

  function selectedConfiguration(
    binding: BindingDraft
  ): AgentConfigurationRevisionListItem | undefined {
    return publishedConfigurations.find(
      (item) => item.agent_configuration_revision_hash === binding.selectedHash
    );
  }

  function registeredConfiguration(
    configurationHash: string
  ): RegisteredConfiguration | undefined {
    return registeredConfigurations.find(
      ({ configuration }) => configuration.agent_configuration_revision_hash === configurationHash
    );
  }

  function registeredConfigurationLabel(registered: RegisteredConfiguration): string {
    const { configuration, modelId, accountId } = registered;
    return `${configuration.provider_id} · ${modelId} · ${accountId}`;
  }

  function bindingHasUnavailableExecutor(binding: BindingDraft): boolean {
    return selectedConfiguration(binding)?.startable === false;
  }

  function setOrderValue(name: string, value: string): void {
    if (draft === null) return;
    draft = {
      ...draft,
      orders: draft.orders.map((order) =>
        order.name === name ? { ...order, value, error: null, fieldErrors: [] } : order
      )
    };
  }

  function manualOverridesOf(bindings: readonly BindingDraft[]): ModelOverride[] {
    return bindings.flatMap((binding) =>
      binding.manualOverrideHash === null
        ? []
        : [{
            role: binding.role,
            agent_configuration_revision_hash: binding.manualOverrideHash
          }]
    );
  }

  function overrideKeyOf(overrides: readonly ModelOverride[]): string {
    return JSON.stringify(overrides);
  }

  function applyBindingRecommendations(): void {
    if (draft === null) return;
    const registered = new Set(
      registeredConfigurations.map(({ configuration }) => configuration.agent_configuration_revision_hash)
    );
    const agentListComplete =
      configurations.confirmed !== null && configurations.request.state === "idle";
    const overrides = manualOverridesOf(draft.bindings);
    const modelSnapshot =
      projectModels.confirmed?.workflowRevisionHash === draft.revision.workflow_revision_hash &&
      projectModels.confirmed.overrideKey === overrideKeyOf(overrides)
        ? projectModels.confirmed
        : null;
    draft = {
      ...draft,
      bindings: draft.bindings.map((binding) => {
        if (modelSnapshot === null) {
          return {
            ...binding,
            selectedHash: binding.manualOverrideHash ?? "",
            source: "looking",
            resolutionHint: null,
            error: null
          };
        }
        const projectChoice = modelSnapshot?.bindings.get(binding.role);
        if (projectChoice !== undefined && projectChoice.agent_configuration_revision_hash !== null) {
          if (registered.has(projectChoice.agent_configuration_revision_hash)) {
            return {
              ...binding,
              selectedHash: projectChoice.agent_configuration_revision_hash,
              source:
                projectChoice.source === "chosen-now"
                  ? "chosen"
                  : projectChoice.source === "pinned-in-workflow"
                    ? "workflow"
                    : projectChoice.default_difficulty !== projectChoice.declared_difficulty
                      ? "next-higher"
                      : "project",
              resolutionHint: null,
              error: null
            };
          }
          return {
            ...binding,
            selectedHash: projectChoice.agent_configuration_revision_hash,
            source: agentListComplete ? "unavailable" : "looking",
            resolutionHint: null,
            error: null
          };
        }
        return {
          ...binding,
          selectedHash: "",
          source: "choose",
          resolutionHint: projectChoice === undefined
            ? "Model resolution did not name this role."
            : resolutionHint(projectChoice),
          error: null
        };
      })
    };
  }

  function chooseNamedAgent(role: string, hash: string): void {
    if (draft === null) return;
    const resolvesProjectModels = draft.revision.graph.workflow_format_version === 3;
    draft = {
      ...draft,
      bindings: draft.bindings.map((binding) =>
        binding.role === role
          ? {
              ...binding,
              selectedHash: hash,
              source:
                hash.length === 0
                  ? resolvesProjectModels
                    ? "looking"
                    : "choose"
                  : "chosen",
              manualOverrideHash: hash.length === 0 ? null : hash,
              expertOverrideDrafted: false,
              resolutionHint: null,
              error: null
            }
          : binding
      )
    };
    if (!resolvesProjectModels) return;
    modelDraftGeneration += 1;
    void loadProjectModels(
      draft.revision.workflow_revision_hash,
      manualOverridesOf(draft.bindings),
      false,
      modelDraftGeneration
    );
  }

  async function publishManualOverrides(
    bindings: BindingDraft[]
  ): Promise<ModelOverride[] | null> {
    const published: Array<{ role: string; agent_configuration_revision_hash: string }> = [];
    for (const binding of bindings) {
      if (binding.manualOverrideHash !== null) {
        published.push({
          role: binding.role,
          agent_configuration_revision_hash: binding.manualOverrideHash
        });
        continue;
      }
      if (!binding.expertOverrideDrafted) continue;
      try {
        const authInput = {
          profile_id: binding.profileId,
          revision_number: Number(binding.revisionNumber),
          provider_id: binding.providerId,
          auth_mode: requireAuthMode(binding.authMode)
        };
        const auth = await cockpitApi.publishAuthProfile(authInput);
        if (!sameFields(auth.value, authInput)) throw new Error("The auth response changed these fields.");
        const configurationInput = {
          model: binding.model,
          auth_profile_revision_hash: auth.value.auth_profile_revision_hash,
          executor_revision: binding.executorRevision
        };
        const configuration = await cockpitApi.publishAgentConfiguration(configurationInput);
        if (!sameFields(configuration.value, {
          ...configurationInput,
          provider_id: binding.providerId,
          auth_mode: binding.authMode
        })) throw new Error("The configuration response changed these fields.");
        published.push({
          role: binding.role,
          agent_configuration_revision_hash: configuration.value.agent_configuration_revision_hash
        });
        if (draft !== null) {
          draft = {
            ...draft,
            bindings: draft.bindings.map((candidate) =>
              candidate.role === binding.role
                ? {
                    ...candidate,
                    selectedHash: configuration.value.agent_configuration_revision_hash,
                    source: "chosen",
                    manualOverrideHash: configuration.value.agent_configuration_revision_hash,
                    expertOverrideDrafted: false,
                    resolutionHint: null,
                    error: null
                  }
                : candidate
            )
          };
        }
      } catch (error) {
        setBindingError(binding.role, error instanceof Error ? error.message : "Binding failed.");
        return null;
      }
    }
    return published;
  }

  function validateExpertOverrides(bindings: BindingDraft[]): boolean {
    let valid = true;
    for (const binding of bindings) {
      if (!binding.expertOverrideDrafted || binding.manualOverrideHash !== null) {
        binding.error = null;
        continue;
      }
      const revisionNumber = Number(binding.revisionNumber);
      const expert =
        binding.profileId.length > 0 &&
        /^(?:[1-9][0-9]*)$/.test(binding.revisionNumber) &&
        Number.isSafeInteger(revisionNumber) &&
        binding.providerId.length > 0 &&
        binding.authMode !== "" &&
        binding.model.length > 0 &&
        binding.executorRevision.length > 0;
      binding.error = expert ? null : "Complete every field.";
      binding.resolutionHint = null;
      valid &&= expert;
    }
    draft = draft === null ? null : { ...draft, bindings: [...bindings] };
    return valid;
  }

  function requireAuthMode(value: BindingDraft["authMode"]): AuthProfileInput["auth_mode"] {
    if (value === "") throw new Error("Auth mode is required.");
    return value;
  }

  function sameFields(actual: object, expected: object): boolean {
    return Object.entries(expected).every(([key, value]) => actual[key as keyof typeof actual] === value);
  }

  function setBindingError(role: string, error: string | null): void {
    if (draft === null) return;
    draft = {
      ...draft,
      bindings: draft.bindings.map((binding) =>
        binding.role === role
          ? error === null
            ? binding.manualOverrideHash === null
              ? {
                  ...binding,
                  selectedHash: "",
                  source: "choose",
                  expertOverrideDrafted: true,
                  resolutionHint: null,
                  error: null
                }
              : { ...binding, expertOverrideDrafted: true, error: null }
            : { ...binding, error }
          : binding
      )
    };
  }

  async function deliverStart(mutation: StartMutation): Promise<void> {
    const result = await cockpitApi.start(mutation);
    const expectedBindings = requestedStartAgentBindings(mutation);
    const returnedBindings = "workflow_format_version" in result.value
      ? result.value.agent_bindings
      : null;
    const resolvesBindings =
      "workflow_format_version" in result.value && result.value.workflow_format_version === 3;
    if (
      expectedBindings !== null &&
      (returnedBindings === null ||
        (!resolvesBindings && returnedBindings.length !== expectedBindings.length) ||
        expectedBindings.some((binding) => {
          const returnedBinding = returnedBindings.find((candidate) => candidate.role === binding.role);
          return returnedBinding?.agent_configuration_revision_hash !==
            binding.agent_configuration_revision_hash;
        }))
    ) throw new Error("The start response changed the exact role bindings.");
    const resolved = await mutationJournal.resolve(mutation.mutation_id, {
      type: "start_response",
      status: result.status,
      target: mutation.target,
      request_body_base64: mutation.body_base64,
      run_id: result.value.run_id,
      public_run_reference: result.value.public_run_reference,
      workflow_revision_hash: result.value.workflow_revision_hash
    });
    if (!resolved) throw new Error("The start response did not prove the exact request.");
    navigate(`/atelier/runs/${result.value.public_run_reference}`);
  }

  async function recordDeliveryFailure(mutationId: string, error: unknown): Promise<void> {
    if (error instanceof CockpitRequestError && error.definitive_failure) {
      await mutationJournal.discard(mutationId);
      return;
    }
    if (await mutationJournal.get(mutationId)) await mutationJournal.markUncertain(mutationId);
  }

  function showFailure(error: unknown, fallback: string): void {
    failureMessage = humanErrorMessage(error, fallback);
  }

  function handleEscape(event: KeyboardEvent): void {
    if (publicationOpen && event.key === "Escape") {
      event.preventDefault();
      void closePublication();
    }
  }
</script>

<svelte:window onkeydown={handleEscape} />

<section aria-labelledby="new-title">
  <BackLink label={catalogRoom.label} path={catalogRoom.path} {navigate} />
  <h1 id="new-title">Choose a workflow</h1>

  {#if failureMessage !== null}<ProblemNotice message={failureMessage} />{/if}

  {#if pending.length > 0}
    <section class="pending" aria-labelledby="pending-title">
      <h2 id="pending-title">Exact requests awaiting confirmation</h2>
      {#each pending as entry (entry.mutation_id)}
        <div class="pending-row">
          <span><strong>{entry.kind === "publish" ? "Publication" : "Run start"}</strong><small>{entry.mutation_id}</small></span>
          <span class="actions"><button type="button" disabled={busy} onclick={() => retry(entry)}>Retry</button><button class="quiet" type="button" disabled={busy} onclick={() => discard(entry.mutation_id)}>Discard</button></span>
        </div>
      {/each}
    </section>
  {/if}

  <fieldset class="mode-picker">
    <legend>Workflow source</legend>
    <label><input type="radio" name="source" value="saved" bind:group={mode} disabled={busy} onchange={changeWorkflowSource} /> Saved workflow</label>
    <label><input type="radio" name="source" value="publish" bind:group={mode} disabled={busy} onchange={changeWorkflowSource} /> Publish YAML</label>
  </fieldset>

  {#if mode === "saved"}
    <fieldset class="revision-picker">
      <legend>Saved workflow</legend>
      <ReadState
        read={revisions}
        label="saved workflows"
        onRetry={() => { void loadRevisions(); }}
      />
      {#each visibleRows as row (row.key)}
        {@const revision = selectedRevisionOf(row, selectedHashByKey[row.key])}
        {@const catalogForm = catalogFormOf(
          revision,
          revision.name === null ? undefined : catalogByName[revision.name]
        )}
        {@const published = workflowDetails[revision.workflow_revision_hash]?.read.confirmed?.graph}
        {@const activeDetail = activeWorkflowDetailHash === null
          ? undefined
          : workflowDetails[activeWorkflowDetailHash]}
        {@const rowDetail = activeDetail?.intent.rowKey === row.key ? activeDetail : undefined}
        <article
          class="saved-workflow form-{catalogForm}"
          data-catalog-form={catalogForm}
          aria-label={row.name ?? revision.workflow_revision_hash}
        >
          <span class="form-mark" aria-hidden="true"></span>
          <div class="saved-workflow-body">
            <div class="saved-workflow-choice">
              <label class="revision-option" class:unstartable={!cockpitCanShow(revision)}>
                <input
                  type="radio"
                  name="saved-revision"
                  value={row.key}
                  checked={chosenRowKey === row.key}
                  disabled={busy || !cockpitCanShow(revision)}
                  onchange={(event) => {
                    event.currentTarget.checked = chosenRowKey === row.key;
                    void requestWorkflowDetail({
                      kind: "select",
                      rowKey: row.key,
                      revisionHash: revision.workflow_revision_hash,
                      chooseRow: true,
                      lineageId: catalogLineageOf(revision)
                    });
                  }}
                />
                <span class="revision-label">
                  {#if revision.name === null}
                    <strong class="revision-name">Unnamed workflow</strong>
                    <span class="muted">format {revision.workflow_format_version} declares no name · {shortFingerprint(revision.workflow_revision_hash)}</span>
                  {:else}
                    <strong class="revision-name">{revision.name}</strong>
                    {#if revision.description !== null}<span class="revision-description">{revision.description}</span>{/if}
                    {#if catalogStateLabel(catalogByName[revision.name]) !== null}
                      <span class="revision-catalog">
                        {catalogStateLabel(catalogByName[revision.name])}
                        <InfoHint
                          label={`Why ${catalogStateLabel(catalogByName[revision.name])?.toLowerCase()}`}
                          exact={catalogStateHint(catalogByName[revision.name]) ?? ""}
                        />
                      </span>
                    {/if}
                  {/if}
                  {#if !revision.executable}
                    <span class="revision-refusal">{cannotBeStarted(revision.not_executable_reason)}</span>
                  {/if}
                </span>
              </label>
              {#if chosenRowKey === row.key}
                <button type="button" class="quiet" disabled={busy} onclick={changeChosenWorkflow}>
                  Change
                </button>
              {/if}
            </div>
            {#if rowDetail !== undefined && rowDetail.read.request.state !== "idle"}
              <ReadState
                read={rowDetail.read}
                label="workflow detail"
                onRetry={() => retryWorkflowDetail(rowDetail.intent.revisionHash)}
              />
            {/if}
            <details
              class="revision-details"
              ontoggle={(event) => {
                if (event.currentTarget.open) {
                  void requestWorkflowDetail({
                    kind: "details",
                    rowKey: row.key,
                    revisionHash: revision.workflow_revision_hash
                  });
                }
              }}
            >
              <summary
                aria-label={revision.name === null
                  ? "Details for this unnamed workflow"
                  : `Details for ${revision.name}`}
              >Details</summary>
              <p class="revision-facts">
                {publishedRevisionFacts(revision, published)}
              </p>
              {#if publishedNodePreviews(published) !== null}
                <WorkflowGraphDrawing
                  previews={publishedNodePreviews(published) ?? []}
                  loops={publishedLoops(published)}
                />
              {/if}
              {#if publishedOrders(published) !== null}
                {#if publishedOrders(published)?.length}
                  <section class="revision-orders" aria-label="Orders">
                    <h3>Orders</h3>
                    <ul>
                      {#each publishedOrders(published) ?? [] as order (order.name)}
                        <li>
                          <strong>{order.name}</strong>
                          <span class="muted">{order.schema.ref}</span>
                          <ProofAnchor
                            label={`Schema of ${order.name}`}
                            seals="the published schema this order pinned"
                            value={order.schema.revision}
                          />
                        </li>
                      {/each}
                    </ul>
                  </section>
                {:else}
                  <p class="muted">No orders.</p>
                {/if}
              {/if}
              {#if row.name !== null}
                <section class="revision-history" aria-label="Revisions">
                  <h3>Revisions</h3>
                  {#if row.revisions.length === 1}
                    <p class="muted">One revision.</p>
                  {:else}
                    <label class="revision-choice">
                      <select
                        value={revision.workflow_revision_hash}
                        onchange={(event) => {
                          const attemptedHash = event.currentTarget.value;
                          event.currentTarget.value = revision.workflow_revision_hash;
                          setRowRevision(row, attemptedHash);
                        }}
                        disabled={busy}
                        aria-label={`Revision of ${row.name}`}
                      >
                        {#each row.revisions as choice (choice.workflow_revision_hash)}
                          <option value={choice.workflow_revision_hash}>{revisionChoiceLabel(choice, row.revisions[0]?.workflow_revision_hash ?? choice.workflow_revision_hash)}</option>
                        {/each}
                      </select>
                    </label>
                  {/if}
                </section>
              {/if}
              <p class="revision-origin">
                {#if revision.name === null}
                  An unnamed published document.
                {:else}
                  {revision.name} → this revision.
                {/if}
                <ProofAnchor
                  label="Workflow revision"
                  seals="the published document"
                  value={revision.workflow_revision_hash}
                />
              </p>
              <button
                type="button"
                class="quiet"
                disabled={busy || (rowDetail !== undefined && rowDetail.read.request.state !== "idle")}
                onclick={() => {
                  void requestWorkflowDetail({
                    kind: "edit",
                    rowKey: row.key,
                    revisionHash: revision.workflow_revision_hash
                  });
                }}
              >Edit</button>
              {#if editingHash === revision.workflow_revision_hash}
                {#if editYaml === null}
                  <p class="muted">The published document could not be read.</p>
                {:else}
                  <div class="field">
                    <label for="edit-yaml">Exact workflow YAML</label>
                    <textarea
                      id="edit-yaml"
                      rows="12"
                      bind:value={editYaml}
                      spellcheck="false"
                      disabled={busy}
                    ></textarea>
                    <button
                      type="button"
                      disabled={busy}
                      onclick={() => { void reviewPublication(editYaml ?? ""); }}
                    >Review publication</button>
                  </div>
                {/if}
              {/if}
            </details>
          </div>
        </article>
      {/each}
      {#if revisions.confirmed?.items.length === 0}<p class="muted">No saved workflows yet.</p>{/if}
    </fieldset>
  {:else}
    <div class="field">
      <label for="workflow-yaml">Exact workflow YAML</label>
      <textarea id="workflow-yaml" rows="12" bind:value={exactYaml} spellcheck="false" disabled={busy}></textarea>
      <button bind:this={publicationTrigger} type="button" disabled={busy} onclick={() => { void reviewPublication(); }}>Review publication</button>
    </div>
  {/if}

  {#if operation === "publish"}<p class="status" role="status">Publishing workflow…</p>
  {:else if operation === "retry"}<p class="status" role="status">Retrying exact request…</p>{/if}

  {#if draft !== null}
    {#if draft.orders.length > 0}
      <section class="binding-list" aria-labelledby="material-list-title">
        <p class="eyebrow">Material</p>
        <h2 id="material-list-title">Orders</h2>
        {#each draft.orders as order (order.name)}
          <OrderEditor
            {order}
            value={order.value}
            error={order.error}
            fieldErrors={order.fieldErrors}
            schemaRead={order.schemaRead}
            {busy}
            starting={operation === "start"}
            onInput={(value) => setOrderValue(order.name, value)}
            onRetrySchema={() => retryOrderSchema(order.name)}
          />
        {/each}
      </section>
    {/if}
    {#if bindsAgentRoles(draft.revision.graph) && draft.bindings.length > 0}
      <section class="binding-list" aria-labelledby="binding-list-title">
        <p class="eyebrow">Agent setup</p>
        <h2 id="binding-list-title">Bindings</h2>
        {#if !correctingOrder}
          <ReadState
            read={configurations}
            label="published agents"
            onRetry={() => void loadConfigurations()}
          />
        {/if}
        {#if projectModels.request.state !== "idle"}
          <ReadState
            read={projectModels}
            label="model setup"
            onRetry={retryProjectModels}
          />
        {/if}
        {#if configurations.confirmed?.length === 0}
          <p class="muted">No published agents yet.</p>
        {/if}
        {#each draft.bindings as binding (binding.role)}
          <article class="node-card binding-card" class:node-queued={operation !== "start" && binding.error === null} class:node-working={operation === "start" && binding.error === null} class:node-needs_you={binding.error !== null} aria-label={`Binding ${binding.role}`}>
            <header class="node-header">
              <div>
                <span class="node-kind">Agent role</span><h3>{binding.role}</h3>
              </div>
              <div class="binding-source-field">
                <span
                  class="binding-source source-{binding.source}"
                  aria-label={`Binding source: ${bindingSourceLabel(binding.source)}`}
                >
                  <span aria-hidden="true">{bindingSourceShape(binding.source)}</span>
                  {bindingSourceLabel(binding.source)}
                </span>
                {#if binding.resolutionHint !== null}
                  <InfoHint
                    label={`Why ${binding.role} needs a choice`}
                    exact={binding.resolutionHint}
                    text="Why"
                  />
                {/if}
              </div>
            </header>
            {#if bindingHasUnavailableExecutor(binding)}
              <p class="binding-startability" role="status">
                <span aria-hidden="true">◇</span>
                Unavailable
                <InfoHint
                  label={`Why ${binding.role} is unavailable`}
                  exact="This deployment cannot start this executor. Choose another agent or repair its startup check."
                />
              </p>
            {/if}
            {#if registeredConfigurations.length > 0 || binding.selectedHash.length > 0}
              <label class="named-agent">Agent
                <select
                  value={binding.selectedHash}
                  onchange={(event) => chooseNamedAgent(binding.role, event.currentTarget.value)}
                  disabled={busy}
                  aria-invalid={binding.error !== null}
                  aria-label={`Agent for ${binding.role}`}
                >
                  <option value="">Choose</option>
                  {#if binding.selectedHash.length > 0 && registeredConfiguration(binding.selectedHash) === undefined}
                    <option value={binding.selectedHash} disabled>
                      {binding.source === "unavailable" ? "Saved agent" : "Looking…"}
                    </option>
                  {/if}
                  {#each registeredConfigurations as registered (registered.configuration.agent_configuration_revision_hash)}
                    <option
                      value={registered.configuration.agent_configuration_revision_hash}
                      disabled={!registered.configuration.startable}
                    >{registeredConfigurationLabel(registered)}</option>
                  {/each}
                </select>
              </label>
            {/if}
            <details class="revision-details expert-fields">
              <summary>Expert fields</summary>
              <div class="binding-grid">
                <label>Profile ID<input type="text" bind:value={binding.profileId} oninput={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null} /></label>
                <label>Revision<input type="text" inputmode="numeric" bind:value={binding.revisionNumber} oninput={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null} /></label>
                <label>Provider<input type="text" bind:value={binding.providerId} oninput={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null} /></label>
                <label>Auth mode<select bind:value={binding.authMode} onchange={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null}><option value="">Choose</option><option value="subscription">Subscription</option><option value="api_key">API key</option></select></label>
                <label>Model<input type="text" bind:value={binding.model} oninput={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null} /></label>
                <label>Executor<input type="text" bind:value={binding.executorRevision} oninput={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null} /></label>
              </div>
            </details>
            {#if binding.error !== null}<p class="binding-error" role="alert">{binding.error}</p>{/if}
          </article>
        {/each}
      </section>
    {/if}
    <!-- Only a version 3 revision can be unexecutable: the older formats carry no
         such field, because everything they can express this build runs. -->
    {#if draft.revision.graph.workflow_format_version === 3 && !draft.revision.graph.executable}
      <section class="start-card unstartable" aria-labelledby="start-title">
        <div>
          <p class="eyebrow">Published</p>
          <h2 id="start-title">{draft.revision.graph.name}</h2>
          {#if draft.revision.graph.description !== null}<p class="muted">{draft.revision.graph.description}</p>{/if}
          <p class="revision-refusal">{cannotBeStarted(draft.revision.graph.not_executable_reason)}</p>
        </div>
      </section>
    {:else if !draftHasUnavailableBinding}
      <section class="start-card" aria-labelledby="start-title">
        <div><p class="eyebrow">{operation === "start" ? "Starting" : "Ready"}</p><h2 id="start-title">Run ID</h2><code>{draft.runId}</code></div>
        <button class="primary" type="button" disabled={busy} onclick={startDraft}>Start</button>
      </section>
    {/if}
    {#if operation === "start"}<p class="status" role="status">Starting the exact run…</p>{/if}
  {/if}
</section>

{#if publicationOpen}
  <div class="modal-backdrop">
    <div bind:this={publicationDialog} class="dialog" role="dialog" aria-modal="true" aria-labelledby="publish-title" tabindex="-1">
      <p class="eyebrow">Exact bytes</p>
      <h2 id="publish-title">Publish this exact workflow?</h2>
      <p>The YAML will be stored exactly as written. The browser does not reinterpret it.</p>
      <div class="dialog-actions">
        <button class="quiet" type="button" onclick={closePublication}>Cancel</button>
        <button class="primary" type="button" onclick={confirmPublication}>Publish</button>
      </div>
    </div>
  </div>
{/if}
