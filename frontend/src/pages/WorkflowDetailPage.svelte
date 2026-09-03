<script lang="ts">
  import { onMount } from "svelte";

  import type { CockpitApi, WorkflowRevisionDetail } from "../api/client";
  import BackLink from "../components/BackLink.svelte";
  import ProofAnchor from "../components/ProofAnchor.svelte";
  import ReadState from "../components/ReadState.svelte";
  import RetireCatalogLineageSheet from "../components/RetireCatalogLineageSheet.svelte";
  import WorkflowGraphDrawing from "../components/WorkflowGraphDrawing.svelte";
  import WorkflowNodePreviewPanel from "../components/WorkflowNodePreviewPanel.svelte";
  import WorkflowStartSheet from "../components/WorkflowStartSheet.svelte";
  import type { MutationJournal } from "../lib/mutationJournal";
  import { catalogHeadsOf, catalogNameStateOf, type CatalogNameState } from "../lib/catalogName";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { cannotBeStarted, humanErrorMessage } from "../lib/humanRefusal";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    type RetainedRead
  } from "../lib/readResource";
  import { readEveryRevision } from "../lib/runPages";
  import { groupSavedWorkflows } from "../lib/savedWorkflows";
  import { summarizeOrderSchema, typeLabel, type OrderSchemaSummary } from "../lib/orderSchema";
  import { WORKSHOP_DESTINATION } from "../lib/workshop";
  import { catalogPageCopy, catalogStateNote, workflowDetailCopy } from "../lib/catalogPageCopy";
  import { catalogActivatedAt, COCKPIT_CATALOG_ACTOR } from "../lib/catalogAdmission";

  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let navigate: (path: string) => void;
  export let name: string;
  export let createRunId: () => string;

  const catalog = WORKSHOP_DESTINATION.catalog;

  type ReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  /**
   * A completed read answers with one of two outcomes, not a third failure
   * kind: nothing here failed to read when a name simply names no published
   * workflow, the same way `catalogNameStateOf` treats a 404 as an outcome
   * rather than a transport failure.
   */
  type DetailOutcome =
    | {
        kind: "found";
        detail: WorkflowRevisionDetail;
        catalogState: CatalogNameState;
        orders: readonly DeclaredOrderSummary[];
      }
    | { kind: "not-found" };

  type DeclaredOrderSummary = {
    readonly name: string;
    readonly schemaRef: string;
    readonly summary: OrderSchemaSummary | null;
  };

  let detail: RetainedRead<DetailOutcome, ReadFailure> = retainedRead<DetailOutcome, ReadFailure>();
  let failureMessage: string | null = null;
  let selectedNodeId: string | null = null;
  let startSheetOpen = false;
  let retireSheetOpen = false;
  let retiring = false;
  let retireFailure: string | null = null;

  $: found = detail.confirmed?.kind === "found" ? detail.confirmed : null;
  $: graph = found?.detail.graph ?? null;
  $: orders = found?.orders ?? [];
  $: revisionHash = found?.detail.workflow_revision_hash ?? "";
  $: provenance = found?.detail.provenance ?? null;
  /**
   * The Technical fold's two facts beyond the revision anchor (mockup v8
   * :1528): the format the document itself declares, and -- only when a
   * connected source delivered these bytes -- where they came from. A
   * file-imported revision carries `provenance: null`, so this row is
   * absent rather than a placeholder (#1077). `workflowDetailCopy.sourceFact`
   * is the one owner for the shown commit's short prefix and its full-hash
   * hover title, shared with the catalog card's own chip (#1112) -- the
   * fold's one other proof affordance, `ProofAnchor`, already owns
   * copy-to-clipboard for the revision hash, so this reuses a plain hover
   * reveal rather than a second copy control for the same purpose.
   */
  $: technicalFormat =
    graph !== null && graph.workflow_format_version === 3
      ? workflowDetailCopy.formatVersion(graph.workflow_format_version)
      : null;
  $: technicalSource =
    provenance !== null
      ? workflowDetailCopy.sourceFact(provenance.source_commit, provenance.source_path)
      : null;
  $: retired = found?.catalogState.kind === "retired";
  $: admitted = found?.catalogState.kind === "admitted";
  $: catalogNote = catalogStateNote(found?.catalogState);
  /**
   * Only a version 3 document ever declares a `name:`, so a row this page
   * reaches by name is a version 3 revision by construction -- but the fetch
   * that confirms it crosses the network, a real boundary, so this still
   * checks rather than assumes.
   */
  $: previews = graph !== null && graph.workflow_format_version === 3 ? graph.node_previews : null;
  $: loops = graph !== null && graph.workflow_format_version === 3 ? graph.loops : [];
  $: selectedPreview = previews?.find((preview) => preview.id === selectedNodeId) ?? null;

  /**
   * `name` is read once, on mount, the same way `RunCockpitPage` reads
   * `publicReference`: the router only ever puts this page in `App`'s branch
   * by leaving a different route (the catalog list) and coming back, which
   * remounts it, so a prop change on a live instance is not a real case here.
   */
  onMount(() => {
    void load();
  });

  async function load(): Promise<void> {
    failureMessage = null;
    selectedNodeId = null;
    const begun = beginRead(detail);
    detail = begun.read;
    try {
      const reading = await readEveryRevision((after) => cockpitApi.listWorkflowRevisions(after));
      if (!reading.complete) {
        detail = failRead(detail, begun.generation, {
          kind: "incomplete",
          title: workflowDetailCopy.detailUnavailable
        });
        return;
      }
      const catalogState = await catalogNameStateOf(name, (asked) => cockpitApi.getRevisionByName(asked));
      const newestByName = catalogHeadsOf(reading.revisions, { [name]: catalogState });
      if (newestByName === null) {
        detail = failRead(detail, begun.generation, {
          kind: "unavailable",
          title: workflowDetailCopy.detailUnavailable
        });
        return;
      }
      const row = groupSavedWorkflows(reading.revisions, newestByName).find(
        (candidate) => candidate.name === name
      );
      const head = row?.revisions[0];
      if (head === undefined) {
        detail = confirmRead(detail, begun.generation, { kind: "not-found" });
        return;
      }
      const full = await cockpitApi.getWorkflowRevision(head.workflow_revision_hash);
      const orders = await declaredOrderSummaries(full);
      detail = confirmRead(detail, begun.generation, { kind: "found", detail: full, catalogState, orders });
    } catch (error) {
      failureMessage = humanErrorMessage(error, workflowDetailCopy.detailUnavailable);
      detail = failRead(detail, begun.generation, {
        kind: "unavailable",
        title: workflowDetailCopy.detailUnavailable
      });
    }
  }

  function selectNode(nodeId: string): void {
    selectedNodeId = nodeId;
  }

  function closePanel(): void {
    selectedNodeId = null;
  }

  async function declaredOrderSummaries(
    revision: WorkflowRevisionDetail
  ): Promise<readonly DeclaredOrderSummary[]> {
    if (revision.graph.workflow_format_version !== 3) return [];
    return Promise.all(
      revision.graph.orders.map(async (order) => {
        try {
          return {
            name: order.name,
            schemaRef: order.schema.ref,
            summary: summarizeOrderSchema(await cockpitApi.getSchemaRevision(order.schema.revision))
          };
        } catch {
          return { name: order.name, schemaRef: order.schema.ref, summary: null };
        }
      })
    );
  }

  /** Opens the catalog's one start sheet for this exact revision. */
  function goToStart(): void {
    startSheetOpen = true;
  }

  function openRetireSheet(): void {
    retireFailure = null;
    retireSheetOpen = true;
  }

  async function retire(): Promise<void> {
    if (found === null || found.catalogState.kind !== "admitted") return;
    retiring = true;
    retireFailure = null;
    try {
      await cockpitApi.retireCatalogLineage(found.catalogState.lineageId, {
        actor: COCKPIT_CATALOG_ACTOR,
        activated_at: catalogActivatedAt()
      });
      retireSheetOpen = false;
      navigate(catalog.path);
    } catch (error) {
      retireFailure = humanErrorMessage(error, workflowDetailCopy.retireFailed);
    } finally {
      retiring = false;
    }
  }
</script>

<section class="surface" aria-labelledby="workflow-detail-title">
  <BackLink label={catalog.label} path={catalog.path} {navigate} />

  <ReadState read={detail} label={workflowDetailCopy.detailLabel} onRetry={() => { void load(); }} />
  {#if failureMessage !== null}<p class="failure" role="alert">{failureMessage}</p>{/if}

  {#if detail.confirmed?.kind === "not-found"}
    <p class="empty-title">{wrapDisplayCopy(workflowDetailCopy.notFoundTitle)}</p>
    <p class="muted">{wrapDisplayCopy(workflowDetailCopy.notFoundDescription)}</p>
  {:else if graph !== null}
    <header class="surface-head detail-head">
      <div>
        <h1 id="workflow-detail-title">{name}</h1>
        {#if catalogNote !== null}
          <p class="note">{wrapDisplayCopy(catalogNote)}</p>
        {/if}
      </div>
      {#if !retired}
        <button
          type="button"
          class="primary"
          disabled={!admitted || (graph.workflow_format_version === 3 && !graph.executable)}
          onclick={goToStart}
        >{wrapDisplayCopy(catalogPageCopy.start)}</button>
      {/if}
    </header>

    {#if retired}
      <p class="failure" role="alert">{wrapDisplayCopy(workflowDetailCopy.retiredNotice)}</p>
    {:else if graph.workflow_format_version === 3 && !graph.executable}
      <p class="failure" role="alert">{cannotBeStarted(graph.not_executable_reason)}</p>
    {/if}

    {#if previews !== null}
      <WorkflowGraphDrawing {previews} {loops} onSelect={selectNode} {selectedNodeId} />
    {:else}
      <p class="muted">{wrapDisplayCopy(workflowDetailCopy.graphUnavailable)}</p>
    {/if}

    <section class="declared-orders" aria-labelledby="workflow-orders-title">
      <h2 id="workflow-orders-title">{wrapDisplayCopy(workflowDetailCopy.orders)}</h2>
      {#if orders.length === 0}
        <p class="muted">{wrapDisplayCopy(workflowDetailCopy.noOrders)}</p>
      {:else}
        {#each orders as order (order.name)}
          <article class="declared-order">
            <h3>{order.name}</h3>
            <p class="order-schema">{wrapDisplayCopy(workflowDetailCopy.schema)} · {order.schemaRef}</p>
            {#if order.summary === null}
              <p class="muted">{wrapDisplayCopy(workflowDetailCopy.schemaUnavailable)}</p>
            {:else if order.summary.fields.length === 0}
              <p class="muted">{wrapDisplayCopy(workflowDetailCopy.schemaAcceptsAny)}</p>
            {:else}
              <ul>
                {#each order.summary.fields as field (field.name)}
                  <li>{field.name} · {typeLabel(field.types)}{field.required ? ` · ${workflowDetailCopy.required}` : ""}</li>
                {/each}
              </ul>
            {/if}
          </article>
        {/each}
      {/if}
    </section>

    {#if selectedPreview !== null}
      <WorkflowNodePreviewPanel preview={selectedPreview} onClose={closePanel} />
    {/if}

    <details class="detail-technical">
      <summary>{workflowDetailCopy.technical}</summary>
      <div class="detail-provenance">
        <ProofAnchor
          label={workflowDetailCopy.workflowRevision}
          seals={workflowDetailCopy.sealsWorkflowRevision}
          value={revisionHash}
        />
        {#if technicalFormat !== null || technicalSource !== null}
          <dl class="detail-technical-facts">
            {#if technicalSource !== null}
              <div>
                <dt>{workflowDetailCopy.source}</dt>
                <dd title={technicalSource.title}>{technicalSource.label}</dd>
              </div>
            {/if}
            {#if technicalFormat !== null}
              <div>
                <dt>{workflowDetailCopy.format}</dt>
                <dd>{technicalFormat}</dd>
              </div>
            {/if}
          </dl>
        {/if}
      </div>
      {#if admitted && !retired}
        <div class="technical-action">
          <button class="danger" type="button" onclick={openRetireSheet}>{wrapDisplayCopy(workflowDetailCopy.retire)}</button>
        </div>
      {/if}
    </details>

    {#if startSheetOpen && found !== null}
      <WorkflowStartSheet
        {cockpitApi}
        {mutationJournal}
        revision={found.detail}
        workflowName={name}
        {navigate}
        {createRunId}
        onClose={() => { startSheetOpen = false; }}
      />
    {/if}
    {#if retireSheetOpen}
      <RetireCatalogLineageSheet
        {name}
        submitting={retiring}
        failure={retireFailure}
        onConfirm={() => { void retire(); }}
        onDismiss={() => { retireSheetOpen = false; }}
      />
    {/if}
  {/if}
</section>

<style>
  .muted {
    color: var(--ink-dim);
  }

  .note {
    margin: var(--space-1) 0 0;
    font-size: var(--text-2xs);
    text-transform: uppercase;
    letter-spacing: var(--tracking-label);
    color: var(--signal-attention);
  }

  .failure {
    color: var(--signal-failure);
  }

  .empty-title {
    margin: 0 0 var(--space-1);
    font-weight: var(--weight-strong);
  }

  .detail-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-5);
  }

  /* Shape and colour come from the one `button.primary` skin; this button only
     declares how it sits in the head row, and why it cannot be pressed. */
  .primary {
    flex: none;
  }

  .primary:disabled {
    background: transparent;
    color: var(--ink-dim);
    border-color: var(--line);
    cursor: not-allowed;
  }

  .detail-technical {
    margin-top: var(--space-5);
  }

  .detail-technical summary {
    cursor: pointer;
  }

  .detail-provenance {
    margin-top: var(--space-2);
    padding: var(--space-3);
    border-left: var(--edge-mark) solid var(--ink-dim);
    background: color-mix(in srgb, currentColor 4%, transparent);
  }

  .detail-technical-facts {
    display: grid;
    gap: var(--space-2);
    margin: var(--space-3) 0 0;
  }

  .detail-technical-facts dt {
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  .detail-technical-facts dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .technical-action {
    margin-top: var(--space-3);
  }

  .danger {
    border-color: var(--signal-failure);
    color: var(--signal-failure);
    background: transparent;
  }

  .declared-orders {
    display: grid;
    gap: var(--space-3);
    margin-top: var(--space-5);
  }

  .declared-orders h2,
  .declared-order h3,
  .declared-order p,
  .declared-order ul {
    margin: 0;
  }

  .declared-order {
    display: grid;
    gap: var(--space-1);
    padding: var(--space-3);
    border: var(--edge) solid var(--line);
    border-radius: var(--r);
  }

  .order-schema {
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }
</style>
