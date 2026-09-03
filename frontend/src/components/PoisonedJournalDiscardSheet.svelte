<script lang="ts">
  import ThreeFactConfirmSheet from "./ThreeFactConfirmSheet.svelte";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { journalPoisonedCopy } from "../lib/journalPoisonedCopy";

  /**
   * The one confirmation a poisoned mutation journal's door opens (#914,
   * mockup v8 `#v8-21-journal-poisoned`): the shared three-fact confirm shape
   * (`ThreeFactConfirmSheet.svelte`) also used by the catalog's own retire
   * card, plus one addition the mockup does not draw -- the exact raw text
   * about to be forgotten, copyable behind a Technical reveal, because it is
   * the only honest receipt a poisoned `localStorage` key can still give
   * (issue #914 line 12). `raw` is read once by the caller, before this
   * sheet ever opens, so the same bytes shown here are the ones the caller's
   * post-discard receipt measures.
   */
  export let raw: string;
  export let submitting = false;
  export let failure: string | null = null;
  export let onConfirm: () => void;
  export let onDismiss: () => void;

  /**
   * A scrollable exact-bytes box takes a tab stop the same way
   * `ReadableResult.svelte`'s own exact-bytes disclosure already does.
   */
  function keyboardScrollableRegion(region: HTMLElement): void {
    region.tabIndex = 0;
  }
</script>

<ThreeFactConfirmSheet
  ariaLabel={journalPoisonedCopy.confirmLabel}
  heading={journalPoisonedCopy.confirmQuestion}
  facts={[
    { label: journalPoisonedCopy.disappears, text: journalPoisonedCopy.disappearsFact },
    { label: journalPoisonedCopy.stays, text: journalPoisonedCopy.staysFact },
    { label: journalPoisonedCopy.permanent, text: journalPoisonedCopy.permanentFact }
  ]}
  confirmLabel={journalPoisonedCopy.confirm}
  cancelLabel={journalPoisonedCopy.cancel}
  {submitting}
  {failure}
  {onConfirm}
  {onDismiss}
>
  <details class="technical">
    <summary>{wrapDisplayCopy(journalPoisonedCopy.technical)}</summary>
    <pre
      class="exact"
      role="region"
      use:keyboardScrollableRegion
      aria-label={wrapDisplayCopy(journalPoisonedCopy.rawContentLabel)}
    >{raw}</pre>
  </details>
</ThreeFactConfirmSheet>

<style>
  .technical {
    margin: 0 0 var(--space-4);
    font-size: var(--text-xs);
  }

  .technical summary {
    display: flex;
    align-items: center;
    min-height: var(--tap);
    cursor: pointer;
  }

  .exact {
    margin: var(--space-2) 0 0;
    max-height: var(--scroll-box);
    overflow: auto;
    padding: var(--space-3);
    border-radius: var(--r);
    background: var(--chip);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-family: var(--mono);
    font-size: var(--text-sm);
  }

  .exact:focus-visible {
    outline: var(--edge-focus) solid var(--accent);
    outline-offset: var(--edge-focus);
  }
</style>
