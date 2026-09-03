<script lang="ts">
  import { THE_ONE_PROJECT } from "../lib/project";
  import type { CockpitRoute } from "../lib/route";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { railCopy } from "../lib/railCopy";
  import { exactLocal } from "../lib/when";
  import { reloadPage } from "../lib/pageReload";
  import { loadedVersion, newVersionAvailable } from "../lib/versionState";
  import {
    WORKSHOP_DESTINATION,
    WORKSHOP_ROOMS,
    activeWorkshopDestination,
    runsWaitingForYou
  } from "../lib/workshop";

  export let route: CockpitRoute;
  export let navigate: (path: string) => void;
  let stage: HTMLElement;

  export function focusStage(): void {
    stage.focus();
  }

  const settings = WORKSHOP_DESTINATION.settings;
  // Short enough to sit quietly on one line; the full hash still lives in
  // the line's own `title` for the one time someone wants to check it.
  const SHORT_COMMIT_LENGTH = 8;

  $: active = activeWorkshopDestination(route);
</script>

<div class="workshop">
  <nav class="workshop-rail" aria-label="Workshop">
    <div class="rail-brand">{wrapDisplayCopy(railCopy.brand)}</div>

    {#each WORKSHOP_ROOMS as room (room.id)}
      <a
        class="nav-destination"
        class:active={active === room.id}
        href={room.path}
        aria-current={active === room.id ? "page" : undefined}
        onclick={(event) => {
          event.preventDefault();
          navigate(room.path);
        }}
      >
        <span class="nav-destination-mark" aria-hidden="true">{room.glyph}</span>
        <span class="nav-destination-label">{wrapDisplayCopy(room.label)}</span>
        <!-- The one number the rail carries, and only where something wants
             you: the count in the rail is the notification (ADR 0019 §1). -->
        {#if room.id === "workbench" && $runsWaitingForYou !== null && $runsWaitingForYou > 0}
          <span
            class="rail-count"
            aria-label={`${$runsWaitingForYou} ${wrapDisplayCopy(railCopy.needsYouCountSuffix)}`}
          >{$runsWaitingForYou}</span>
        {/if}
      </a>
    {/each}

    <div class="rail-grow"></div>

    <!-- Settings stands at the foot, set apart by a line: the context above the
         three rooms, carrying the name of the project it is the context of. -->
    <a
      class="nav-destination rail-foot"
      class:active={active === settings.id}
      href={settings.path}
      aria-current={active === settings.id ? "page" : undefined}
      onclick={(event) => {
        event.preventDefault();
        navigate(settings.path);
      }}
    >
      <span class="nav-destination-mark" aria-hidden="true">{settings.glyph}</span>
      <span class="nav-destination-label">{wrapDisplayCopy(settings.label)}</span>
      <span class="rail-project">{THE_ONE_PROJECT}</span>
    </a>

    <!-- Provenance on the back (HEART): who is running, never in the way. -->
    {#if $newVersionAvailable}
      <p class="serve-footer new-version" role="status">
        <span>{wrapDisplayCopy(railCopy.newVersionAvailable)}</span>
        <button type="button" class="quiet" onclick={reloadPage}>
          {wrapDisplayCopy(railCopy.reload)}
        </button>
      </p>
    {:else if $loadedVersion}
      <p class="serve-footer" title={$loadedVersion.commit}>
        {$loadedVersion.commit.slice(0, SHORT_COMMIT_LENGTH)} · {exactLocal($loadedVersion.deployedAt)}
      </p>
    {/if}
  </nav>

  <main bind:this={stage} class="workshop-stage" tabindex="-1">
    <slot />
  </main>
</div>

<style>
  /* Ochre, and only where something wants you: the count carries the state's
     own hue, never a second sentence beside it. */
  .rail-count {
    margin-left: auto;
    border-radius: var(--r-pill);
    padding: 0 var(--space-2);
    background: var(--signal-attention);
    color: var(--signal-ink);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
  }

  /* Provenance on the back (HEART): a hairline, small and uncoloured, so it
     reads as a signature rather than a status. */
  .serve-footer {
    margin: var(--space-2) 0 0;
    padding-top: var(--space-3);
    border-top: var(--edge) solid var(--line);
    color: var(--ink-dim);
    font-size: var(--text-2xs);
  }

  /* The one state that does earn a control: an operator confirms the
     reload instead of the page taking the decision for them (#1100). */
  .serve-footer.new-version {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .serve-footer button {
    padding: 0;
    color: inherit;
    font-size: inherit;
    text-decoration: underline;
  }
</style>
