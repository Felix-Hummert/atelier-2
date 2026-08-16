<script lang="ts" context="module">
  /** One walkable step of the trail; the level being looked at carries no path. */
  export interface TrailStep {
    label: string;
    path: string;
  }
</script>

<script lang="ts">
  export let steps: readonly TrailStep[];
  export let current: string;
  export let navigate: (path: string) => void;
</script>

<nav class="trail" aria-label="Where you are">
  {#each steps as step (step.path)}
    <a
      href={step.path}
      onclick={(event) => { event.preventDefault(); navigate(step.path); }}
    >{step.label}</a>
    <span class="trail-step" aria-hidden="true">›</span>
  {/each}
  <span class="trail-here">{current}</span>
</nav>
