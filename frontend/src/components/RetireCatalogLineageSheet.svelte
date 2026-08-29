<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";

  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { catalogPageCopy, workflowDetailCopy } from "../lib/catalogPageCopy";

  export let name: string;
  export let submitting: boolean;
  export let failure: string | null;
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
  aria-label={wrapDisplayCopy(workflowDetailCopy.retireTitle(name))}
  aria-busy={submitting}
  bind:this={dialogElement}
  oncancel={handleCancel}
>
  <h2>{wrapDisplayCopy(workflowDetailCopy.retireTitle(name))}</h2>
  <div class="facts">
    <div class="fact">
      <b>{wrapDisplayCopy(workflowDetailCopy.retireDisappears)}</b>
      <span>{wrapDisplayCopy(workflowDetailCopy.retireDisappearsFact)}</span>
    </div>
    <div class="fact">
      <b>{wrapDisplayCopy(workflowDetailCopy.retireStays)}</b>
      <span>{wrapDisplayCopy(workflowDetailCopy.retireStaysFact)}</span>
    </div>
    <div class="fact">
      <b>{wrapDisplayCopy(workflowDetailCopy.retirePermanent)}</b>
      <span>{wrapDisplayCopy(workflowDetailCopy.retirePermanentFact)}</span>
    </div>
  </div>
  {#if failure !== null}
    <p class="failure" role="alert">{wrapDisplayCopy(failure)}</p>
  {/if}
  <footer>
    <button class="danger" type="button" disabled={submitting} onclick={onConfirm}>
      {wrapDisplayCopy(workflowDetailCopy.retire)}
    </button>
    <button bind:this={cancelButton} class="quiet" type="button" disabled={submitting} onclick={dismiss}>
      {wrapDisplayCopy(catalogPageCopy.cancel)}
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
