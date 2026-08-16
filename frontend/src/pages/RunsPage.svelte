<script lang="ts">
  import { onMount } from "svelte";

  import type { CockpitApi, RunPage } from "../api/client";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import { confirmResource, startLoading, type RetainedResource } from "../lib/runProjection";
  import { waitsForAHuman } from "../lib/runState";

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
      failureMessage = error instanceof Error ? error.message : "The run list could not be loaded.";
      runs = { ...runs, request: { state: "idle" } };
    }
  }
</script>

<section aria-labelledby="runs-title">
  <a class="back-link" href="/atelier" onclick={(event) => { event.preventDefault(); navigate("/atelier"); }}>Studio</a>

  <header class="page-header">
    <div>
      <p class="eyebrow">Durable work</p>
      <h1 id="runs-title">Runs</h1>
    </div>
    <a class="button primary" href="/atelier/new" onclick={(event) => { event.preventDefault(); navigate("/atelier/new"); }}>New</a>
  </header>

  <div class="toolbar">
    <button class="quiet" type="button" onclick={load}>Refresh</button>
  </div>

  {#if failureMessage !== null}
    <ProblemNotice message={failureMessage} />
  {/if}
  {#if runs.confirmed !== null}
    {#if runs.confirmed.items.length === 0}
      <div class="empty"><h2>No runs yet</h2><p>Start one from a saved or newly published workflow.</p></div>
    {:else}
      <ul class="card-list">
        {#each runs.confirmed.items as run (run.public_run_reference)}
          <li>
            <a class="run-card" href={`/atelier/runs/${run.public_run_reference}`} onclick={(event) => { event.preventDefault(); navigate(`/atelier/runs/${run.public_run_reference}`); }}>
              <strong>{run.run_id}</strong>
              <span class:state-done={run.state === "COMPLETED"} class:state-waiting={waitsForAHuman(run.state)} class="state-label"><span aria-hidden="true">{run.state === "COMPLETED" ? "●" : waitsForAHuman(run.state) ? "⬢" : "▲"}</span>{run.state.replaceAll("_", " ").toLowerCase()}</span>
            </a>
          </li>
        {/each}
      </ul>
    {/if}
  {:else if runs.request.state === "loading"}
    <p class="status" role="status">Loading durable runs…</p>
  {/if}
</section>
