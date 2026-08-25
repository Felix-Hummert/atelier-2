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
{:else if result.kind === "object"}
  {#if result.sentence !== null}
    <p class="readable-result-text">{result.sentence}</p>
  {/if}
  {#if result.fields.length > 0}
    <dl class="readable-result-fields">
      {#each result.fields as field (field.label)}
        <div>
          <dt>{field.label}</dt>
          <dd>{field.value}</dd>
        </div>
      {/each}
    </dl>
  {/if}
{:else}
  <ul class="readable-result-items">
    {#each result.items as item, index (index)}
      <li>{item}</li>
    {/each}
  </ul>
{/if}
{#if result.raw !== null}
  <details class="readable-result-raw">
    <summary class="reveal-affordance">{wrapDisplayCopy(runResultCopy.exactText)}</summary>
    <pre class="exact">{result.raw}</pre>
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

  .readable-result-items {
    margin: 0;
    padding-left: var(--space-5);
  }

  .readable-result-items li {
    overflow-wrap: anywhere;
  }

  /* The one exact-bytes box every reveal in the house shares (Prompt tab,
     Evidence, this disclosure): same padding, same chip background, same
     scroll-box bound once a value runs long. */
  .exact {
    margin: var(--space-2) 0 0;
    max-height: var(--scroll-box);
    overflow: auto;
    padding: var(--space-3);
    border-radius: var(--r);
    background: var(--chip);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: var(--text-sm);
  }
</style>
