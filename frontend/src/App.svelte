<script lang="ts">
  import { onMount } from "svelte";

  import { createCockpitApi, type CockpitApi } from "./api/client";
  import { MutationJournal, createRunId as makeRunId } from "./lib/mutationJournal";
  import { cockpitRoute } from "./lib/route";
  import NewRunPage from "./pages/NewRunPage.svelte";
  import RunCockpitPage from "./pages/RunCockpitPage.svelte";
  import RunsPage from "./pages/RunsPage.svelte";

  export let cockpitApi: CockpitApi = createCockpitApi();
  export let mutationJournal: MutationJournal = new MutationJournal(sessionStorage);
  export let createRunId: () => string = makeRunId;

  let route = cockpitRoute(window.location.pathname);

  onMount(() => {
    if (window.location.pathname === "/atelier" || window.location.pathname === "/atelier/") {
      window.history.replaceState(null, "", "/atelier/runs");
      route = cockpitRoute(window.location.pathname);
    }
    const readRoute = () => { route = cockpitRoute(window.location.pathname); };
    window.addEventListener("popstate", readRoute);
    return () => window.removeEventListener("popstate", readRoute);
  });

  function navigate(path: string): void {
    window.history.pushState(null, "", path);
    route = cockpitRoute(path);
  }
</script>

<svelte:head><meta name="theme-color" content="#f4f1e8" /><title>Atelier 2</title></svelte:head>

<main>
  {#if route.page === "runs"}
    <RunsPage {cockpitApi} {navigate} />
  {:else if route.page === "new"}
    <NewRunPage {cockpitApi} {mutationJournal} {navigate} {createRunId} />
  {:else if route.page === "run"}
    <RunCockpitPage {cockpitApi} publicReference={route.publicReference} {navigate} />
  {:else}
    <section><p class="eyebrow">Atelier 2</p><h1>Page not found</h1><a class="button" href="/atelier/runs" onclick={(event) => { event.preventDefault(); navigate("/atelier/runs"); }}>Runs</a></section>
  {/if}
</main>
