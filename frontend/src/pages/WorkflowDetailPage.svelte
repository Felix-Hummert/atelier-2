<script lang="ts">
  import { onMount } from "svelte";

  import type { CockpitApi, WorkflowRevisionDetail } from "../api/client";
  import BackLink from "../components/BackLink.svelte";
  import ReadState from "../components/ReadState.svelte";
  import WorkflowGraphDrawing from "../components/WorkflowGraphDrawing.svelte";
  import WorkflowNodePreviewPanel from "../components/WorkflowNodePreviewPanel.svelte";
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
  import { WORKSHOP_DESTINATION } from "../lib/workshop";
  import { catalogPageCopy, catalogStateNote, workflowDetailCopy } from "../lib/catalogPageCopy";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;
  export let name: string;

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
    | { kind: "found"; detail: WorkflowRevisionDetail; catalogState: CatalogNameState }
    | { kind: "not-found" };

  let detail: RetainedRead<DetailOutcome, ReadFailure> = retainedRead<DetailOutcome, ReadFailure>();
  let failureMessage: string | null = null;
  let selectedNodeId: string | null = null;

  $: found = detail.confirmed?.kind === "found" ? detail.confirmed : null;
  $: graph = found?.detail.graph ?? null;
  $: retired = found?.catalogState.kind === "retired";
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
      detail = confirmRead(detail, begun.generation, { kind: "found", detail: full, catalogState });
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

  /**
   * Start is one header action to the existing start door, not a second one
   * built here: this page names which workflow, `/atelier/new` still owns
   * choosing it, binding agent roles, and confirming the run. Preselecting
   * this workflow there is a named, deferred convenience.
   */
  function goToStart(): void {
    navigate("/atelier/new");
  }
</script>

<section class="surface" aria-labelledby="workflow-detail-title">
  <BackLink label={catalog.label} path={catalog.path} {navigate} />

  <ReadState read={detail} label="workflow detail" onRetry={() => { void load(); }} />
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
        {#if graph.workflow_format_version === 3 && graph.description !== null}
          <p class="muted description">{graph.description}</p>
        {/if}
      </div>
      <button
        type="button"
        class="primary"
        disabled={retired || (graph.workflow_format_version === 3 && !graph.executable)}
        onclick={goToStart}
      >{wrapDisplayCopy(catalogPageCopy.start)}</button>
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

    {#if selectedPreview !== null}
      <WorkflowNodePreviewPanel preview={selectedPreview} onClose={closePanel} />
    {/if}
  {/if}
</section>

<style>
  .muted {
    color: var(--ink-dim);
  }

  /* A description reads as prose about the workflow, not a system line, the
     same visual marker the run page's own description wears. */
  .description {
    font-style: italic;
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
</style>
