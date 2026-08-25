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

  /**
   * A scrollable exact-bytes box takes a tab stop the same way
   * `RunCockpitPage.svelte`'s own event evidence already does: through an
   * action, not a static `tabindex` attribute, because a static one on a
   * non-interactive element is exactly what the house's a11y lint refuses.
   */
  function keyboardScrollableRegion(region: HTMLElement): void {
    region.tabIndex = 0;
  }
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
  <details>
    <summary class="reveal-affordance">{wrapDisplayCopy(runResultCopy.exactText)}</summary>
    <pre
      class="exact"
      role="region"
      use:keyboardScrollableRegion
      aria-label={wrapDisplayCopy(runResultCopy.exactText)}
    >{result.raw}</pre>
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

  /* The house's one exact-bytes box (Prompt tab, Evidence, this disclosure):
     same padding, same chip background, same scroll-box bound once a value
     runs long. Svelte scopes CSS per component, so this is a value copy of
     `NodeDetailPanel.svelte`'s own `.exact` rather than one shared selector
     -- promoting it into `styles.css` is a named follow-up, not done here
     because that file is under another lane's exact-scope claim while this
     fix lands. */
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

  .exact:focus-visible {
    outline: var(--edge-focus) solid var(--accent);
    outline-offset: var(--edge-focus);
  }
</style>
