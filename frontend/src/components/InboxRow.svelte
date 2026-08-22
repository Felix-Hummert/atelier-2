<script lang="ts">
  import type { AnyRun } from "../api/client";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { runPath } from "../lib/route";
  import { humanMove, standingMarks, standingWords, waitsForAHuman } from "../lib/runState";
  import { studioPageCopy } from "../lib/studioPageCopy";

  export let runs: readonly AnyRun[];
  export let navigate: (path: string) => void;

  $: waiting = runs.filter((run) => waitsForAHuman(run.state));
</script>

{#if waiting.length > 0}
  <section class="inbox" aria-labelledby="inbox-title">
    <h2 id="inbox-title">{wrapDisplayCopy(standingWords.waiting)}</h2>
    <p class="inbox-count">{waiting.length} {wrapDisplayCopy(waiting.length === 1 ? studioPageCopy.needsYou : studioPageCopy.needYou)}</p>
    <ul class="inbox-cards">
      {#each waiting as run (run.public_run_reference)}
        {@const move = humanMove(run.state)}
        <li>
          <a
            class="inbox-card"
            href={runPath(run.public_run_reference)}
            onclick={(event) => { event.preventDefault(); navigate(runPath(run.public_run_reference)); }}
          >
            <span class="inbox-mark" aria-hidden="true">{standingMarks.waiting}</span>
            <span class="inbox-what">
              <strong>{run.run_id}</strong>
              <small>{wrapDisplayCopy(studioPageCopy.needsYou)}</small>
            </span>
            <span class="inbox-go">{move === null ? "" : wrapDisplayCopy(move)}</span>
          </a>
        </li>
      {/each}
    </ul>
  </section>
{/if}
