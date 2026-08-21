<script lang="ts">
  import { onMount, tick } from "svelte";

  import { createCockpitApi, type CockpitApi } from "./api/client";
  import {
    MutationJournal,
    createReconcileCommandId as makeReconcileCommandId,
    createRunId as makeRunId
  } from "./lib/mutationJournal";
  import { cockpitRoute } from "./lib/route";
  import WorkshopShell from "./components/WorkshopShell.svelte";
  import NewRunPage from "./pages/NewRunPage.svelte";
  import RunCockpitPage from "./pages/RunCockpitPage.svelte";
  import ProjectPage from "./pages/ProjectPage.svelte";
  import StudioPage from "./pages/StudioPage.svelte";

  export let cockpitApi: CockpitApi = createCockpitApi();
  export let mutationJournal: MutationJournal = new MutationJournal(sessionStorage);
  export let createRunId: () => string = makeRunId;
  export let createReconcileCommandId: () => string = makeReconcileCommandId;

  let route = cockpitRoute(window.location.pathname);
  let workshopShell: WorkshopShell;

  onMount(() => {
    const readRoute = () => { route = cockpitRoute(window.location.pathname); };
    window.addEventListener("popstate", readRoute);
    return () => window.removeEventListener("popstate", readRoute);
  });

  async function navigate(path: string): Promise<void> {
    window.history.pushState(null, "", path);
    route = cockpitRoute(path);
    await tick();
    workshopShell?.focusStage();
  }
</script>

<svelte:head><meta name="theme-color" content="#f2efe7" /><title>Atelier 2</title></svelte:head>

<WorkshopShell bind:this={workshopShell} {route} {navigate}>
  {#if route.page === "studio"}
    <StudioPage {cockpitApi} {navigate} />
  {:else if route.page === "project"}
    <ProjectPage {cockpitApi} {navigate} />
  {:else if route.page === "new"}
    <NewRunPage {cockpitApi} {mutationJournal} {navigate} {createRunId} />
  {:else if route.page === "run"}
    <RunCockpitPage
      {cockpitApi}
      {mutationJournal}
      publicReference={route.publicReference}
      {navigate}
      {createReconcileCommandId}
    />
  {:else}
    <section><p class="eyebrow">Atelier 2</p><h1>Page not found</h1><a class="button" href="/atelier" onclick={(event) => { event.preventDefault(); navigate("/atelier"); }}>Studio</a></section>
  {/if}
</WorkshopShell>
