<script lang="ts">
  import type { AnyRun } from "../api/client";
  import { runPath } from "../lib/route";
  import { humanMove, standingMarks, waitsForAHuman } from "../lib/runState";

  export let runs: readonly AnyRun[];
  export let navigate: (path: string) => void;

  $: waiting = runs.filter((run) => waitsForAHuman(run.state));
</script>

{#if waiting.length > 0}
  <section class="inbox" aria-labelledby="inbox-title">
    <h2 id="inbox-title">Waiting for you</h2>
    <p class="inbox-count">{waiting.length} {waiting.length === 1 ? "needs" : "need"} you</p>
    <ul class="inbox-cards">
      {#each waiting as run (run.public_run_reference)}
        <li>
          <a
            class="inbox-card"
            href={runPath(run.public_run_reference)}
            onclick={(event) => { event.preventDefault(); navigate(runPath(run.public_run_reference)); }}
          >
            <span class="inbox-mark" aria-hidden="true">{standingMarks.waiting}</span>
            <span class="inbox-what">
              <strong>{run.run_id}</strong>
              <small>needs you</small>
            </span>
            <span class="inbox-go">{humanMove(run.state)}</span>
          </a>
        </li>
      {/each}
    </ul>
  </section>
{/if}
