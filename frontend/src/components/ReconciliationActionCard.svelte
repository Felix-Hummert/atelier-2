<script lang="ts">
  import { tick } from "svelte";

  import { decodeCanonicalBase64, type Run } from "../api/client";
  import { decisionStatusCopy } from "../lib/decisionStatusCopy";
  import {
    reconciliationCommand,
    type ReconciliationDeterminationInput,
    type ReconciliationMutation
  } from "../lib/mutationJournal";
  import { PRODUCT_NAME } from "../lib/productName";
  import InfoHint from "./InfoHint.svelte";

  type WaitingReconciliation = Extract<Run["waiting"], { type: "WAITING_RECONCILIATION" }>;

  export let waiting: WaitingReconciliation;
  export let pending: ReconciliationMutation | null;
  export let accepted = false;
  export let busy = false;
  export let failureMessage: string | null = null;
  export let onResolve: (
    actor: string,
    evidence: string,
    determination: ReconciliationDeterminationInput
  ) => Promise<void>;
  export let onRetry: () => void;
  export let onDiscard: () => void;

  let actor = "";
  let evidence = "";
  let determination: "found" | "absent" = "found";
  let effectId = "";
  let resultBase64 = "";
  let validationMessage: string | null = null;
  let absenceDialog = false;
  let reviewButton: { focus(): void };
  let dialogElement: globalThis.HTMLDialogElement;
  let cancelButton: HTMLButtonElement;
  let executeButton: HTMLButtonElement;
  let actorInput: { focus(): void };
  let retryButton: { focus(): void };
  let statusHeading: { focus(): void };

  $: localCommand = pending === null ? null : reconciliationCommand(pending);
  $: visibleCommand = waiting.pending_command ?? localCommand;
  $: pendingResult = visibleCommand?.determination.type === "operator_found"
    ? visibleResult(visibleCommand.determination.result_base64)
    : null;

  export function focusInput(): void {
    actorInput?.focus();
  }

  export function focusRetry(): void {
    retryButton?.focus();
  }

  export function focusStatus(): void {
    statusHeading?.focus();
  }

  async function submitFound(event: Event): Promise<void> {
    event.preventDefault();
    validationMessage = validate(true);
    if (validationMessage !== null) return;
    await onResolve(actor, evidence, {
      type: "operator_found",
      effect_id: effectId,
      result_base64: resultBase64
    });
  }

  async function reviewAbsence(): Promise<void> {
    validationMessage = validate(false);
    if (validationMessage !== null) return;
    absenceDialog = true;
    await tick();
    dialogElement.showModal();
    cancelButton.focus();
  }

  async function closeAbsenceDialog(): Promise<void> {
    dialogElement.close();
    absenceDialog = false;
    await tick();
    reviewButton.focus();
  }

  async function executeAbsence(): Promise<void> {
    dialogElement.close();
    absenceDialog = false;
    await onResolve(actor, evidence, { type: "operator_authoritative_absence" });
  }

  function handleDialogCancel(event: Event): void {
    event.preventDefault();
    void closeAbsenceDialog();
  }

  function containDialogFocus(event: KeyboardEvent): void {
    if (event.key !== "Tab") return;
    if (event.shiftKey && globalThis.document.activeElement === cancelButton) {
      event.preventDefault();
      executeButton.focus();
    } else if (!event.shiftKey && globalThis.document.activeElement === executeButton) {
      event.preventDefault();
      cancelButton.focus();
    }
  }

  function validate(found: boolean): string | null {
    if (actor.trim().length === 0) return "Name the accountable actor.";
    if (evidence.trim().length === 0) return "Record the evidence inspected.";
    if (found && effectId.trim().length === 0) return "Name the exact effect ID.";
    if (found && decodeCanonicalBase64(resultBase64) === null) {
      return "Use canonical standard base64 for the exact result.";
    }
    return null;
  }

  function visibleResult(encoded: string): string {
    const bytes = decodeCanonicalBase64(encoded);
    if (bytes === null) return "Invalid result";
    if (bytes.byteLength === 0) return "Empty result";
    try {
      return new globalThis.TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      return encoded;
    }
  }
</script>

<section
  class="human-action reconciliation-action"
  class:human-action-working={visibleCommand !== null || busy}
  aria-labelledby="reconciliation-action-title"
>
  <div class="human-action-header">
    <p class="eyebrow">Reconciliation</p>
    <span
      class="human-action-shape"
      class:human-action-shape-working={visibleCommand !== null || busy}
      class:human-action-shape-needs={visibleCommand === null && !busy}
      aria-hidden="true"
    >{visibleCommand === null && !busy ? "!" : "▲"}</span>
  </div>

  {#if visibleCommand !== null || busy}
    <h2 id="reconciliation-action-title" tabindex="-1" bind:this={statusHeading}>
      {busy ? "Sending decision" : accepted || waiting.pending_command !== null ? "Decision pending" : "Decision uncertain"}
    </h2>
    {#if failureMessage !== null}
      <div class="wait-alert" role="alert" aria-label={decisionStatusCopy.sendUncertain}>
        <span class="wait-alert-shape" aria-hidden="true">?</span>
        <span><strong>{decisionStatusCopy.sendUncertain}</strong><small>{failureMessage}</small></span>
      </div>
    {/if}
    {#if visibleCommand !== null}
      <dl class="decision-summary">
        <div><dt>Actor</dt><dd>{visibleCommand.actor}</dd></div>
        <div><dt>Evidence</dt><dd>{visibleCommand.evidence}</dd></div>
        <div><dt>Command</dt><dd><code>{visibleCommand.command_id}</code></dd></div>
        {#if visibleCommand.determination.type === "operator_found"}
          <div><dt>Effect ID</dt><dd>{visibleCommand.determination.effect_id}</dd></div>
          <div><dt>Result</dt><dd class:empty-value={pendingResult === "Empty result"}>{pendingResult}</dd></div>
        {:else}
          <div><dt>Decision</dt><dd>Authoritative absence</dd></div>
        {/if}
      </dl>
    {/if}
    {#if visibleCommand !== null && pending !== null && !accepted && waiting.pending_command === null && !busy}
      <div class="actions">
        <button type="button" onclick={onRetry} bind:this={retryButton}>Retry</button>
        <button class="quiet" type="button" onclick={onDiscard}>Discard</button>
      </div>
    {/if}
  {:else}
    <h2 id="reconciliation-action-title">Decision needed</h2>
    <dl class="request-summary">
      <div><dt>Effect</dt><dd>{waiting.logical_effect_key}</dd></div>
      <div><dt>Hash</dt><dd><code>{waiting.request_hash}</code></dd></div>
      <div><dt>Version</dt><dd>{waiting.intent_state_version}</dd></div>
      <div>
        <dt>Request</dt>
        <dd>
          <span>{decodeCanonicalBase64(waiting.request_base64)?.byteLength ?? 0} bytes</span>
          <InfoHint label="Request info" exact={waiting.request_base64} />
        </dd>
      </div>
    </dl>
    <form class="reconciliation-form" onsubmit={submitFound} novalidate>
      <label for="reconcile-actor">Actor</label>
      <input id="reconcile-actor" type="text" autocomplete="name" bind:value={actor} bind:this={actorInput} />
      <label for="reconcile-evidence">Evidence</label>
      <textarea id="reconcile-evidence" rows="3" bind:value={evidence}></textarea>
      <fieldset class="determination-picker">
        <legend>Decision</legend>
        <label><input type="radio" name="reconciliation-determination" bind:group={determination} value="found" /> Found</label>
        <label><input type="radio" name="reconciliation-determination" bind:group={determination} value="absent" /> Absent</label>
      </fieldset>
      {#if determination === "found"}
        <label for="reconcile-effect">Effect ID</label>
        <input id="reconcile-effect" type="text" autocomplete="off" bind:value={effectId} />
        <label for="reconcile-result">Exact result (base64)</label>
        <textarea id="reconcile-result" rows="3" spellcheck="false" bind:value={resultBase64}></textarea>
      {/if}
      {#if validationMessage !== null}
        <p class="field-error" role="alert">{validationMessage}</p>
      {/if}
      {#if determination === "found"}
        <button class="primary" type="submit" disabled={busy}>Resolve</button>
      {:else}
        <button
          class="primary"
          type="button"
          disabled={busy}
          bind:this={reviewButton}
          onclick={() => { void reviewAbsence(); }}
        >Review</button>
      {/if}
    </form>
  {/if}
</section>

{#if absenceDialog}
  <dialog
    class="dialog"
    aria-labelledby="absence-title"
    bind:this={dialogElement}
    oncancel={handleDialogCancel}
    onkeydown={containDialogFocus}
  >
    <h2 id="absence-title">Execute this exact effect?</h2>
    <p>{PRODUCT_NAME} will execute the exact request once.</p>
    <div class="dialog-actions">
      <button
        class="quiet"
        type="button"
        bind:this={cancelButton}
        onclick={() => { void closeAbsenceDialog(); }}
      >Cancel</button>
      <button
        class="primary"
        type="button"
        bind:this={executeButton}
        onclick={() => { void executeAbsence(); }}
      >Execute</button>
    </div>
  </dialog>
{/if}
