<script lang="ts">
  import { onMount, tick } from "svelte";

  import { createCockpitApi, type CockpitApi } from "./api/client";
  import {
    MutationJournal,
    createReconcileCommandId as makeReconcileCommandId,
    createRunId as makeRunId
  } from "./lib/mutationJournal";
  import { PRODUCT_NAME } from "./lib/productName";
  import { cockpitRoute } from "./lib/route";
  import WorkshopShell from "./components/WorkshopShell.svelte";
  import WorkbenchPage from "./pages/WorkbenchPage.svelte";
  import NewRunPage from "./pages/NewRunPage.svelte";
  import RunCockpitPage from "./pages/RunCockpitPage.svelte";
  import ProjectPage from "./pages/ProjectPage.svelte";
  import StudioPage from "./pages/StudioPage.svelte";
  import WorkflowDetailPage from "./pages/WorkflowDetailPage.svelte";
  import WorkflowsPage from "./pages/WorkflowsPage.svelte";
  import CatalogPage from "./pages/CatalogPage.svelte";
  import HistoryPage from "./pages/HistoryPage.svelte";

  export let cockpitApi: CockpitApi = createCockpitApi();
  export let mutationJournal: MutationJournal = new MutationJournal(sessionStorage);
  export let createRunId: () => string = makeRunId;
  export let createReconcileCommandId: () => string = makeReconcileCommandId;

  let route = cockpitRoute(window.location.pathname + window.location.search);
  let workshopShell: WorkshopShell;

  onMount(() => {
    const readRoute = () => {
      route = cockpitRoute(window.location.pathname + window.location.search);
    };
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

<svelte:head><meta name="theme-color" content="#f2efe7" /><title>{PRODUCT_NAME}</title></svelte:head>

<WorkshopShell bind:this={workshopShell} {route} {navigate}>
  {#if route.page === "chat"}
    <WorkbenchPage {cockpitApi} {mutationJournal} {navigate} />
  {:else if route.page === "studio"}
    <StudioPage {cockpitApi} {mutationJournal} {navigate} />
  {:else if route.page === "project"}
    <ProjectPage {cockpitApi} {navigate} />
  {:else if route.page === "new"}
    <NewRunPage {cockpitApi} {mutationJournal} {navigate} {createRunId} />
  {:else if route.page === "workflows"}
    <WorkflowsPage {cockpitApi} {navigate} />
  {:else if route.page === "catalog"}
    <CatalogPage {cockpitApi} {navigate} />
  {:else if route.page === "workflow"}
    <WorkflowDetailPage {cockpitApi} {navigate} name={route.name} />
  {:else if route.page === "history"}
    <HistoryPage {cockpitApi} {navigate} />
  {:else if route.page === "run"}
    <RunCockpitPage
      {cockpitApi}
      {mutationJournal}
      publicReference={route.publicReference}
      origin={route.origin}
      {navigate}
      {createReconcileCommandId}
    />
  {:else}
    <section class="surface">
      <header class="surface-head">
        <h1>Page not found</h1>
        <p>No page lives at this address. The Board holds what is running.</p>
      </header>
      <a class="button primary" href="/atelier" onclick={(event) => { event.preventDefault(); navigate("/atelier"); }}>Board</a>
    </section>
  {/if}
</WorkshopShell>
