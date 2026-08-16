<script lang="ts">
  import { onMount } from "svelte";

  import type { CockpitApi, RunPage } from "../api/client";
  import InboxRow from "../components/InboxRow.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import ProjectCard from "../components/ProjectCard.svelte";
  import { confirmResource, startLoading, type RetainedResource } from "../lib/runProjection";

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
      failureMessage = error instanceof Error ? error.message : "The workshop could not be read.";
      runs = { ...runs, request: { state: "idle" } };
    }
  }

  $: items = runs.confirmed?.items ?? [];
  $: empty = runs.confirmed !== null && items.length === 0;
</script>

<section aria-labelledby="studio-title">
  <header class="page-header">
    <div>
      <p class="eyebrow">Atelier</p>
      <h1 id="studio-title">Studio</h1>
    </div>
    {#if !empty}
      <a class="button primary" href="/atelier/new" onclick={(event) => { event.preventDefault(); navigate("/atelier/new"); }}>Start</a>
    {/if}
  </header>

  {#if failureMessage !== null}
    <ProblemNotice message={failureMessage} />
  {/if}

  {#if runs.confirmed !== null}
    <InboxRow runs={items} {navigate} />

    {#if empty}
      <div class="empty">
        <h2>Nothing is running</h2>
        <p>A workflow becomes a run, and a run is what this workshop shows.</p>
        <a class="button primary" href="/atelier/new" onclick={(event) => { event.preventDefault(); navigate("/atelier/new"); }}>Start a run</a>
      </div>
    {:else}
      <h2 class="section-title">Projects</h2>
      <ProjectCard runs={items} {navigate} />
    {/if}
  {:else if runs.request.state === "loading"}
    <p class="status" role="status">Looking…</p>
  {/if}

  <section class="chat-door" aria-labelledby="chat-title">
    <h2 id="chat-title">Chat</h2>
    <p>The conductor is not built yet. When it is, it answers here — and the same door stands on the project and on the run.</p>
  </section>
</section>
