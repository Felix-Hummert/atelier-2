<script lang="ts">
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { runResultCopy } from "../lib/runResultCopy";
  import { readableResult } from "../lib/runProjection";

  /**
   * One node's declared answer, still the exact bytes it wrote (#716): the
   * decode already happened at the caller's one owner for that decision, so
   * this component only classifies and lays out what it was handed.
   */
  export let decodedAnswer: string;

  $: result = readableResult(decodedAnswer);
</script>

{#if result.kind === "text"}
  <p class="readable-result-text">{result.text}</p>
{:else}
  <dl class="readable-result-fields">
    {#each result.fields as field (field.label)}
      <div>
        <dt>{field.label}</dt>
        <dd>{field.value}</dd>
      </div>
    {/each}
  </dl>
{/if}
{#if result.raw !== null}
  <details class="readable-result-raw">
    <summary class="reveal-affordance">{wrapDisplayCopy(runResultCopy.raw)}</summary>
    <pre>{result.raw}</pre>
  </details>
{/if}

<style>
  .readable-result-text {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .readable-result-fields {
    display: grid;
    gap: var(--space-2);
    margin: 0;
  }

  .readable-result-fields dt {
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  .readable-result-fields dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .readable-result-raw pre {
    margin: var(--space-2) 0 0;
    padding: var(--space-3);
    border-radius: var(--r);
    background: var(--chip);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: var(--text-sm);
  }
</style>
