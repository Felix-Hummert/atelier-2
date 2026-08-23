<script lang="ts" context="module">
  /** One earlier result the waiting step reads, as the operator needs to read it too. */
  export type WaitContextSource = {
    nodeId: string;
    /** The result text, or null where the store could not give it back. */
    text: string | null;
  };
</script>

<script lang="ts">
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import type { WaitMutation } from "../lib/mutationJournal";
  import { runPageCopy } from "../lib/runPageCopy";

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
   * `boolean` and `enum` render as decision buttons that send an exact JSON
   * value the click itself decides, never text a person typed; `free` is
   * every other schema shape, including one this build has not yet resolved
   * -- the composer falls back to the textarea it always had, so an
   * unclassified wait is no worse than before.
   */
  export let answerKind: "boolean" | "enum" | "free" = "free";
  /** The enum's own members, each already the exact JSON text a click sends. Present only when `answerKind` is `enum`. */
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

  /** A schema-authored JSON value, read back the way a person means it. */
  function decisionLabel(jsonEncoded: string): string {
    const parsed = JSON.parse(jsonEncoded) as unknown;
    return typeof parsed === "string" ? parsed : JSON.stringify(parsed);
  }

  $: confirmedDecision =
    pendingAnswer === null
      ? null
      : answerKind === "boolean"
        ? pendingAnswer === "true"
          ? runPageCopy.answerYes
          : runPageCopy.answerNo
        : answerKind === "enum"
          ? decisionLabel(pendingAnswer)
          : null;
</script>

<section
  class="decision"
  class:decision-sent={pending !== null}
  aria-labelledby="v3-wait-action-title"
>
  <p class="eyebrow">{wrapDisplayCopy(runPageCopy.needsYou)}</p>

  {#if pending !== null}
    <h2 id="v3-wait-action-title" tabindex="-1" bind:this={statusHeading}>{busy ? "Sending answer" : accepted ? "Answer pending" : "Answer uncertain"}</h2>
    {#if failureMessage !== null}
      <div class="wait-alert" role="alert" aria-label="Send uncertain">
        <span class="wait-alert-shape" aria-hidden="true">?</span>
        <span><strong>Send uncertain</strong><small>{failureMessage}</small></span>
      </div>
    {/if}
    <output class="exact-answer" aria-label="Exact answer"
      >{#if confirmedDecision !== null}{wrapDisplayCopy(runPageCopy.answeredPrefix)} {confirmedDecision}{:else}{pendingAnswer}{/if}</output
    >
    {#if !accepted && !busy}
      <div class="actions">
        <button type="button" disabled={busy} onclick={onRetry} bind:this={retryButton}>Retry</button>
        <button class="quiet" type="button" disabled={busy} onclick={onDiscard}>Discard</button>
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
      <h2 id="v3-wait-action-title" class="looking">{wrapDisplayCopy(runPageCopy.questionLooking)}</h2>
    {/if}

    <section class="decision-context" aria-labelledby="v3-wait-context-title">
      <h3 id="v3-wait-context-title">{wrapDisplayCopy(runPageCopy.answerContext)}</h3>
      {#if sourcesLoading}
        <p class="muted" role="status">{wrapDisplayCopy(runPageCopy.answerContextLooking)}</p>
      {:else if sources.length === 0}
        <p class="muted">{wrapDisplayCopy(runPageCopy.answerContextNone)}</p>
      {:else}
        {#each sources as source (source.nodeId)}
          <article class="decision-source" aria-label={source.nodeId}>
            <h4>{source.nodeId}</h4>
            {#if source.text === null}
              <p class="muted">{wrapDisplayCopy(runPageCopy.answerContextUnreadable)}</p>
            {:else}
              <pre>{source.text}</pre>
            {/if}
          </article>
        {/each}
      {/if}
    </section>

    {#snippet sendFailedAlert()}
      {#if failureMessage !== null}
        <div class="wait-alert" role="alert" aria-label="Send failed">
          <span class="wait-alert-shape" aria-hidden="true">!</span>
          <span><strong>Send failed</strong><small>{failureMessage}</small></span>
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
            {decisionLabel(value)}
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
    border: 2px solid var(--danger);
    border-radius: var(--r-lg);
    padding: var(--space-5);
    background: var(--panel2);
  }

  .decision-sent {
    border-color: var(--working);
  }

  .eyebrow {
    margin: 0;
    color: var(--danger);
    font-size: var(--text-2xs);
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .decision-sent .eyebrow {
    color: var(--working);
  }

  h2 {
    margin: 0;
    font-size: var(--text-lg);
    line-height: 1.25;
    overflow-wrap: anywhere;
  }

  h2.looking {
    color: var(--muted);
  }

  h3 {
    margin: 0 0 var(--space-2);
    color: var(--muted);
    font-size: var(--text-2xs);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  h4 {
    margin: 0 0 var(--space-1);
    font-size: var(--text-xs);
    font-weight: 700;
  }

  .decision-context {
    display: grid;
    gap: var(--space-2);
  }

  .decision-source pre {
    margin: 0;
    max-height: 14rem;
    overflow: auto;
    padding: var(--space-3);
    border-radius: var(--r);
    background: var(--chip);
    font-size: var(--text-xs);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .muted {
    margin: 0;
    color: var(--muted);
    font-size: var(--text-sm);
  }

  .decision-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
  }
</style>
