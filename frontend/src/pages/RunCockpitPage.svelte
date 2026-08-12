<script lang="ts">
  import { onMount } from "svelte";

  import type { CockpitApi, Run } from "../api/client";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import { confirmResource, startLoading, type RetainedResource } from "../lib/runProjection";

  export let cockpitApi: CockpitApi;
  export let publicReference: string;
  export let navigate: (path: string) => void;

  let run: RetainedResource<Run> = { confirmed: null, request: { state: "idle" } };
  let failureMessage: string | null = null;

  onMount(load);

  async function load(): Promise<void> {
    run = startLoading(run);
    failureMessage = null;
    try {
      run = confirmResource(run, await cockpitApi.getRun(publicReference));
    } catch (error) {
      failureMessage = error instanceof Error ? error.message : "The durable run could not be loaded.";
      run = { ...run, request: { state: "idle" } };
    }
  }
</script>

<section aria-labelledby="run-title">
  <a class="back-link" href="/atelier/runs" onclick={(event) => { event.preventDefault(); navigate("/atelier/runs"); }}>← Runs</a>
  {#if failureMessage !== null}<ProblemNotice message={failureMessage} />{/if}
  {#if run.confirmed !== null}
    <p class="eyebrow">Durable run</p>
    <h1 id="run-title">Run {run.confirmed.run_id}</h1>
    <dl class="run-summary"><div><dt>State</dt><dd>{run.confirmed.state.replaceAll("_", " ").toLowerCase()}</dd></div><div><dt>Workflow</dt><dd><code>{run.confirmed.workflow_revision_hash}</code></dd></div></dl>
  {:else if run.request.state === "loading"}
    <p class="status" role="status">Loading durable run…</p>
  {/if}
</section>
