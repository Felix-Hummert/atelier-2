<script lang="ts">
  import { onMount } from "svelte";

  import type { CockpitApi } from "../api/client";
  import ReadState from "../components/ReadState.svelte";
  import { catalogHeadsOf, catalogNameStateOf, type CatalogNameState } from "../lib/catalogName";
  import { catalogWorkflowRows, type CatalogWorkflowRow } from "../lib/catalogRows";
  import { onConnectionRecovered } from "../lib/connectionState";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    type RetainedRead
  } from "../lib/readResource";
  import { readEveryRevision } from "../lib/runPages";
  import { workflowsPageCopy } from "../lib/workflowsPageCopy";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;

  type ReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  let workflows: RetainedRead<CatalogWorkflowRow[], ReadFailure> =
    retainedRead<CatalogWorkflowRow[], ReadFailure>();
  let failureMessage: string | null = null;

  /**
   * This room is the start room (operator ruling #684): only a workflow the
   * catalog has admitted is startable, so only its card appears here. A
   * published-but-not-admitted or retired name is the Catalog's story to
   * tell, not this one -- it stays out entirely rather than wearing a note
   * that would invite a click nothing here can honour.
   */
  $: startableRows = (workflows.confirmed ?? []).filter((row) => row.state?.kind === "startable");

  onMount(() => {
    void loadWorkflows();
    // A read that failed while the connection was lost is worth asking again
    // on its own once it returns, with no reload (#700).
    return onConnectionRecovered(() => { void loadWorkflows(); });
  });

  async function loadWorkflows(): Promise<void> {
    failureMessage = null;
    const begun = beginRead(workflows);
    workflows = begun.read;
    try {
      const reading = await readEveryRevision((after) => cockpitApi.listWorkflowRevisions(after));
      if (!reading.complete) {
        workflows = failRead(workflows, begun.generation, {
          kind: "incomplete",
          title: workflowsPageCopy.listIncomplete
        });
        return;
      }
      const names = [
        ...new Set(reading.revisions.flatMap((item) => (item.name === null ? [] : [item.name])))
      ];
      const catalogByName = Object.fromEntries(
        await Promise.all(
          names.map(async (name) => [
            name,
            await catalogNameStateOf(name, (asked) => cockpitApi.getRevisionByName(asked))
          ])
        )
      ) as Record<string, CatalogNameState>;
      if (catalogHeadsOf(reading.revisions, catalogByName) === null) {
        workflows = failRead(workflows, begun.generation, {
          kind: "unavailable",
          title: workflowsPageCopy.listUnavailable
        });
        return;
      }
      workflows = confirmRead(
        workflows,
        begun.generation,
        catalogWorkflowRows(reading.revisions, catalogByName)
      );
    } catch (error) {
      // A message identical to the failed read's own title would just repeat
      // it a second time (#700's own fallback for a round trip that never
      // happened is exactly that title); only a genuinely more specific
      // reading is worth this second line.
      const message = humanErrorMessage(error, workflowsPageCopy.listUnavailable);
      failureMessage = message === workflowsPageCopy.listUnavailable ? null : message;
      workflows = failRead(workflows, begun.generation, {
        kind: "unavailable",
        title: workflowsPageCopy.listUnavailable
      });
    }
  }

  /**
   * Every card leads to starting (operator ruling #684): this room offers no
   * detail view of its own, so a click goes straight to the start door's own
   * picker -- the same "Choose a workflow" step reachable from the Board.
   */
  function startWorkflow(): void {
    navigate("/atelier/new");
  }
</script>

<section class="surface" aria-labelledby="workflows-title">
  <header class="surface-head">
    <h1 id="workflows-title">{wrapDisplayCopy(workflowsPageCopy.title)}</h1>
    <p>{wrapDisplayCopy(workflowsPageCopy.lead)}</p>
  </header>

  <ReadState read={workflows} label="workflows" onRetry={() => { void loadWorkflows(); }} />
  {#if failureMessage !== null}<p class="failure" role="alert">{failureMessage}</p>{/if}

  {#if workflows.confirmed !== null && startableRows.length === 0}
    <div class="card empty-state">
      <h2>{wrapDisplayCopy(workflowsPageCopy.emptyTitle)}</h2>
      <p>{wrapDisplayCopy(workflowsPageCopy.emptyDescription)}</p>
      <a
        class="button primary"
        href="/atelier/catalog"
        onclick={(event) => { event.preventDefault(); navigate("/atelier/catalog"); }}
      >{wrapDisplayCopy(workflowsPageCopy.emptyNext)}</a>
    </div>
  {/if}

  <ul class="workflow-cards">
    {#each startableRows as row (row.revisionHash)}
      <li>
        <button type="button" class="workflow-card" onclick={startWorkflow}>
          <span class="workflow-card-head">
            <strong>{row.title}</strong>
          </span>
          <span class="muted">{row.description ?? wrapDisplayCopy(workflowsPageCopy.noDescription)}</span>
        </button>
      </li>
    {/each}
  </ul>
</section>

<style>
  h2 {
    margin: 0;
  }

  .muted {
    color: var(--ink-dim);
  }

  .failure {
    color: var(--signal-failure);
  }

  .workflow-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(var(--card-min), 1fr));
    gap: var(--space-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* The base button skin centres its content; a card is not a control label,
     so it takes the surface's own left edge back. */
  .workflow-card {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    justify-content: start;
    gap: var(--space-1);
    width: 100%;
    padding: var(--space-4) var(--space-5);
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    background: var(--panel2);
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .workflow-card:hover,
  .workflow-card:focus-visible {
    border-color: var(--accent);
  }

  .workflow-card-head {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
  }

  .workflow-card strong {
    font-size: var(--text-sm);
  }

  .workflow-card span {
    font-size: var(--text-xs);
  }
</style>
