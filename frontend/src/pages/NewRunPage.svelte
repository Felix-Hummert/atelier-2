<script lang="ts">
  import { onMount, tick } from "svelte";

  import {
    CockpitRequestError,
    decodeCanonicalBase64,
    type AgentConfigurationRevisionListItem,
    type AuthProfileInput,
    type CockpitApi,
    type OccupancyRevision,
    type WorkflowRevisionDetail,
    type WorkflowRevisionSummary
  } from "../api/client";
  import Breadcrumb from "../components/Breadcrumb.svelte";
  import InfoHint from "../components/InfoHint.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import ProofAnchor from "../components/ProofAnchor.svelte";
  import ReadState from "../components/ReadState.svelte";
  import WorkflowGraphDrawing from "../components/WorkflowGraphDrawing.svelte";
  import { THE_ONE_PROJECT } from "../lib/project";
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
    namedAgentLabel,
    readLastNamedAgentChoices,
    rememberNamedAgentChoice
  } from "../lib/namedAgentChoice";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    type RetainedRead
  } from "../lib/readResource";
  import { readEveryAgentConfiguration, readEveryRevision } from "../lib/runPages";
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

  type BindingSource = "looking" | "project" | "remembered" | "choose" | "unavailable";

  interface BindingDraft {
    role: string;
    selectedHash: string;
    source: BindingSource;
    manual: boolean;
    profileId: string;
    revisionNumber: string;
    providerId: string;
    authMode: "" | AuthProfileInput["auth_mode"];
    model: string;
    executorRevision: string;
    error: string | null;
  }

  interface OrderDraft {
    name: string;
    schema: { ref: string; revision: string };
    value: string;
    error: string | null;
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

  interface ProjectOccupancySnapshot {
    lineageId: string;
    bindings: Map<string, string>;
  }

  let revisions: RetainedRead<SavedWorkflowSnapshot, ReadFailure> =
    retainedRead<SavedWorkflowSnapshot, ReadFailure>();
  let configurations: RetainedRead<AgentConfigurationRevisionListItem[], ReadFailure> =
    retainedRead<AgentConfigurationRevisionListItem[], ReadFailure>();
  let projectOccupancy: RetainedRead<ProjectOccupancySnapshot, ReadFailure> =
    retainedRead<ProjectOccupancySnapshot, ReadFailure>();
  let activeOccupancyLineageId: string | null = null;
  let occupancyDraftGeneration = 0;
  let publishedConfigurations: AgentConfigurationRevisionListItem[] = [];
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
    clearProjectOccupancy();
    activeWorkflowDetailHash = null;
    failureMessage = null;
    editingHash = null;
    editYaml = null;
  }

  function changeWorkflowSource(): void {
    activeWorkflowDetailHash = null;
    draft = null;
    clearProjectOccupancy();
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
      const confirmed = confirmRead(configurations, begun.generation, reading.configurations);
      configurations = confirmed;
      if (confirmed.generation === begun.generation) {
        applyBindingRecommendations();
      }
    } catch {
      configurations = failRead(configurations, begun.generation, {
        kind: "unavailable",
        title: "Published agents unavailable"
      });
      applyBindingRecommendations();
    }
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
      error: null
    }));
  }

  function missingOrderRefusal(name: string): string {
    return `input '${name}' was refused: missing`;
  }

  function validateOrders(orders: OrderDraft[]): boolean {
    let valid = true;
    for (const order of orders) {
      const present = order.value.trim().length > 0;
      order.error = present ? null : missingOrderRefusal(order.name);
      valid &&= present;
    }
    draft = draft === null ? null : { ...draft, orders: [...orders] };
    return valid;
  }

  function schemaHint(order: OrderDraft): string {
    return `${order.schema.ref}@${order.schema.revision}`;
  }

  async function startDraft(): Promise<void> {
    if (draft === null) return;
    const selected = draft;
    if (selected.bindings.some((binding) => bindingHasUnavailableExecutor(binding))) return;
    let mutation: StartMutation | null = null;
    if (selected.orders.length > 0 && !validateOrders(selected.orders)) return;
    if (bindsAgentRoles(selected.revision.graph)) {
      if (!validateBindings(selected.bindings)) return;
      operation = "start";
      failureMessage = null;
      const bindings = selected.bindings.map((binding) => ({ ...binding }));
      const publishedBindings = await resolveBindings(bindings);
      if (publishedBindings === null) {
        operation = null;
        return;
      }
      const bound = publishedBindings.map(({ role, agent_configuration_revision_hash }) => ({
        role,
        agent_configuration_revision_hash
      }));
      mutation =
        selected.orders.length > 0
          ? startMutationV3(
              selected.runId,
              selected.revision.workflow_revision_hash,
              bound,
              selected.orders.map((order) => ({ name: order.name, value: order.value }))
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
    draft = {
      revision,
      lineageId,
      runId: createRunId(),
      bindings: roles.map((role) => ({
        role,
        selectedHash: "",
        source: lineageId === null ? "choose" : "looking",
        manual: false,
        profileId: "",
        revisionNumber: "",
        providerId: "",
        authMode: "",
        model: "",
        executorRevision: "",
        error: null
      })),
      orders: declaredOrdersOf(revision.graph)
    };
    if (lineageId === null || roles.length === 0) {
      clearProjectOccupancy();
      applyBindingRecommendations();
      return;
    }
    occupancyDraftGeneration += 1;
    void loadProjectOccupancy(lineageId, false, occupancyDraftGeneration);
  }

  function clearProjectOccupancy(): void {
    occupancyDraftGeneration += 1;
    activeOccupancyLineageId = null;
    projectOccupancy = {
      ...projectOccupancy,
      generation: projectOccupancy.generation + 1,
      request: { state: "idle" }
    };
  }

  function occupancySnapshotOf(
    lineageId: string,
    occupancy: OccupancyRevision | null
  ): ProjectOccupancySnapshot {
    return {
      lineageId,
      bindings: new Map(
        (occupancy?.bindings ?? []).map((binding) => [
          binding.role,
          binding.agent_configuration_revision_hash
        ])
      )
    };
  }

  async function loadProjectOccupancy(
    lineageId: string,
    retry = false,
    draftGeneration = occupancyDraftGeneration
  ): Promise<void> {
    if (draft === null || draft.lineageId !== lineageId || draft.bindings.length === 0) return;
    if (activeOccupancyLineageId !== lineageId) {
      activeOccupancyLineageId = lineageId;
      projectOccupancy = projectOccupancy.confirmed?.lineageId === lineageId
        ? { ...projectOccupancy, request: { state: "idle" } }
        : retainedRead<ProjectOccupancySnapshot, ReadFailure>();
    } else if (projectOccupancy.request.state === "failed" && !retry) {
      applyBindingRecommendations();
      return;
    }
    const begun = beginRead(projectOccupancy);
    projectOccupancy = begun.read;
    applyBindingRecommendations();
    try {
      const projects = await cockpitApi.listProjects();
      let snapshot = occupancySnapshotOf(lineageId, null);
      const project = projects.items[0];
      if (project !== undefined) {
        try {
          const occupancy = await cockpitApi.getProjectOccupancy(
            project.public_project_reference,
            lineageId
          );
          snapshot = occupancySnapshotOf(lineageId, occupancy);
        } catch (error) {
          if (problemCode(error) !== "occupancy-missing") throw error;
        }
      }
      if (
        activeOccupancyLineageId !== lineageId ||
        occupancyDraftGeneration !== draftGeneration
      ) return;
      projectOccupancy = confirmRead(projectOccupancy, begun.generation, snapshot);
      if (draft?.lineageId === lineageId) applyBindingRecommendations();
    } catch {
      if (
        activeOccupancyLineageId !== lineageId ||
        occupancyDraftGeneration !== draftGeneration
      ) return;
      projectOccupancy = failRead(projectOccupancy, begun.generation, {
        kind: "unavailable",
        title: "Project occupancy unavailable"
      });
      if (draft?.lineageId === lineageId) applyBindingRecommendations();
    }
  }

  function retryProjectOccupancy(): void {
    if (draft?.lineageId !== null && draft?.lineageId !== undefined) {
      void loadProjectOccupancy(draft.lineageId, true);
    }
  }

  function bindingSourceLabel(source: BindingSource): string {
    if (source === "looking") return "Looking…";
    if (source === "project") return "Project";
    if (source === "remembered") return "Remembered";
    if (source === "unavailable") return "Unavailable";
    return "Choose";
  }

  function bindingSourceShape(source: BindingSource): string {
    if (source === "project") return "◆";
    if (source === "remembered") return "●";
    if (source === "looking") return "↻";
    if (source === "unavailable") return "◇";
    return "○";
  }

  function selectedConfiguration(
    binding: BindingDraft
  ): AgentConfigurationRevisionListItem | undefined {
    return publishedConfigurations.find(
      (item) => item.agent_configuration_revision_hash === binding.selectedHash
    );
  }

  function bindingHasUnavailableExecutor(binding: BindingDraft): boolean {
    return selectedConfiguration(binding)?.startable === false;
  }

  function setOrderValue(name: string, value: string): void {
    if (draft === null) return;
    draft = {
      ...draft,
      orders: draft.orders.map((order) =>
        order.name === name ? { ...order, value, error: null } : order
      )
    };
  }

  function applyBindingRecommendations(): void {
    if (draft === null) return;
    const remembered = readLastNamedAgentChoices(globalThis.localStorage);
    const known = new Set(
      (configurations.confirmed ?? []).map(
        (item) => item.agent_configuration_revision_hash
      )
    );
    const agentListComplete =
      configurations.confirmed !== null && configurations.request.state === "idle";
    const occupancy =
      draft.lineageId !== null && projectOccupancy.confirmed?.lineageId === draft.lineageId
        ? projectOccupancy.confirmed
        : null;
    draft = {
      ...draft,
      bindings: draft.bindings.map((binding) => {
        if (binding.manual) return binding;
        if (draft?.lineageId !== null && occupancy === null) {
          return { ...binding, selectedHash: "", source: "looking" };
        }
        const projectHash = occupancy?.bindings.get(binding.role);
        if (projectHash !== undefined) {
          if (known.has(projectHash)) {
            return { ...binding, selectedHash: projectHash, source: "project" };
          }
          return {
            ...binding,
            selectedHash: projectHash,
            source: agentListComplete ? "unavailable" : "looking"
          };
        }
        const rememberedHash = remembered.get(binding.role);
        if (rememberedHash !== undefined && known.has(rememberedHash)) {
          return { ...binding, selectedHash: rememberedHash, source: "remembered" };
        }
        if (rememberedHash !== undefined && !agentListComplete) {
          return { ...binding, selectedHash: "", source: "looking" };
        }
        return { ...binding, selectedHash: "", source: "choose" };
      })
    };
  }

  function chooseNamedAgent(role: string, hash: string): void {
    if (draft === null) return;
    draft = {
      ...draft,
      bindings: draft.bindings.map((binding) =>
        binding.role === role
          ? {
              ...binding,
              selectedHash: hash,
              source: hash.length === 0 ? "choose" : "remembered",
              manual: true,
              error: null
            }
          : binding
      )
    };
    if (hash.length > 0) rememberNamedAgentChoice(globalThis.localStorage, role, hash);
  }

  async function resolveBindings(
    bindings: BindingDraft[]
  ): Promise<Array<{ role: string; agent_configuration_revision_hash: string }> | null> {
    const published: Array<{ role: string; agent_configuration_revision_hash: string }> = [];
    for (const binding of bindings) {
      if (binding.selectedHash.length > 0) {
        published.push({
          role: binding.role,
          agent_configuration_revision_hash: binding.selectedHash
        });
        continue;
      }
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
      } catch (error) {
        setBindingError(binding.role, error instanceof Error ? error.message : "Binding failed.");
        return null;
      }
    }
    return published;
  }

  function validateBindings(bindings: BindingDraft[]): boolean {
    const known = new Set(
      publishedConfigurations.map((item) => item.agent_configuration_revision_hash)
    );
    let valid = true;
    for (const binding of bindings) {
      const revisionNumber = Number(binding.revisionNumber);
      const named = binding.selectedHash.length > 0 && known.has(binding.selectedHash);
      const expert =
        binding.profileId.length > 0 &&
        /^(?:[1-9][0-9]*)$/.test(binding.revisionNumber) &&
        Number.isSafeInteger(revisionNumber) &&
        binding.providerId.length > 0 &&
        binding.authMode !== "" &&
        binding.model.length > 0 &&
        binding.executorRevision.length > 0;
      const complete = named || expert;
      binding.error = complete
        ? null
        : publishedConfigurations.length === 0
          ? "Complete every field."
          : "Choose a published agent or complete every field.";
      valid &&= complete;
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
          ? { ...binding, source: binding.selectedHash === "" ? "choose" : binding.source, manual: true, error }
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
    if (
      expectedBindings !== null &&
      (returnedBindings === null ||
        returnedBindings.length !== expectedBindings.length ||
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
  <Breadcrumb
    steps={[{ label: "Board", path: "/atelier" }, { label: THE_ONE_PROJECT, path: "/atelier/project" }]}
    current="New run"
    {navigate}
  />
  <p class="eyebrow">New durable work</p>
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
                    <code class="revision-hash">{revision.workflow_revision_hash}</code>
                    <span class="muted">unnamed — format {revision.workflow_format_version} declares no name</span>
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
                  showExcerpt={true}
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
          <article
            class="node-card binding-card"
            class:node-queued={operation !== "start" && order.error === null}
            class:node-working={operation === "start" && order.error === null}
            class:node-needs_you={order.error !== null}
            aria-label={`Order ${order.name}`}
          >
            <header class="node-header">
              <span class="node-kind">Order</span><h3>{order.name}</h3>
            </header>
            <p class="muted"><code>{schemaHint(order)}</code></p>
            <label class="named-agent">Material
              <textarea
                rows="6"
                value={order.value}
                oninput={(event) => setOrderValue(order.name, event.currentTarget.value)}
                spellcheck="false"
                disabled={busy}
                aria-invalid={order.error !== null}
                aria-label={`Material ${order.name}`}
              ></textarea>
            </label>
            {#if order.error !== null}<p class="binding-error" role="alert">{order.error}</p>{/if}
          </article>
        {/each}
      </section>
    {/if}
    {#if bindsAgentRoles(draft.revision.graph) && draft.bindings.length > 0}
      <section class="binding-list" aria-labelledby="binding-list-title">
        <p class="eyebrow">Agent setup</p>
        <h2 id="binding-list-title">Bindings</h2>
        <ReadState read={configurations} label="published agents" onRetry={() => void loadConfigurations()} />
        {#if draft.lineageId !== null && projectOccupancy.request.state !== "idle"}
          <ReadState
            read={projectOccupancy}
            label="project occupancy"
            onRetry={retryProjectOccupancy}
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
              <span
                class="binding-source source-{binding.source}"
                aria-label={`Binding source: ${bindingSourceLabel(binding.source)}`}
              >
                <span aria-hidden="true">{bindingSourceShape(binding.source)}</span>
                {bindingSourceLabel(binding.source)}
              </span>
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
            {#if publishedConfigurations.length > 0 || binding.selectedHash.length > 0}
              <label class="named-agent">Agent
                <select
                  value={binding.selectedHash}
                  onchange={(event) => chooseNamedAgent(binding.role, event.currentTarget.value)}
                  disabled={busy}
                  aria-invalid={binding.error !== null}
                  aria-label={`Agent for ${binding.role}`}
                >
                  <option value="">Choose</option>
                  {#if binding.selectedHash.length > 0 && !publishedConfigurations.some(
                    (item) => item.agent_configuration_revision_hash === binding.selectedHash
                  )}
                    <option value={binding.selectedHash} disabled>
                      {binding.source === "unavailable" ? "Unavailable" : "Looking…"}
                    </option>
                  {/if}
                  {#each publishedConfigurations as item (item.agent_configuration_revision_hash)}
                    <option
                      value={item.agent_configuration_revision_hash}
                      disabled={!item.startable}
                    >{namedAgentLabel(item)}{item.startable ? "" : " — Unavailable"}</option>
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
    {#if (draft.revision.graph.workflow_format_version === 3 && !draft.revision.graph.executable) || draftHasUnavailableBinding}
      <section class="start-card unstartable" aria-labelledby="start-title">
        <div>
          {#if draftHasUnavailableBinding}
            <p class="eyebrow">Unavailable</p>
            <h2 id="start-title">This run cannot start</h2>
            <p class="revision-refusal">Choose a startable agent before starting this run.</p>
          {:else}
            {#if draft.revision.graph.workflow_format_version === 3}
              <p class="eyebrow">Published</p>
              <h2 id="start-title">{draft.revision.graph.name}</h2>
              {#if draft.revision.graph.description !== null}<p class="muted">{draft.revision.graph.description}</p>{/if}
              <p class="revision-refusal">{cannotBeStarted(draft.revision.graph.not_executable_reason)}</p>
            {/if}
          {/if}
        </div>
      </section>
    {:else}
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
