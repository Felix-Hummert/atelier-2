<script lang="ts">
  import { THE_ONE_PROJECT } from "../lib/project";
  import type { CockpitRoute } from "../lib/route";
  import {
    WORKSHOP_DESTINATIONS,
    activeWorkshopDestination,
    destinationIsReachable
  } from "../lib/workshop";

  export let route: CockpitRoute;
  export let navigate: (path: string) => void;

  const destinationMarks: Record<(typeof WORKSHOP_DESTINATIONS)[number]["id"], string> = {
    studio: "✳",
    projects: "▣",
    runs: "▷",
    library: "❖",
    settings: "⚙"
  };

  $: active = activeWorkshopDestination(route);
</script>

<div class="workshop">
  <header class="workshop-topbar">
    <span class="wordmark">atelier<b>·2</b></span>
    <button
      class="project-chip"
      type="button"
      title="Open the project"
      onclick={() => navigate("/atelier/project")}
    >
      <span class="project-chip-dot" aria-hidden="true"></span>
      <span>{THE_ONE_PROJECT}</span>
      <small>project</small>
    </button>
  </header>

  <nav class="workshop-rail" aria-label="Workshop">
    {#each WORKSHOP_DESTINATIONS as destination (destination.id)}
      {#if destinationIsReachable(destination)}
        <a
          class="nav-destination"
          class:active={active === destination.id}
          href={destination.path}
          aria-current={active === destination.id ? "page" : undefined}
          onclick={(event) => {
            event.preventDefault();
            navigate(destination.path);
          }}
        >
          <span class="nav-destination-mark" aria-hidden="true">{destinationMarks[destination.id]}</span>
          <span class="nav-destination-label">{destination.label}</span>
        </a>
      {:else}
        <span class="nav-destination unavailable" aria-disabled="true" title={destination.vision}>
          <span class="nav-destination-mark" aria-hidden="true">{destinationMarks[destination.id]}</span>
          <span class="nav-destination-label">{destination.label}</span>
          <small class="nav-destination-vision">{destination.visionRef}</small>
        </span>
      {/if}
    {/each}
  </nav>

  <main class="workshop-stage">
    <slot />
  </main>
</div>
