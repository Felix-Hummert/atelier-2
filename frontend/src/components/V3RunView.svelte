<script lang="ts">
  import type { RunV3 } from "../api/client";
  import StateMark, { stateLabels } from "./StateMark.svelte";

  export let run: RunV3;

  /**
   * The rail is rendered, not derived.
   *
   * A V1 or V2 run is folded here in the browser because its events arrive one
   * at a time and the page has to keep up. A V3 run carries the rail the server
   * already walked, along the one edge its author declared, so recomputing it
   * here would be a second owner of the same order -- and the browser's copy has
   * no way to walk a V3 graph anyway, because the wire carries no nodes for one.
   */
  $: rail = run.node_rail;
</script>

<section class="v3-run" aria-labelledby="v3-run-title">
  <header>
    <p class="eyebrow">Version 3 run</p>
    <h2 id="v3-run-title">{run.run_id}</h2>
    <p class="state">
      <StateMark state={run.state === "COMPLETED" ? "succeeded" : "working"} />
      {run.state === "COMPLETED" ? "completed" : "running"}
      <span class="muted">at {run.current_node_id}</span>
    </p>
  </header>

  <ol class="rail">
    {#each rail as entry (entry.node_id)}
      <li class="rail-entry" class:current={entry.node_id === run.current_node_id}>
        <StateMark state={entry.state} />
        <span class="node-id">{entry.node_id}</span>
        <span class="node-state">{stateLabels[entry.state]}</span>
      </li>
    {/each}
  </ol>

  <dl class="facts">
    <dt>Terminal hash</dt>
    <dd>
      {#if run.terminal_hash === null}
        <span class="muted">not yet — this run has not ended</span>
      {:else}
        <code>{run.terminal_hash}</code>
      {/if}
    </dd>
    <dt>Run configuration</dt>
    <dd><code>{run.run_configuration_revision_hash}</code></dd>
    <dt>Workflow revision</dt>
    <dd><code>{run.workflow_revision_hash}</code></dd>
  </dl>

  <p class="muted standing">
    This view does not follow the run live: a version 3 run has no event stream
    yet, so what is shown is the snapshot as it was read. Reload to see further.
  </p>
</section>

<style>
  .v3-run { display: grid; gap: 1rem; }
  .state { display: flex; align-items: center; gap: 0.5rem; }
  .rail { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.4rem; }
  .rail-entry { display: flex; align-items: center; gap: 0.6rem; padding: 0.4rem 0.6rem; border-radius: 0.4rem; }
  .rail-entry.current { background: color-mix(in srgb, currentColor 8%, transparent); }
  .node-id { font-weight: 600; }
  .node-state { opacity: 0.75; }
  .facts { display: grid; grid-template-columns: auto 1fr; gap: 0.3rem 1rem; margin: 0; }
  .facts dd { margin: 0; overflow-wrap: anywhere; }
  .muted { opacity: 0.7; }
  .standing { font-size: 0.9em; }
</style>
