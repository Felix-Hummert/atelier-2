<script lang="ts">
  import { decisionStatusCopy } from "../lib/decisionStatusCopy";
  import type { WaitMutation } from "../lib/mutationJournal";

  export let pending: WaitMutation | null;
  export let pendingAnswer: string | null;
  export let accepted = false;
  export let busy = false;
  export let validationMessage: string | null = null;
  export let failureMessage: string | null = null;
  export let onAnswer: (answer: string) => void;
  export let onRetry: () => void;
  export let onDiscard: () => void;

  let answer = "";
  let answerInput: { focus(): void };
  let retryButton: { focus(): void };
  let statusHeading: { focus(): void };

  export function focusInput(): void {
    answerInput?.focus();
  }

  export function focusRetry(): void {
    retryButton?.focus();
  }

  export function focusStatus(): void {
    statusHeading?.focus();
  }

  function submit(event: Event): void {
    event.preventDefault();
    onAnswer(answer);
  }
</script>

<section
  class="human-action"
  class:human-action-working={pending !== null}
  aria-labelledby="wait-action-title"
>
  <div class="human-action-header">
    <p class="eyebrow">Wait node</p>
    <span
      class="human-action-shape"
      class:human-action-shape-working={pending !== null}
      class:human-action-shape-needs={pending === null}
      aria-hidden="true"
    >{pending === null ? "!" : "▲"}</span>
  </div>
  {#if pending !== null}
    <h2 id="wait-action-title" tabindex="-1" bind:this={statusHeading}>{busy ? decisionStatusCopy.sending : accepted ? decisionStatusCopy.pending : decisionStatusCopy.uncertain}</h2>
    {#if failureMessage !== null}
      <div class="wait-alert" role="alert" aria-label={decisionStatusCopy.sendUncertain}>
        <span class="wait-alert-shape" aria-hidden="true">?</span>
        <span><strong>{decisionStatusCopy.sendUncertain}</strong><small>{failureMessage}</small></span>
      </div>
    {/if}
    <output class="exact-answer" aria-label={decisionStatusCopy.exactAnswer}>{pendingAnswer}</output>
    {#if !accepted && !busy}
      <div class="actions">
        <button type="button" disabled={busy} onclick={onRetry} bind:this={retryButton}>Retry</button>
        <button class="quiet" type="button" disabled={busy} onclick={onDiscard}>Discard</button>
      </div>
    {/if}
  {:else}
    <h2 id="wait-action-title">Answer needed</h2>
    <form class="wait-form" onsubmit={submit} novalidate>
      <label for="wait-answer">Integer answer</label>
      <input
        id="wait-answer"
        type="text"
        inputmode="numeric"
        autocomplete="off"
        spellcheck="false"
        bind:value={answer}
        aria-describedby={validationMessage === null ? undefined : "wait-validation"}
        aria-invalid={validationMessage === null ? undefined : "true"}
        bind:this={answerInput}
      />
      {#if failureMessage !== null}
        <div class="wait-alert" role="alert" aria-label={decisionStatusCopy.sendFailed}>
          <span class="wait-alert-shape" aria-hidden="true">!</span>
          <span><strong>{decisionStatusCopy.sendFailed}</strong><small>{failureMessage}</small></span>
        </div>
      {/if}
      {#if validationMessage !== null}
        <p id="wait-validation" class="field-error" role="alert">{validationMessage}</p>
      {/if}
      <button class="primary" type="submit" disabled={busy}>Answer</button>
    </form>
  {/if}
</section>
