<script lang="ts" context="module">
  import type { NodeState } from "../lib/runProjection";

  /** What the operator reads for a state the server named. One owner, one word. */
  export const stateLabels: Record<NodeState, string> = {
    queued: "Queued",
    working: "Working",
    needs_you: "Needs you",
    succeeded: "Done",
    failed: "Failed",
    cancelled: "Cancelled",
    interrupted: "Interrupted"
  };
</script>

<script lang="ts">
  export let state: NodeState;
  export let animated = true;

  const shapes: Record<NodeState, string> = {
    queued: "",
    working: "▲",
    needs_you: "!",
    succeeded: "✓",
    failed: "×",
    cancelled: "—",
    interrupted: "◇"
  };
</script>

<span class="state-mark state-{state}" class:state-still={!animated} data-state={state}>
  <span class="state-shape" aria-hidden="true"><span>{shapes[state]}</span></span>
  <strong>{stateLabels[state]}</strong>
</span>
