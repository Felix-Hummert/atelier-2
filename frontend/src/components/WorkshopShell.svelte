<script lang="ts">
  import { THE_ONE_PROJECT } from "../lib/project";
  import type { CockpitRoute } from "../lib/route";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { railCopy } from "../lib/railCopy";
  import {
    WORKSHOP_DESTINATIONS,
    activeWorkshopDestination,
    boardBadgeCounts,
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
    <div class="rail-brand">{wrapDisplayCopy(railCopy.brand)}</div>

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
          {#if destination.id === "board" && $boardBadgeCounts !== null}
            {#if $boardBadgeCounts.running > 0}
              <span class="rail-badge rail-badge-running" aria-label={`${$boardBadgeCounts.running} ${wrapDisplayCopy(railCopy.runningBadgeSuffix)}`}>{$boardBadgeCounts.running}</span>
            {/if}
            {#if $boardBadgeCounts.needsYou > 0}
              <span class="rail-badge rail-badge-needs-you" aria-label={`${$boardBadgeCounts.needsYou} ${wrapDisplayCopy(railCopy.needsYouBadgeSuffix)}`}>{$boardBadgeCounts.needsYou}</span>
            {/if}
          {/if}
        </a>
      {:else}
        <span class="nav-destination unavailable" aria-disabled="true" title={wrapDisplayCopy(destination.vision)}>
          <span class="nav-destination-mark" aria-hidden="true">{destinationMarks[destination.id]}</span>
          <span class="nav-destination-label">{wrapDisplayCopy(destination.label)}</span>
          <small class="nav-destination-vision">{wrapDisplayCopy(railCopy.later)}</small>
        </span>
      {/if}
    {/each}

    <div class="rail-grow"></div>

    <div class="rail-project" title={wrapDisplayCopy(railCopy.switchProjectHint)}>
      <b>{THE_ONE_PROJECT}</b>
      <span>{wrapDisplayCopy(railCopy.switchProject)}</span>
    </div>
    <div class="rail-settings">
      <span aria-hidden="true">⚙</span>
      <span title={wrapDisplayCopy(railCopy.settingsHint)}>{wrapDisplayCopy(railCopy.settings)}</span>
      ·
      <span aria-hidden="true">◯</span>
      <span title={wrapDisplayCopy(railCopy.profileHint)}>{wrapDisplayCopy(railCopy.profile)}</span>
      <small>{wrapDisplayCopy(railCopy.later)}</small>
    </div>
  </nav>

  <main bind:this={stage} class="workshop-stage" tabindex="-1">
    <slot />
  </main>
</div>

<style>
  .rail-badge {
    margin-left: auto;
    border-radius: var(--r-pill);
    padding: 0.02rem 0.4rem;
    font-size: var(--text-2xs);
    font-weight: 700;
    color: var(--accent-ink);
  }

  /* Only the first rendered badge pushes the group right; a second sits beside it. */
  .rail-badge + .rail-badge {
    margin-left: 0;
  }

  .rail-badge-running {
    background: var(--blue);
  }

  .rail-badge-needs-you {
    background: var(--amber);
  }
</style>
