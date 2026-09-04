<script lang="ts" context="module">
  /** One earlier result the waiting step reads, as the operator needs to read it too. */
  export type WaitContextSource = {
    nodeId: string;
    /** The result text, or null where the store could not give it back. */
    text: string | null;
  };
</script>

<script lang="ts">
  import { decisionStatusCopy } from "../lib/decisionStatusCopy";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import type { WaitMutation } from "../lib/mutationJournal";
  import { runPageCopy } from "../lib/runPageCopy";
  import { confirmedDecisionLabel, decisionLabel } from "../lib/waitDecision";
  import LoadingState from "./LoadingState.svelte";
  import ReadableResult from "./ReadableResult.svelte";

  /**
   * The one card that carries a decision the run is waiting on.
   *
   * It leads with the question, not with the node's type and id: "WAIT gate"
   * tells a person nothing they can act on (operator, 23.08.). Under the
   * question stands the material the question is about — the results of the
   * steps this one reads — because a decision without its problem statement
   * cannot be made. The composer sits last, where the eye already is.
   */
  export let question: string | null;
  export let questionMissing: boolean;
  export let questionFailed = false;
  export let sources: readonly WaitContextSource[] = [];
  export let sourcesLoading = false;
  export let pending: WaitMutation | null;
  export let pendingAnswer: string | null;
  export let accepted = false;
  export let busy = false;
  export let validationMessage: string | null = null;
  export let failureMessage: string | null = null;
  export let onAnswer: (answer: string) => void;
  export let onRetry: () => void;
  export let onDiscard: () => void;
  /**
   * How the waiting node's own schema classifies its answer (#553).
   *
   * `boolean` and `enum` render as decision buttons that send an exact
   * value the click itself decides, never text a person typed; `string` and
   * `free` both render the textarea this card always had -- `string` sends
   * what was typed verbatim (`onAnswer`'s caller reads `answerStringTyped`
   * to decide, #1091 PR #1108 finding 1), `free` still JSON-encodes it, and
   * an unclassified wait (this build has not yet resolved its schema) is
   * `free`, no worse than before.
   */
  export let answerKind: "boolean" | "enum" | "string" | "free" = "free";
  /**
   * Whether the waiting node's schema is `type: string` (every `string`
   * kind, and an `enum` that also names `type: string`) -- the one shape
   * whose door reads an answer's raw text verbatim, so `answerValues` there
   * already carries each member's raw text rather than JSON-encoded text
   * (#1091 PR #1108 finding 1).
   */
  export let answerStringTyped = false;
  /** The enum's own members -- raw text when `answerStringTyped`, JSON-encoded text otherwise. Present only when `answerKind` is `enum`. */
  export let answerValues: readonly string[] = [];

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

  $: confirmedDecision =
    pendingAnswer === null
      ? null
      : confirmedDecisionLabel(
          answerKind,
          answerStringTyped,
          pendingAnswer,
          runPageCopy.answerYes,
          runPageCopy.answerNo
        );
</script>

<section
  class="decision"
  class:decision-sent={pending !== null}
  aria-labelledby="v3-wait-action-title"
>
  <p class="eyebrow">{wrapDisplayCopy(runPageCopy.needsYou)}</p>

  {#if pending !== null}
    <h2 id="v3-wait-action-title" tabindex="-1" bind:this={statusHeading}>{busy ? decisionStatusCopy.sending : accepted ? decisionStatusCopy.pending : decisionStatusCopy.uncertain}</h2>
    {#if failureMessage !== null}
      <div class="wait-alert" role="alert" aria-label={decisionStatusCopy.sendUncertain}>
        <span class="wait-alert-shape" aria-hidden="true">?</span>
        <span><strong>{decisionStatusCopy.sendUncertain}</strong><small>{failureMessage}</small></span>
      </div>
    {/if}
    <output class="exact-answer" aria-label={decisionStatusCopy.exactAnswer}
      >{#if confirmedDecision !== null}{wrapDisplayCopy(runPageCopy.answeredPrefix)} {confirmedDecision}{:else}{pendingAnswer}{/if}</output
    >
    {#if !accepted && !busy}
      <div class="actions">
        <button type="button" disabled={busy} onclick={onRetry} bind:this={retryButton}>{runPageCopy.retry}</button>
        <button class="quiet" type="button" disabled={busy} onclick={onDiscard}>{runPageCopy.discard}</button>
      </div>
    {/if}
  {:else}
    {#if question !== null}
      <h2 id="v3-wait-action-title">{question}</h2>
    {:else if questionMissing}
      <h2 id="v3-wait-action-title">{wrapDisplayCopy(runPageCopy.questionMissing)}</h2>
    {:else if questionFailed}
      <h2 id="v3-wait-action-title">{wrapDisplayCopy(runPageCopy.needsYou)}</h2>
    {:else}
      <h2 id="v3-wait-action-title">
        <LoadingState label={wrapDisplayCopy(runPageCopy.questionLooking)} compact />
      </h2>
    {/if}

    <section class="decision-context" aria-labelledby="v3-wait-context-title">
      <h3 id="v3-wait-context-title">{wrapDisplayCopy(runPageCopy.answerContext)}</h3>
      {#if sourcesLoading}
        <LoadingState label={wrapDisplayCopy(runPageCopy.answerContextLooking)} compact />
      {:else if sources.length === 0}
        <p class="muted">{wrapDisplayCopy(runPageCopy.answerContextNone)}</p>
      {:else}
        {#each sources as source (source.nodeId)}
          <article class="decision-source" aria-label={source.nodeId}>
            <h4>{source.nodeId}</h4>
            {#if source.text === null}
              <p class="muted">{wrapDisplayCopy(runPageCopy.answerContextUnreadable)}</p>
            {:else}
              <ReadableResult decodedAnswer={source.text} />
            {/if}
          </article>
        {/each}
      {/if}
    </section>

    {#snippet sendFailedAlert()}
      {#if failureMessage !== null}
        <div class="wait-alert" role="alert" aria-label={decisionStatusCopy.sendFailed}>
          <span class="wait-alert-shape" aria-hidden="true">!</span>
          <span><strong>{decisionStatusCopy.sendFailed}</strong><small>{failureMessage}</small></span>
        </div>
      {/if}
    {/snippet}

    {#if answerKind === "boolean"}
      <div class="decision-buttons" role="group" aria-label={wrapDisplayCopy(runPageCopy.answerLabel)}>
        <button class="primary" type="button" disabled={busy} onclick={() => onAnswer("true")}>
          {wrapDisplayCopy(runPageCopy.answerYes)}
        </button>
        <button class="primary" type="button" disabled={busy} onclick={() => onAnswer("false")}>
          {wrapDisplayCopy(runPageCopy.answerNo)}
        </button>
      </div>
      {@render sendFailedAlert()}
    {:else if answerKind === "enum"}
      <div class="decision-buttons" role="group" aria-label={wrapDisplayCopy(runPageCopy.answerLabel)}>
        {#each answerValues as value (value)}
          <button class="primary" type="button" disabled={busy} onclick={() => onAnswer(value)}>
            {decisionLabel(value, answerStringTyped)}
          </button>
        {/each}
      </div>
      {@render sendFailedAlert()}
    {:else}
      <form class="wait-form" onsubmit={submit} novalidate>
        <label for="v3-wait-answer">{wrapDisplayCopy(runPageCopy.answerLabel)}</label>
        <textarea
          id="v3-wait-answer"
          rows="4"
          spellcheck="false"
          bind:value={answer}
          aria-describedby={validationMessage === null ? undefined : "v3-wait-validation"}
          aria-invalid={validationMessage === null ? undefined : "true"}
          bind:this={answerInput}
        ></textarea>
        {@render sendFailedAlert()}
        {#if validationMessage !== null}
          <p id="v3-wait-validation" class="field-error" role="alert">{validationMessage}</p>
        {/if}
        <button class="primary" type="submit" disabled={busy}>
          {wrapDisplayCopy(runPageCopy.answerSubmit)}
        </button>
      </form>
    {/if}
  {/if}
</section>

<style>
  .decision {
    display: grid;
    gap: var(--space-3);
    border: var(--edge-strong) solid var(--signal-attention-mark);
    border-radius: var(--r-lg);
    padding: var(--space-5);
    background: var(--panel2);
  }

  .decision-sent {
    border-color: var(--signal-live);
  }

  .eyebrow {
    margin: 0;
    color: var(--signal-attention);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
  }

  .decision-sent .eyebrow {
    color: var(--signal-live);
  }

  h2 {
    margin: 0;
    font-size: var(--text-lg);
    line-height: var(--leading-tight);
    overflow-wrap: anywhere;
  }

  h3 {
    margin: 0 0 var(--space-2);
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
  }

  h4 {
    margin: 0 0 var(--space-1);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  .decision-context {
    display: grid;
    gap: var(--space-2);
  }

  .muted {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-sm);
  }

  .decision-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
  }
</style>
