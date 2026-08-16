<script lang="ts">
  import { onMount } from "svelte";

  import type { CockpitApi, RunPage } from "../api/client";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import { THE_ONE_PROJECT } from "../lib/project";
  import { confirmResource, startLoading, type RetainedResource } from "../lib/runProjection";
  import { humanMove, runsStanding, standingMarks, standingOrder, standingWords } from "../lib/runState";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;

  let runs: RetainedResource<RunPage> = { confirmed: null, request: { state: "idle" } };
  let failureMessage: string | null = null;

  onMount(load);

  async function load(): Promise<void> {
    runs = startLoading(runs);
    failureMessage = null;
    try {
      runs = confirmResource(runs, await cockpitApi.listRuns());
    } catch (error) {
      failureMessage = error instanceof Error ? error.message : "This project could not be read.";
      runs = { ...runs, request: { state: "idle" } };
    }
  }

  $: items = runs.confirmed?.items ?? [];
  // A next cursor means the durable page left runs behind it: the adapter reads
  // one row past the limit to decide it. Following those pages is not built, so
  // the level says so rather than presenting a part as the whole.
  $: partial = runs.confirmed?.next_after != null;
  $: groups = standingOrder
    .map((standing) => ({ standing, runs: runsStanding(items, standing) }))
    .filter((group) => group.runs.length > 0);
</script>

<section aria-labelledby="project-title">
  <a class="back-link" href="/atelier" onclick={(event) => { event.preventDefault(); navigate("/atelier"); }}>← Studio</a>

  <header class="page-header">
    <div>
      <p class="eyebrow">Project</p>
      <h1 id="project-title">{THE_ONE_PROJECT}</h1>
    </div>
  </header>

  <div class="toolbar">
    <button class="quiet" type="button" onclick={load}>Refresh</button>
  </div>

  {#if failureMessage !== null}
    <ProblemNotice message={failureMessage} />
  {/if}

  <section class="queue" aria-labelledby="queue-title">
    <h2 id="queue-title">Queue</h2>
    <p>This project has no priority and no assignment yet.</p>
    <a class="button primary" href="/atelier/new" onclick={(event) => { event.preventDefault(); navigate("/atelier/new"); }}>Start a run</a>
  </section>

  {#if runs.confirmed !== null}
    {#if partial}
      <p class="muted">Not every run of this project is on this page. Reading further is not built yet.</p>
    {/if}
    {#if groups.length === 0}
      <p class="muted">No runs here yet.</p>
    {/if}
    {#each groups as group (group.standing)}
      <section class="run-group" aria-labelledby={`group-${group.standing}`}>
        <h2 class="section-title" id={`group-${group.standing}`}>{standingWords[group.standing]}</h2>
        <ul class="card-list">
          {#each group.runs as run (run.public_run_reference)}
            <li>
              <a class="run-card" href={`/atelier/runs/${run.public_run_reference}`} onclick={(event) => { event.preventDefault(); navigate(`/atelier/runs/${run.public_run_reference}`); }}>
                <strong>{run.run_id}</strong>
                {#if group.standing === "waiting"}
                  <span class="state-label state-waiting"><span aria-hidden="true">{standingMarks.waiting}</span>{humanMove(run.state)}</span>
                {/if}
              </a>
            </li>
          {/each}
        </ul>
      </section>
    {/each}
  {:else if runs.request.state === "loading"}
    <p class="status" role="status">Looking…</p>
  {/if}
</section>
