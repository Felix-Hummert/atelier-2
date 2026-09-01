<script lang="ts">
  import { readStateCopy } from "../lib/readStateCopy";

  /**
   * REQ-UIQ-10's loading state ("Laden ist ein stilles Skelett") and
   * REQ-UIQ-13 (one behaviour, one component across surfaces): the single
   * owner every "Looking…" rendering consumes instead of composing its own
   * spinner glyph or dimmed heading. The skeleton shape is the primary
   * signal a sighted operator reads; `label` still carries the sentence law
   * 6 (`design-interface`) asks for -- an honest word for what is loading,
   * kept for assistive technology and for the reader who wants it.
   */
  export let label: string = readStateCopy.looking;
  /**
   * The card form (default) is a standalone loading region: dashed border,
   * two skeleton lines, room to breathe. `compact` drops that chrome for a
   * loading state that already sits inside another element's own box (a
   * heading, a status line, a button) -- one bar, no border, no padding.
   */
  export let compact = false;
</script>

<span class="loading-state" class:compact role="status">
  <span class="loading-mark" aria-hidden="true"></span>
  <span class="loading-lines" aria-hidden="true">
    <span></span>
    {#if !compact}<span class="short"></span>{/if}
  </span>
  <span class="loading-label">{label}</span>
</span>

<style>
  .loading-state {
    display: inline-flex;
    align-items: center;
    gap: var(--space-3);
    width: 100%;
    border: var(--edge) dashed var(--signal-live);
    border-radius: var(--r-lg);
    background: var(--panel2);
    box-sizing: border-box;
    padding: var(--space-3) var(--space-4);
    font-size: var(--text-sm);
    color: var(--ink-dim);
  }

  .loading-state.compact {
    width: auto;
    gap: var(--space-2);
    border: 0;
    background: transparent;
    padding: 0;
  }

  .loading-mark {
    width: var(--mark);
    height: var(--mark);
    border: var(--edge-strong) solid var(--signal-live);
    border-radius: var(--r);
    position: relative;
    flex: none;
  }

  .loading-mark::after {
    content: "";
    position: absolute;
    inset: var(--space-1);
    background: var(--signal-live);
    opacity: 0.35;
  }

  .loading-lines {
    display: grid;
    gap: var(--space-1);
    flex: 1;
    min-width: 0;
  }

  /*
   * A compact host (a heading, a button, a status line) is often itself
   * shrink-to-fit, so `flex: 1` has no free space to grow into and the bar
   * collapses to nothing. A fixed width -- the same `--mark` scale the
   * shape mark already draws from -- keeps the bar visible regardless of
   * what the host's own width resolves to.
   */
  .loading-state.compact .loading-lines {
    flex: none;
    width: var(--mark);
  }

  .loading-lines span {
    display: block;
    height: var(--space-2);
    border-radius: var(--r-pill);
    background: var(--chip);
  }

  .loading-lines .short {
    width: 62%;
  }
</style>
