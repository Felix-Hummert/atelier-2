<script lang="ts">
  import type { Run } from "../api/client";
  import InfoHint from "./InfoHint.svelte";
  import { countStanding, standingMarks } from "../lib/runState";

  export let runs: readonly Run[];
  export let navigate: (path: string) => void;

  /**
   * The label is client-owned wording, never a stored name: the wire serves no
   * name field for a run, a workflow, or this installation, so a project name
   * here would look like a saved datum and be none. There is exactly one card
   * because there is exactly one installation.
   */
  const label = "This workshop";

  $: running = countStanding(runs, "running");
  $: waiting = countStanding(runs, "waiting");
</script>

<article class="project-card" aria-label={label}>
  <div class="project-head">
    <a
      class="project-open"
      href="/atelier/runs"
      onclick={(event) => { event.preventDefault(); navigate("/atelier/runs"); }}
    >{label}<span class="project-enter" aria-hidden="true">›</span></a>
    <InfoHint
      label="Why one project"
      exact="One installation, one project. The backend has no project register yet."
    />
  </div>
  <p class="project-counts">
    <span class="project-count"><span aria-hidden="true">{standingMarks.running}</span>{running} running</span>
    <span class="project-count" class:project-count-waiting={waiting > 0}><span aria-hidden="true">{standingMarks.waiting}</span>{waiting} waiting for you</span>
  </p>
</article>
