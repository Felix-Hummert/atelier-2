<script lang="ts" context="module">
  import type { NodeState } from "../lib/runProjection";
  import { stateLabels } from "../lib/stateMarkCopy";

  export { stateLabels };

  /**
   * The glyph that carries a state without colour, for eyes that read no
   * colour. It sits beside `stateLabels` so the run graph's small shapes
   * and this mark can never disagree about what "done" looks like.
   */
  export const stateGlyphs: Record<NodeState, string> = {
    queued: "",
    working: "▲",
    needs_you: "!",
    succeeded: "✓",
    failed: "×",
    cancelled: "—",
    interrupted: "◇"
  };
</script>

<script lang="ts">
  export let state: NodeState;
  export let animated = true;
</script>

<span class="state-mark state-{state}" class:state-still={!animated} data-state={state}>
  <span class="state-shape" aria-hidden="true"><span>{stateGlyphs[state]}</span></span>
  <strong>{stateLabels[state]}</strong>
</span>
