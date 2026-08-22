<script lang="ts">
  import { THE_ONE_PROJECT } from "../lib/project";
  import type { CockpitRoute } from "../lib/route";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import {
    WORKSHOP_DESTINATIONS,
    activeWorkshopDestination,
    destinationIsReachable
  } from "../lib/workshop";

  export let route: CockpitRoute;
  export let navigate: (path: string) => void;
  let stage: HTMLElement;

  export function focusStage(): void {
    stage.focus();
  }

  const destinationMarks: Record<(typeof WORKSHOP_DESTINATIONS)[number]["id"], string> = {
    chat: "▸",
    board: "◫",
    workflows: "⧉",
    history: "≡"
  };

  $: active = activeWorkshopDestination(route);
</script>

<div class="workshop">
  <nav class="workshop-rail" aria-label="Workshop">
    <div class="rail-brand">atelier</div>

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
          <span class="nav-destination-label">{wrapDisplayCopy(destination.label)}</span>
        </a>
      {:else}
        <span class="nav-destination unavailable" aria-disabled="true" title={destination.vision}>
          <span class="nav-destination-mark" aria-hidden="true">{destinationMarks[destination.id]}</span>
          <span class="nav-destination-label">{wrapDisplayCopy(destination.label)}</span>
          <small class="nav-destination-vision">{destination.visionRef}</small>
        </span>
      {/if}
    {/each}

    <div class="rail-grow"></div>

    <div class="rail-project" title="One project today — a real switcher is a later #133 slot.">
      <b>{THE_ONE_PROJECT}</b>
      <span>switch project</span>
    </div>
    <div class="rail-settings">
      <span aria-hidden="true">⚙</span>
      <span title="Professional settings surface — not built yet. REQ-UI-15.">Settings</span>
      ·
      <span aria-hidden="true">◯</span>
      <span title="Profile needs login/OIDC — not built yet. #82.">Profile</span>
      <small>(later)</small>
    </div>
  </nav>

  <main bind:this={stage} class="workshop-stage" tabindex="-1">
    <slot />
  </main>
</div>
