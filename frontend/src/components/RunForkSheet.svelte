<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";

  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { forkFactList, type ForkPlan } from "../lib/runFork";
  import { runPageCopy } from "../lib/runPageCopy";
  import ProblemNotice from "./ProblemNotice.svelte";

  export let plan: Extract<ForkPlan, { kind: "ok" }>;
  export let originName: string;
  export let busy: boolean;
  export let failureMessage: string | null;
  export let onConfirm: () => void;
  export let onDismiss: () => void;

  const copy = runPageCopy.fork;

  let dialogElement: globalThis.HTMLDialogElement;
  let dismissButton: HTMLButtonElement;
  let confirmButton: HTMLButtonElement;

  $: carried = [forkFactList(plan.carriedNodeIds), forkFactList(plan.orderNames)]
    .filter((part) => part !== "")
    .join(" · ");
  $: rerun = forkFactList(plan.rerunNodeIds);

  onMount(() => {
    void openDialog();
  });

  async function openDialog(): Promise<void> {
    await tick();
    if (dialogElement == null) return;
    dialogElement.showModal();
    dismissButton?.focus();
  }

  function dismiss(): void {
    if (busy) return;
    dialogElement.close?.();
    onDismiss();
  }

  function handleDialogCancel(event: Event): void {
    event.preventDefault();
    if (busy) return;
    dismiss();
  }

  onDestroy(() => {
    dialogElement?.close?.();
  });

  function containDialogFocus(event: KeyboardEvent): void {
    if (event.key !== "Tab") return;
    if (event.shiftKey && globalThis.document.activeElement === dismissButton) {
      event.preventDefault();
      confirmButton.focus();
    } else if (!event.shiftKey && globalThis.document.activeElement === confirmButton) {
      event.preventDefault();
      dismissButton.focus();
    }
  }
</script>

<dialog
  class="dialog"
  aria-label={wrapDisplayCopy(copy.sheetLabel)}
  bind:this={dialogElement}
  oncancel={handleDialogCancel}
  onkeydown={containDialogFocus}
>
  <h2>{wrapDisplayCopy(copy.confirmTitle(plan.restartFrom))}</h2>
  <p>
    <strong>{wrapDisplayCopy(copy.carriedOver)}</strong>
    {carried === "" ? wrapDisplayCopy(runPageCopy.none) : carried}
  </p>
  <p>
    <strong>{wrapDisplayCopy(copy.runsAgain)}</strong>
    {rerun === "" ? wrapDisplayCopy(runPageCopy.none) : rerun}
  </p>
  <p>
    <strong>{wrapDisplayCopy(copy.origin)}</strong>
    {wrapDisplayCopy(originName)}
  </p>
  <p>{wrapDisplayCopy(copy.deferralSentence)}</p>
  {#if failureMessage !== null}
    <ProblemNotice title={wrapDisplayCopy(copy.unconfirmed)} message={failureMessage} />
  {/if}
  <div class="dialog-actions">
    <button
      class="quiet"
      type="button"
      disabled={busy}
      bind:this={dismissButton}
      onclick={dismiss}
    >{wrapDisplayCopy(copy.back)}</button>
    <button
      class="primary"
      type="button"
      disabled={busy}
      bind:this={confirmButton}
      onclick={onConfirm}
    >{wrapDisplayCopy(copy.startAgain)}</button>
  </div>
</dialog>
