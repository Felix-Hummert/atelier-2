<script lang="ts">
  import { onMount } from "svelte";

  import type { CockpitApi, WorkflowRevisionDetail } from "../api/client";
  import Breadcrumb from "../components/Breadcrumb.svelte";
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
  import { catalogStateNote, workflowFormatFact, workflowsPageCopy } from "../lib/workflowsPageCopy";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;
  export let name: string;

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
          title: workflowsPageCopy.detailUnavailable
        });
        return;
      }
      const catalogState = await catalogNameStateOf(name, (asked) => cockpitApi.getRevisionByName(asked));
      const newestByName = catalogHeadsOf(reading.revisions, { [name]: catalogState });
      if (newestByName === null) {
        detail = failRead(detail, begun.generation, {
          kind: "unavailable",
          title: workflowsPageCopy.detailUnavailable
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
      failureMessage = humanErrorMessage(error, workflowsPageCopy.detailUnavailable);
      detail = failRead(detail, begun.generation, {
        kind: "unavailable",
        title: workflowsPageCopy.detailUnavailable
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

<section aria-labelledby="workflow-detail-title">
  <Breadcrumb steps={[{ label: "Workflows", path: "/atelier/workflows" }]} current={name} {navigate} />

  <ReadState read={detail} label="workflow detail" onRetry={() => { void load(); }} />
  {#if failureMessage !== null}<p class="failure" role="alert">{failureMessage}</p>{/if}

  {#if detail.confirmed?.kind === "not-found"}
    <p class="empty-title">{wrapDisplayCopy(workflowsPageCopy.notFoundTitle)}</p>
    <p class="muted">{wrapDisplayCopy(workflowsPageCopy.notFoundDescription)}</p>
  {:else if graph !== null}
    <header class="detail-head">
      <div>
        <p class="eyebrow">{wrapDisplayCopy(workflowsPageCopy.eyebrow)}</p>
        <h1 id="workflow-detail-title">{name}</h1>
        {#if catalogNote !== null}
          <p class="note">{wrapDisplayCopy(catalogNote)}</p>
        {/if}
        {#if graph.workflow_format_version === 3 && graph.description !== null}
          <p class="muted">{graph.description}</p>
        {/if}
        <p class="fact">
          {workflowFormatFact(
            graph.workflow_format_version,
            graph.workflow_format_version === 3 ? graph.node_count : null
          )}
        </p>
      </div>
      <button
        type="button"
        class="primary"
        disabled={retired || (graph.workflow_format_version === 3 && !graph.executable)}
        onclick={goToStart}
      >{wrapDisplayCopy(workflowsPageCopy.start)}</button>
    </header>

    {#if retired}
      <p class="failure" role="alert">{wrapDisplayCopy(workflowsPageCopy.retiredNotice)}</p>
    {:else if graph.workflow_format_version === 3 && !graph.executable}
      <p class="failure" role="alert">{cannotBeStarted(graph.not_executable_reason)}</p>
    {/if}

    {#if previews !== null}
      <WorkflowGraphDrawing {previews} {loops} showLegend={true} onSelect={selectNode} {selectedNodeId} />
    {:else}
      <p class="muted">{wrapDisplayCopy(workflowsPageCopy.graphUnavailable)}</p>
    {/if}

    {#if selectedPreview !== null}
      <WorkflowNodePreviewPanel preview={selectedPreview} onClose={closePanel} />
    {/if}
  {/if}
</section>

<style>
  .eyebrow {
    margin: 0;
    color: var(--muted);
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  h1 {
    margin: 0.2rem 0 0.2rem;
  }

  .muted {
    color: var(--muted);
  }

  .fact {
    margin: 0.2rem 0 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .note {
    margin: 0.15rem 0 0;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--amber);
  }

  .failure {
    color: var(--danger);
  }

  .empty-title {
    margin: 0 0 0.2rem;
    font-weight: 600;
  }

  .detail-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .primary {
    flex: none;
    padding: 0.4rem 1rem;
    border: 1px solid var(--accent);
    border-radius: 0.45rem;
    background: var(--accent);
    color: var(--accent-ink);
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }

  .primary:disabled {
    background: transparent;
    color: var(--muted);
    border-color: var(--line);
    cursor: not-allowed;
  }
</style>
