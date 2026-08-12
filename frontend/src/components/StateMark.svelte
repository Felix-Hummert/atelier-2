<script lang="ts">
  import type { NodeState } from "../lib/runProjection";

  export let state: NodeState;
  export let animated = true;

  const labels: Record<NodeState, string> = {
    queued: "Queued",
    working: "Working",
    needs_you: "Needs you",
    done: "Done"
  };
  const shapes: Record<NodeState, string> = {
    queued: "",
    working: "▲",
    needs_you: "!",
    done: "✓"
  };
</script>

<span class="state-mark state-{state}" class:state-still={!animated} data-state={state}>
  <span class="state-shape" aria-hidden="true"><span>{shapes[state]}</span></span>
  <strong>{labels[state]}</strong>
</span>
