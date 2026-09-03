<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";

  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { journalPoisonedCopy } from "../lib/journalPoisonedCopy";

  /**
   * The one confirmation a poisoned mutation journal's door opens (#914,
   * mockup v8 `#v8-21-journal-poisoned`): the same three facts the catalog's
   * own retire card asks with (`RetireCatalogLineageSheet.svelte`,
   * `#v8-16-retire-confirmation`) -- Disappears / Stays / Permanent -- because
   * the mockup names this the identical irreversible-decision shape and it
   * needs no new one. Removing the journal is a synchronous local write, so
   * unlike the retire card this carries no `submitting` or `failure` state.
   */
  export let onConfirm: () => void;
  export let onDismiss: () => void;

  let dialogElement: globalThis.HTMLDialogElement;
  let cancelButton: HTMLButtonElement;

  onMount(() => {
    void openDialog();
  });

  async function openDialog(): Promise<void> {
    await tick();
    dialogElement.showModal();
    cancelButton.focus();
  }

  function dismiss(): void {
    dialogElement.close();
    onDismiss();
  }

  function handleCancel(event: Event): void {
    event.preventDefault();
    dismiss();
  }

  onDestroy(() => {
    dialogElement?.close();
  });
</script>

<dialog
  class="sheet"
  aria-label={wrapDisplayCopy(journalPoisonedCopy.confirmLabel)}
  bind:this={dialogElement}
  oncancel={handleCancel}
>
  <h2>{wrapDisplayCopy(journalPoisonedCopy.confirmQuestion)}</h2>
  <div class="facts">
    <div class="fact">
      <b>{wrapDisplayCopy(journalPoisonedCopy.disappears)}</b>
      <span>{wrapDisplayCopy(journalPoisonedCopy.disappearsFact)}</span>
    </div>
    <div class="fact">
      <b>{wrapDisplayCopy(journalPoisonedCopy.stays)}</b>
      <span>{wrapDisplayCopy(journalPoisonedCopy.staysFact)}</span>
    </div>
    <div class="fact">
      <b>{wrapDisplayCopy(journalPoisonedCopy.permanent)}</b>
      <span>{wrapDisplayCopy(journalPoisonedCopy.permanentFact)}</span>
    </div>
  </div>
  <footer>
    <button class="danger" type="button" onclick={onConfirm}>
      {wrapDisplayCopy(journalPoisonedCopy.confirm)}
    </button>
    <button bind:this={cancelButton} class="quiet" type="button" onclick={dismiss}>
      {wrapDisplayCopy(journalPoisonedCopy.cancel)}
    </button>
  </footer>
</dialog>

<style>
  .sheet { box-sizing: border-box; position: fixed; inset: var(--space-5) var(--space-5) auto auto; width: min(var(--dialog-width), calc(100% - (var(--space-5) * 2))); margin: 0; padding: var(--space-5); background: var(--panel2); border: var(--edge) solid var(--ink); border-radius: var(--r-lg); box-shadow: var(--shadow-lift); }
  .sheet::backdrop { background: color-mix(in srgb, var(--ground) 80%, transparent); }
  h2 { margin: 0; font-family: var(--serif); }
  .facts { display: grid; gap: var(--space-3); margin: var(--space-4) 0; }
  .fact { display: grid; gap: var(--space-1); }
  .fact b { color: var(--ink-dim); font-size: var(--text-2xs); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  footer { display: flex; gap: var(--space-3); }
  .danger { border-color: var(--signal-failure); color: var(--signal-failure); background: transparent; }
  @media (max-width: 390px) { .sheet { inset: auto 0 0 0; width: 100%; border-radius: var(--r-lg) var(--r-lg) 0 0; } }
</style>
