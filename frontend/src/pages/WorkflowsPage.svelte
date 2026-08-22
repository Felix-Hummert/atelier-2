<script lang="ts">
  import { onMount } from "svelte";

  import type { CockpitApi, WorkflowRevisionSummary } from "../api/client";
  import ReadState from "../components/ReadState.svelte";
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
  import { workflowPath } from "../lib/route";
  import { groupSavedWorkflows, type SavedWorkflowRow } from "../lib/savedWorkflows";
  import { workflowsPageCopy } from "../lib/workflowsPageCopy";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;

  type ReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  type NamedWorkflowRow = SavedWorkflowRow & { name: string };

  let revisions: RetainedRead<WorkflowRevisionSummary[], ReadFailure> =
    retainedRead<WorkflowRevisionSummary[], ReadFailure>();
  let failureMessage: string | null = null;

  /**
   * One card per published name, newest revision first.
   *
   * The library shows names, never hashes (REQ-UI-05): the unnamed revision
   * every fresh publish starts as, and every revision published straight
   * through the API without a `name:` field, has no card here at all. This
   * groups by document name rather than by formal catalog-lineage admission,
   * because a name a person can read is what makes a revision "a workflow"
   * on this page -- the catalog-admission bureaucracy is a separate act this
   * page does not gate on.
   */
  $: namedRows = groupSavedWorkflows(revisions.confirmed ?? []).filter(
    (row): row is NamedWorkflowRow => row.name !== null
  );

  onMount(() => {
    void loadRevisions();
  });

  async function loadRevisions(): Promise<void> {
    failureMessage = null;
    const begun = beginRead(revisions);
    revisions = begun.read;
    try {
      const reading = await readEveryRevision((after) => cockpitApi.listWorkflowRevisions(after));
      if (!reading.complete) {
        revisions = failRead(revisions, begun.generation, {
          kind: "incomplete",
          title: workflowsPageCopy.listIncomplete
        });
        return;
      }
      revisions = confirmRead(revisions, begun.generation, reading.revisions);
    } catch (error) {
      failureMessage = humanErrorMessage(error, workflowsPageCopy.listUnavailable);
      revisions = failRead(revisions, begun.generation, {
        kind: "unavailable",
        title: workflowsPageCopy.listUnavailable
      });
    }
  }

  function open(name: string): void {
    navigate(workflowPath(name));
  }
</script>

<section aria-labelledby="workflows-title">
  <p class="eyebrow">{wrapDisplayCopy(workflowsPageCopy.eyebrow)}</p>
  <h1 id="workflows-title">{wrapDisplayCopy(workflowsPageCopy.title)}</h1>

  <ReadState read={revisions} label="workflows" onRetry={() => { void loadRevisions(); }} />
  {#if failureMessage !== null}<p class="failure" role="alert">{failureMessage}</p>{/if}

  {#if revisions.confirmed !== null && namedRows.length === 0}
    <p class="empty-title">{wrapDisplayCopy(workflowsPageCopy.emptyTitle)}</p>
    <p class="muted">{wrapDisplayCopy(workflowsPageCopy.emptyDescription)}</p>
  {/if}

  <ul class="workflow-cards">
    {#each namedRows as row (row.name)}
      {@const head = row.revisions[0]}
      <li>
        <button type="button" class="workflow-card" onclick={() => open(row.name)}>
          <strong>{row.name}</strong>
          <span class="muted">{head?.description ?? wrapDisplayCopy(workflowsPageCopy.noDescription)}</span>
        </button>
      </li>
    {/each}
  </ul>
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
    margin: 0.2rem 0 1rem;
  }

  .empty-title {
    margin: 0 0 0.2rem;
    font-weight: 600;
  }

  .muted {
    color: var(--muted);
  }

  .failure {
    color: var(--danger);
  }

  .workflow-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr));
    gap: 0.75rem;
    margin: 0.5rem 0 0;
    padding: 0;
    list-style: none;
  }

  .workflow-card {
    display: grid;
    gap: 0.3rem;
    width: 100%;
    padding: 0.8rem 1rem;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    background: var(--panel);
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .workflow-card:hover,
  .workflow-card:focus-visible {
    border-color: var(--accent);
  }

  .workflow-card strong {
    font-size: 0.95rem;
  }

  .workflow-card span {
    font-size: 0.85rem;
  }
</style>
