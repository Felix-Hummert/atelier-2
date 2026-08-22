<script lang="ts">
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { workflowsPageCopy } from "../lib/workflowsPageCopy";

  /**
   * What a click on the still graph opens: the authored template a run of
   * this node would carry, never an executed result. `NodeDetailPanel`
   * answers a different question — what a run's node actually did — over a
   * different resource (`NodeDetail`, only readable once a run exists). A
   * catalog node has no run yet, so it has no output, provenance, or log to
   * show; this panel does not pretend otherwise.
   *
   * `instruction_start` is the published excerpt (up to 120 characters), not
   * the full authored prompt — the read API does not carry the rest, so this
   * panel never claims to show a whole prompt.
   */
  export let preview: {
    id: string;
    kind: "agent" | "deterministic" | "wait" | "subworkflow" | "action";
    role: string | null;
    instruction_start: string | null;
    depends_on: readonly string[];
  };
  export let onClose: () => void;
</script>

<aside class="node-preview-panel" aria-labelledby="node-preview-title">
  <header>
    <div>
      <p class="eyebrow">{wrapDisplayCopy(workflowsPageCopy.panelTitle)}</p>
      <h2 id="node-preview-title">{preview.id}</h2>
      <span class="node-kind">{preview.kind}</span>
    </div>
    <button type="button" class="close" on:click={onClose} aria-label={workflowsPageCopy.panelClose}>
      ×
    </button>
  </header>

  <section aria-labelledby="node-preview-role">
    <h3 id="node-preview-role">{wrapDisplayCopy(workflowsPageCopy.panelRole)}</h3>
    {#if preview.role === null}
      <p class="muted">{wrapDisplayCopy(workflowsPageCopy.panelNoRole)}</p>
    {:else}
      <p>{preview.role}</p>
    {/if}
  </section>

  <section aria-labelledby="node-preview-prompt">
    <h3 id="node-preview-prompt">{wrapDisplayCopy(workflowsPageCopy.panelPromptStart)}</h3>
    {#if preview.instruction_start === null}
      <p class="muted">{wrapDisplayCopy(workflowsPageCopy.panelNoPromptStart)}</p>
    {:else}
      <p class="exact">{preview.instruction_start}</p>
    {/if}
  </section>
</aside>

<style>
  .node-preview-panel {
    display: grid;
    gap: 0.9rem;
    padding: 1rem;
    border-radius: 0.5rem;
    background: color-mix(in srgb, currentColor 5%, transparent);
  }

  .node-preview-panel header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.75rem;
  }

  h2 {
    margin: 0;
  }

  .node-kind {
    display: inline-block;
    margin-top: 0.2rem;
    color: var(--muted);
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  h3 {
    margin: 0 0 0.3rem;
    font-size: 0.85rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    opacity: 0.75;
  }

  .close {
    border: 0;
    background: transparent;
    font-size: 1.25rem;
    line-height: 1;
    cursor: pointer;
    color: inherit;
  }

  .exact {
    margin: 0;
    padding: 0.6rem;
    border-radius: 0.4rem;
    background: color-mix(in srgb, currentColor 7%, transparent);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .muted {
    opacity: 0.7;
  }
</style>
