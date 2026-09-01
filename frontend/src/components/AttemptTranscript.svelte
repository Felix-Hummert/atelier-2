<script lang="ts">
  import type { AttemptTranscript as StoredTranscript } from "../api/client";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import {
    runPageCopy,
    transcriptDroppedCopy,
    usageLine
  } from "../lib/runPageCopy";
  import type { NodeState } from "../lib/runProjection";
  import { whenFacts } from "../lib/runProjection";
  import { parseUtc } from "../lib/when";
  import { stateLabels } from "../lib/stateMarkCopy";
  import LoadingState from "./LoadingState.svelte";

  /**
   * The wire already substituted this marker for credential-shaped text.
   * The UI splits on it; it does not scan for secrets.
   */
  const REDACTION_MARKER = "[redacted]";

  export let loading = false;
  export let transcript: StoredTranscript | null = null;
  export let nodeState: NodeState | null = null;
  export let startedAt: string | null = null;
  export let endedAt: string | null = null;

  $: closeClock = endedAt === null ? null : localClock(endedAt);
  $: durationWords =
    startedAt === null || endedAt === null
      ? null
      : whenFacts(startedAt, endedAt, new Date()).durationWords;
  $: failed = nodeState === "failed";

  type RedactionPart =
    | { kind: "text"; text: string }
    | { kind: "badge"; wrapped: boolean };

  function localClock(iso: string): string {
    const date = parseUtc(iso);
    const pad = (value: number) => String(value).padStart(2, "0");
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function redactionParts(text: string, redacted: boolean): RedactionPart[] {
    if (!redacted) {
      return [{ kind: "text", text }];
    }
    if (!text.includes(REDACTION_MARKER)) {
      const parts: RedactionPart[] = [];
      if (text.length > 0) {
        parts.push({ kind: "text", text });
      }
      parts.push({ kind: "badge", wrapped: false });
      return parts;
    }
    const chunks = text.split(REDACTION_MARKER);
    const parts: RedactionPart[] = [];
    for (let index = 0; index < chunks.length; index += 1) {
      const chunk = chunks[index] ?? "";
      if (chunk.length > 0) {
        parts.push({ kind: "text", text: chunk });
      }
      if (index < chunks.length - 1) {
        parts.push({ kind: "badge", wrapped: true });
      }
    }
    return parts;
  }

  /**
   * A scrollable exact-bytes box takes a tab stop the same way
   * `NodeDetailPanel.svelte`'s own prompt box already does: through an
   * action, not a static `tabindex` attribute.
   */
  function keyboardScrollableRegion(region: HTMLElement): void {
    region.tabIndex = 0;
  }
</script>

{#if loading}
  <LoadingState label={wrapDisplayCopy(runPageCopy.questionLooking)} />
{:else if transcript === null}
  <div class="transcript-state" role="status">
    <span class="empty-mark" aria-hidden="true"></span>
    <p>{wrapDisplayCopy(runPageCopy.transcriptEmpty)}</p>
  </div>
{:else}
  <div
    class="transcript"
    role="region"
    aria-label={wrapDisplayCopy(runPageCopy.transcriptRegion)}
  >
    {#each transcript.events as event, index (index)}
      {#if event.event === "assistant-turn"}
        <article class="transcript-entry turn">
          <div class="entry-head"><span>{wrapDisplayCopy(runPageCopy.assistantTurn)}</span></div>
          <p>
            {#each redactionParts(event.text, event.redacted) as part, partIndex (`turn-${index}-${partIndex}`)}
              {#if part.kind === "text"}{part.text}{:else}{#if part.wrapped}[{/if}<span class="redaction">{wrapDisplayCopy(runPageCopy.redacted)}</span>{#if part.wrapped}]{/if}{/if}
            {/each}
          </p>
        </article>
      {:else if event.event === "tool-called"}
        <details class="transcript-entry call">
          <summary>
            <span class="entry-kind">{wrapDisplayCopy(runPageCopy.doorCall)}</span>
            <b>
              {#each redactionParts(event.name, event.redacted) as part, partIndex (`call-name-${index}-${partIndex}`)}
                {#if part.kind === "text"}{part.text}{:else}{#if part.wrapped}[{/if}<span class="redaction">{wrapDisplayCopy(runPageCopy.redacted)}</span>{#if part.wrapped}]{/if}{/if}
              {/each}
            </b>
            <span class="fold">{wrapDisplayCopy(runPageCopy.argumentsFold)}</span>
          </summary>
          <div class="arguments">
            {#each redactionParts(event.arguments, event.redacted) as part, partIndex (`call-args-${index}-${partIndex}`)}
              {#if part.kind === "text"}{part.text}{:else}{#if part.wrapped}[{/if}<span class="redaction">{wrapDisplayCopy(runPageCopy.redacted)}</span>{#if part.wrapped}]{/if}{/if}
            {/each}
          </div>
        </details>
      {:else if event.event === "tool-returned"}
        <article class="transcript-entry answer">
          <div class="entry-head"><span>{wrapDisplayCopy(runPageCopy.doorAnswer)}</span></div>
          <p>
            {#each redactionParts(event.result, event.redacted) as part, partIndex (`answer-${index}-${partIndex}`)}
              {#if part.kind === "text"}{part.text}{:else}{#if part.wrapped}[{/if}<span class="redaction">{wrapDisplayCopy(runPageCopy.redacted)}</span>{#if part.wrapped}]{/if}{/if}
            {/each}
          </p>
        </article>
      {:else if event.event === "usage"}
        <article class="transcript-entry usage">
          <div class="entry-head">
            <span>{wrapDisplayCopy(runPageCopy.usage)}</span>
            {#if closeClock !== null && endedAt !== null}
              <time datetime={endedAt}>{closeClock}</time>
            {/if}
          </div>
          <p>{wrapDisplayCopy(usageLine(event.input_tokens, event.output_tokens, durationWords))}</p>
        </article>
      {:else if event.event === "unrecognised-provider-output"}
        <article class="transcript-entry stdout">
          <div class="entry-head">
            <span>{wrapDisplayCopy(runPageCopy.attemptStdout)}</span>
            {#if failed}
              <span class="failed-word">{wrapDisplayCopy(stateLabels.failed)}</span>
            {/if}
            {#if closeClock !== null && endedAt !== null}
              <time datetime={endedAt}>{closeClock}</time>
            {/if}
          </div>
          <pre
            class="stdout-log"
            role="region"
            use:keyboardScrollableRegion
            aria-label={wrapDisplayCopy(runPageCopy.attemptStdout)}
          >{#each redactionParts(event.text, event.redacted) as part, partIndex (`stdout-${index}-${partIndex}`)}{#if part.kind === "text"}{part.text}{:else}{#if part.wrapped}[{/if}<span class="redaction">{wrapDisplayCopy(runPageCopy.redacted)}</span>{#if part.wrapped}]{/if}{/if}{/each}</pre>
        </article>
      {:else if event.event === "transcript-truncated"}
        <p class="truncated">{wrapDisplayCopy(transcriptDroppedCopy(event.dropped_events))}</p>
      {/if}
    {/each}
  </div>
{/if}

<style>
  .transcript {
    display: grid;
    gap: var(--space-2);
  }

  .transcript-entry {
    background: var(--ground);
    border-left: var(--edge-mark) solid var(--line);
    border-radius: 0 var(--r) var(--r) 0;
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-sm);
  }

  .transcript-entry.turn,
  .transcript-entry.call {
    border-left-color: var(--ink-dim);
  }

  .transcript-entry.answer,
  .transcript-entry.usage {
    border-left-color: var(--ink-faint);
  }

  .transcript-entry.usage {
    font-variant-numeric: tabular-nums;
  }

  .transcript-entry.stdout {
    border-left-color: var(--signal-failure);
  }

  .entry-head,
  .transcript-entry summary {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    flex-wrap: wrap;
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
  }

  .entry-head time {
    margin-left: auto;
    font-weight: var(--weight-medium);
    letter-spacing: 0;
    text-transform: none;
    font-variant-numeric: tabular-nums;
  }

  .transcript-entry p {
    margin: var(--space-1) 0 0;
    overflow-wrap: anywhere;
  }

  .transcript-entry.turn p {
    font-family: var(--serif);
    font-size: var(--text-md);
    text-transform: none;
    letter-spacing: 0;
    font-weight: var(--weight-medium);
    color: var(--ink);
  }

  .transcript-entry.answer p,
  .transcript-entry.usage p {
    text-transform: none;
    letter-spacing: 0;
    font-weight: var(--weight-medium);
    color: var(--ink);
  }

  .transcript-entry summary {
    cursor: pointer;
    list-style: none;
  }

  .transcript-entry summary::marker,
  .transcript-entry summary::-webkit-details-marker {
    display: none;
    content: none;
  }

  .transcript-entry summary::after {
    content: "▸";
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
    letter-spacing: 0;
    text-transform: none;
  }

  .transcript-entry[open] summary::after {
    transform: rotate(90deg);
  }

  .entry-kind {
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
  }

  .transcript-entry summary b {
    color: var(--ink);
    text-transform: none;
    letter-spacing: 0;
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
  }

  .fold {
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
    letter-spacing: 0;
    text-transform: none;
  }

  .arguments {
    margin: var(--space-2) 0 0;
    padding: var(--space-2) var(--space-3);
    background: var(--panel2);
    border: var(--edge) solid var(--line);
    border-radius: var(--r);
    font-family: var(--mono);
    font-size: var(--text-xs);
    overflow-wrap: anywhere;
    color: var(--ink);
    text-transform: none;
    letter-spacing: 0;
    font-weight: var(--weight-medium);
  }

  .redaction {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    border: var(--edge) dashed var(--ink-dim);
    border-radius: var(--r-pill);
    padding: 0 var(--space-2);
    background: var(--panel);
    color: var(--ink-dim);
    font-family: var(--sans);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
    letter-spacing: 0;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .redaction::before {
    content: "▰";
    font-size: var(--text-2xs);
    line-height: 1;
  }

  .failed-word {
    color: var(--signal-failure);
    text-transform: none;
    letter-spacing: 0;
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  .stdout-log {
    margin: var(--space-2) 0 0;
    max-height: var(--scroll-box);
    overflow: auto;
    padding: var(--space-3);
    border: var(--edge) solid var(--signal-failure);
    border-radius: var(--r);
    background: var(--ink);
    color: var(--ground);
    font-family: var(--mono);
    font-size: var(--text-xs);
    line-height: var(--leading-body);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  @media (prefers-color-scheme: dark) {
    .stdout-log {
      background: var(--ground);
      color: var(--ink);
    }
  }

  .stdout-log:focus-visible {
    outline: var(--edge-focus) solid var(--accent);
    outline-offset: var(--edge-focus);
  }

  .truncated {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
    overflow-wrap: anywhere;
  }

  .transcript-state {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    background: var(--panel2);
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-3) var(--space-4);
    font-size: var(--text-sm);
    color: var(--ink-dim);
  }

  .transcript-state p {
    margin: 0;
  }

  .empty-mark {
    width: var(--mark);
    height: var(--mark);
    border: var(--edge-strong) solid var(--ink-faint);
    border-radius: 50%;
    flex: none;
  }
</style>
