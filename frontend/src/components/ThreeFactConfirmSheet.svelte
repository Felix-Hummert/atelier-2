<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";

  import { wrapDisplayCopy } from "../lib/displayCopy";

  /**
   * The one irreversible-decision shape this house asks with (#914; mockup
   * v8 `#v8-16-retire-confirmation`) -- Disappears / Stays / Permanent --
   * parameterized by copy so a catalog lineage retirement
   * (`RetireCatalogLineageSheet.svelte`) and a poisoned mutation journal
   * discard (`PoisonedJournalDiscardSheet.svelte`) render the identical
   * dialog instead of two copies of the same markup and styles. A caller
   * that needs to show something beyond the three facts -- the poisoned
   * journal's raw-content reveal -- puts it in the default slot, between the
   * facts and the failure line.
   */
  export let ariaLabel: string;
  export let heading: string;
  export let facts: ReadonlyArray<{ label: string; text: string }>;
  export let confirmLabel: string;
  export let cancelLabel: string;
  export let submitting = false;
  export let failure: string | null = null;
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
    if (submitting) return;
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
  aria-label={wrapDisplayCopy(ariaLabel)}
  aria-busy={submitting}
  bind:this={dialogElement}
  oncancel={handleCancel}
>
  <h2>{wrapDisplayCopy(heading)}</h2>
  <div class="facts">
    {#each facts as fact (fact.label)}
      <div class="fact">
        <b>{wrapDisplayCopy(fact.label)}</b>
        <span>{wrapDisplayCopy(fact.text)}</span>
      </div>
    {/each}
  </div>
  <slot />
  {#if failure !== null}
    <p class="failure" role="alert">{wrapDisplayCopy(failure)}</p>
  {/if}
  <footer>
    <button class="danger" type="button" disabled={submitting} onclick={onConfirm}>
      {wrapDisplayCopy(confirmLabel)}
    </button>
    <button bind:this={cancelButton} class="quiet" type="button" disabled={submitting} onclick={dismiss}>
      {wrapDisplayCopy(cancelLabel)}
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
  .failure { color: var(--signal-failure); }
  footer { display: flex; gap: var(--space-3); }
  .danger { border-color: var(--signal-failure); color: var(--signal-failure); background: transparent; }
  @media (max-width: 390px) { .sheet { inset: auto 0 0 0; width: 100%; border-radius: var(--r-lg) var(--r-lg) 0 0; } }
</style>
