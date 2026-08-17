<script lang="ts">
  import type { RunV3 } from "../api/client";
  import InfoHint from "./InfoHint.svelte";
  import StateMark from "./StateMark.svelte";

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
  <header class="run-header">
    <div>
      <p class="eyebrow">Durable run</p>
      <h1 id="v3-run-title">Run {run.run_id}</h1>
    </div>
    <p class="standing" aria-label="Where this run stands">
      <StateMark state={run.state === "COMPLETED" ? "succeeded" : "working"} />
      <span class="snapshot">
        Snapshot
        <InfoHint
          label="Why this page does not follow the run live"
          exact="A version 3 run has no event stream yet, so this page does not follow the run live: it shows the run as it was read. Reload to see further."
        />
      </span>
    </p>
  </header>

  <ol class="rail">
    {#each rail as entry (entry.node_id)}
      <li class="rail-entry" class:current={entry.node_id === run.current_node_id}>
        <StateMark state={entry.state} />
        <span class="node-id">{entry.node_id}</span>
      </li>
    {/each}
  </ol>

  <dl class="facts">
    <dt>Terminal hash</dt>
    <dd>
      {#if run.terminal_hash === null}
        <span class="muted">not yet</span>
      {:else}
        <code>{run.terminal_hash}</code>
      {/if}
    </dd>
    <dt>Run configuration</dt>
    <dd><code>{run.run_configuration_revision_hash}</code></dd>
    <dt>Workflow revision</dt>
    <dd><code>{run.workflow_revision_hash}</code></dd>
  </dl>
</section>

<style>
  .v3-run { display: grid; gap: 1rem; }
  .run-header { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: baseline; justify-content: space-between; }
  .standing { display: flex; align-items: center; gap: 0.75rem; margin: 0; }
  .snapshot { display: inline-flex; align-items: center; gap: 0.25rem; opacity: 0.75; }
  .rail { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.4rem; }
  .rail-entry { display: flex; align-items: center; gap: 0.6rem; padding: 0.4rem 0.6rem; border-radius: 0.4rem; }
  .rail-entry.current { background: color-mix(in srgb, currentColor 8%, transparent); }
  .node-id { font-weight: 600; }
  .facts { display: grid; grid-template-columns: auto 1fr; gap: 0.3rem 1rem; margin: 0; }
  .facts dd { margin: 0; overflow-wrap: anywhere; }
  .muted { opacity: 0.7; }
</style>
