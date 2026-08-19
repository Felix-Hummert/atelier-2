<script lang="ts">
  import InfoHint from "./InfoHint.svelte";
  import When from "./When.svelte";
  import { THE_ONE_PROJECT } from "../lib/project";
  import { standingMarks } from "../lib/runState";

  export let running: number;
  export let waiting: number;
  export let landed: number;
  export let lastLandedAt: string | null = null;
  export let navigate: (path: string) => void;

  const label = THE_ONE_PROJECT;
</script>

<article class="project-card" class:live={running > 0 || waiting > 0} aria-label={label}>
  <div class="project-head">
    <a
      class="project-open"
      href="/atelier/project"
      onclick={(event) => { event.preventDefault(); navigate("/atelier/project"); }}
    >{label}<span class="project-enter" aria-hidden="true">›</span></a>
    <InfoHint
      label="Why one project"
      exact="One installation, one project. The backend has no project register yet."
    />
  </div>
  <p class="project-counts">
    <span class="project-count"><span aria-hidden="true">{standingMarks.running}</span>{running} running</span>
    <span class="project-count" class:project-count-waiting={waiting > 0}><span aria-hidden="true">{standingMarks.waiting}</span>{waiting} waiting for you</span>
    <span class="project-count"><span aria-hidden="true">{standingMarks.done}</span>{landed} landed</span>
    {#if lastLandedAt !== null}
      <span class="project-count">last landing <When startedAt={lastLandedAt} kind="ago" /></span>
    {/if}
  </p>
</article>
