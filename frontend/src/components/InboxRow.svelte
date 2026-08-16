<script lang="ts">
  import type { Run } from "../api/client";
  import { humanMove, standingMarks, waitsForAHuman } from "../lib/runState";

  export let runs: readonly Run[];
  export let navigate: (path: string) => void;

  $: waiting = runs.filter((run) => waitsForAHuman(run.state));
</script>

{#if waiting.length > 0}
  <section class="inbox" aria-labelledby="inbox-title">
    <h2 id="inbox-title">Waiting for you</h2>
    <ul class="card-list">
      {#each waiting as run (run.public_run_reference)}
        <li>
          <a
            class="run-card"
            href={`/atelier/runs/${run.public_run_reference}`}
            onclick={(event) => { event.preventDefault(); navigate(`/atelier/runs/${run.public_run_reference}`); }}
          >
            <span class="state-label state-waiting"><span aria-hidden="true">{standingMarks.waiting}</span>{humanMove(run.state)}</span>
            <code>{run.run_id}</code>
          </a>
        </li>
      {/each}
    </ul>
  </section>
{/if}
