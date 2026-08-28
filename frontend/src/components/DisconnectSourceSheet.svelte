<script lang="ts">
  import { onMount } from "svelte";

  import { wrapDisplayCopy } from "../lib/displayCopy";
  import type { DisconnectFacts, SourceDoorError } from "../lib/projectSources";
  import { settingsPageCopy } from "../lib/settingsPageCopy";

  export let facts: DisconnectFacts;
  export let submitting: boolean;
  export let error: SourceDoorError | null;
  export let onConfirm: () => void;
  export let onClose: () => void;

  type DialogElement = HTMLElement & { open: boolean; showModal?: () => void; close?: () => void };

  let dialogElement: DialogElement;
  let keepButton: HTMLButtonElement;

  onMount(() => {
    if (typeof dialogElement.showModal === "function") dialogElement.showModal();
    else dialogElement.open = true;
    keepButton.focus();
  });

  function dismiss(): void {
    if (submitting) return;
    dialogElement.close?.();
    onClose();
  }

  function handleCancel(event: Event): void {
    if (submitting) {
      event.preventDefault();
      return;
    }
    dismiss();
  }
</script>

<div class="sheet-positioner">
  <dialog
    bind:this={dialogElement}
    class="sheet"
    aria-labelledby="disconnect-source-title"
    aria-busy={submitting}
    oncancel={handleCancel}
  >
    <header>
      <h2 id="disconnect-source-title">{wrapDisplayCopy(facts.title)}</h2>
    </header>

    <div class="facts">
      <div class="fact">
        <b>{wrapDisplayCopy(facts.goesLabel)}</b>
        <span>{facts.goes}</span>
      </div>
      <div class="fact">
        <b>{wrapDisplayCopy(facts.staysLabel)}</b>
        <span>{facts.stays}</span>
      </div>
      <div class="fact">
        <b>{wrapDisplayCopy(facts.againLabel)}</b>
        <span>{facts.again}</span>
      </div>
    </div>

    {#if submitting}
      <p class="running" role="status">{settingsPageCopy.running}</p>
    {/if}
    {#if error !== null}
      <p class="error" role="alert">
        <span>{error.sentence}</span>
        <button class="next-step" type="button" disabled={submitting} onclick={() => { onConfirm(); }}>
          {wrapDisplayCopy(error.nextStep)}
        </button>
      </p>
    {/if}

    <footer>
      <button class="danger" type="button" disabled={submitting} onclick={() => { onConfirm(); }}>
        {wrapDisplayCopy(settingsPageCopy.disconnect)}
      </button>
      <button bind:this={keepButton} class="quiet" type="button" disabled={submitting} onclick={dismiss}>
        {wrapDisplayCopy(settingsPageCopy.keepIt)}
      </button>
    </footer>
  </dialog>
</div>

<style>
  .sheet-positioner { position: fixed; inset: 0; z-index: 10; pointer-events: none; }
  .sheet { box-sizing: border-box; position: fixed; inset: var(--space-5) var(--space-5) auto auto; width: min(var(--dialog-width), calc(100% - (var(--space-5) * 2))); max-height: calc(100vh - (var(--space-5) * 2)); margin: 0; padding: var(--space-5); background: var(--panel2); border: var(--edge) solid var(--ink); border-radius: var(--r-lg); box-shadow: var(--shadow-lift); pointer-events: auto; }
  .sheet::backdrop { background: color-mix(in srgb, var(--ground) 80%, transparent); }
  header, footer { display: flex; align-items: center; gap: var(--space-3); }
  h2 { margin: 0; font-family: var(--serif); }
  .facts { display: grid; gap: var(--space-3); margin: var(--space-4) 0; }
  .fact { display: grid; gap: var(--space-1); }
  .fact b { color: var(--ink-dim); font-size: var(--text-2xs); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  .running { margin: 0 0 var(--space-3); color: var(--signal-live); }
  .error { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); margin: 0 0 var(--space-3); color: var(--signal-failure); }
  .next-step { min-height: var(--tap); border-color: transparent; background: transparent; color: var(--signal-failure); text-decoration: underline; text-underline-offset: var(--underline-offset); }
  .danger { border-color: var(--signal-failure); color: var(--signal-failure); background: transparent; }
  @media (max-width: 390px) { .sheet { inset: auto 0 0 0; width: 100%; max-height: 85vh; border-radius: var(--r-lg) var(--r-lg) 0 0; } }
</style>
