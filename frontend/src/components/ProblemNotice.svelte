<script lang="ts">
  import type { Problem } from "../api/client";
  import { humanProblemDetail } from "../lib/humanRefusal";
  import { problemNoticeCopy } from "../lib/problemNoticeCopy";

  /**
   * A problem in two layers (operator ruling 23.08.).
   *
   * The visible layer speaks to the person reading it: what happened and what
   * it means for them. The layer behind the reveal speaks to whoever has to
   * repair it — the code the server named and the status it answered with. A
   * `urn:atelier2:problem:` token never stands on a main surface: it says
   * nothing to someone who has not read this repository, and standing there it
   * crowds out the sentence that would.
   */
  export let title: string = problemNoticeCopy.title;
  export let message: string = problemNoticeCopy.message;
  export let problem: Problem | null = null;
</script>

<div class="notice" role="alert">
  <span class="notice-mark" aria-hidden="true">◇</span>
  <span>
    <strong>{problem?.title ?? title}</strong>
    <span>{problem !== null ? humanProblemDetail(problem) : message}</span>
    {#if problem !== null}
      <details class="notice-technical">
        <summary>{problemNoticeCopy.technicalDetail}</summary>
        <code>{problem.type}</code>
        <code>{problemNoticeCopy.http} {problem.status}</code>
      </details>
    {/if}
  </span>
</div>

<style>
  .notice-technical {
    margin-top: var(--space-2);
    font-size: var(--text-xs);
  }

  .notice-technical summary {
    display: flex;
    align-items: center;
    min-height: var(--tap);
    cursor: pointer;
  }

  .notice-technical code {
    display: block;
    overflow-wrap: anywhere;
  }
</style>
