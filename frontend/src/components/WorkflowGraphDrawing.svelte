<script lang="ts" context="module">
  let nextMarker = 0;

  const kindMarks: Record<string, string> = {
    agent: "●",
    wait: "○",
    action: "■",
    deterministic: "◆",
    subworkflow: "▣"
  };
</script>

<script lang="ts">
  import { onMount, tick } from "svelte";

  import type { NodeState } from "../lib/runProjection";
  import { layerWorkflowGraph } from "../lib/workflowGraph";
  import StateMark, { stateLabels } from "./StateMark.svelte";

  export let previews: readonly {
    id: string;
    kind: "agent" | "deterministic" | "wait" | "subworkflow" | "action";
    role: string | null;
    instruction_start: string | null;
    depends_on: readonly string[];
  }[];
  export let rail: readonly { node_id: string; state: NodeState }[] = [];
  export let currentNodeId: string | null = null;
  export let selectedNodeId: string | null = null;
  export let onSelect: ((nodeId: string) => void) | null = null;
  export let showExcerpt = false;

  const markerId = `workflow-graph-arrow-${nextMarker++}`;

  type EdgePath = { key: string; d: string };

  let host: HTMLElement;
  let edgePaths: EdgePath[] = [];

  $: layered = layerWorkflowGraph(previews);
  $: stateById = new Map(rail.map((entry) => [entry.node_id, entry.state]));
  $: scheduleEdges(layered, previews);

  function scheduleEdges(next: typeof layered, nodes: typeof previews): void {
    void next;
    void nodes;
    void tick().then(applyEdges);
  }

  function nodeLabel(id: string, state: NodeState | undefined): string {
    return state === undefined ? id : `${id} — ${stateLabels[state]}`;
  }

  function applyEdges(): void {
    const next = measureEdges();
    if (
      next.length === edgePaths.length &&
      next.every((edge, index) => edge.key === edgePaths[index]?.key && edge.d === edgePaths[index]?.d)
    ) {
      return;
    }
    edgePaths = next;
  }

  function measureEdges(): EdgePath[] {
    if (host == null || layered.ok === false) return [];
    const root = host.getBoundingClientRect();
    const next: EdgePath[] = [];
    for (const preview of previews) {
      const to = host.querySelector(`[data-node-id="${CSS.escape(preview.id)}"]`);
      if (!(to instanceof HTMLElement)) continue;
      const toBox = to.getBoundingClientRect();
      for (const dependency of preview.depends_on) {
        const from = host.querySelector(`[data-node-id="${CSS.escape(dependency)}"]`);
        if (!(from instanceof HTMLElement)) continue;
        const fromBox = from.getBoundingClientRect();
        const x1 = fromBox.left + fromBox.width / 2 - root.left;
        const y1 = fromBox.top + fromBox.height / 2 - root.top;
        const x2 = toBox.left + toBox.width / 2 - root.left;
        const y2 = toBox.top + toBox.height / 2 - root.top;
        const midX = (x1 + x2) / 2;
        next.push({
          key: `${dependency}->${preview.id}`,
          d: `M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`
        });
      }
    }
    return next;
  }

  onMount(() => {
    applyEdges();
    if (typeof ResizeObserver === "undefined" || host == null) return;
    const observer = new ResizeObserver(() => applyEdges());
    observer.observe(host);
    return () => observer.disconnect();
  });
</script>

<section class="workflow-graph" bind:this={host} aria-label="Workflow">
  {#if !layered.ok}
    <p class="muted" role="status">{layered.reason}</p>
  {:else}
    <svg class="graph-edges" aria-hidden="true">
      <defs>
        <marker
          id={markerId}
          viewBox="0 0 10 10"
          refX="8"
          refY="5"
          markerWidth="6"
          markerHeight="6"
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
        </marker>
      </defs>
      {#each edgePaths as edge (edge.key)}
        <path d={edge.d} fill="none" stroke="currentColor" stroke-width="1.5" marker-end="url(#{markerId})" />
      {/each}
    </svg>
    <div class="graph-layers">
      {#each layered.layers as layer, layerIndex (layerIndex)}
        <div class="graph-layer" data-layer={layerIndex}>
          {#each layer as preview (preview.id)}
            {@const state = stateById.get(preview.id)}
            {@const label = nodeLabel(preview.id, state)}
            {#if onSelect !== null}
              <button
                type="button"
                class="graph-node"
                class:current={preview.id === currentNodeId}
                data-node-id={preview.id}
                data-layer={layerIndex}
                data-state={state}
                aria-label={label}
                aria-expanded={selectedNodeId === preview.id}
                on:click={() => onSelect?.(preview.id)}
              >
                <header class="graph-node-header">
                  <span class="node-kind">{preview.kind}</span>
                  {#if state !== undefined}
                    <StateMark {state} />
                  {:else}
                    <span class="kind-mark" aria-hidden="true">{kindMarks[preview.kind]}</span>
                  {/if}
                </header>
                <strong class="node-id">{preview.id}</strong>
                {#if showExcerpt && preview.role !== null}
                  <span class="node-role">{preview.role}</span>
                {/if}
                {#if showExcerpt && preview.instruction_start !== null}
                  <p class="node-instruction">{preview.instruction_start}</p>
                {/if}
              </button>
            {:else}
              <article
                class="graph-node"
                class:current={preview.id === currentNodeId}
                data-node-id={preview.id}
                data-layer={layerIndex}
                data-state={state}
                aria-label={label}
              >
                <header class="graph-node-header">
                  <span class="node-kind">{preview.kind}</span>
                  {#if state !== undefined}
                    <StateMark {state} />
                  {:else}
                    <span class="kind-mark" aria-hidden="true">{kindMarks[preview.kind]}</span>
                  {/if}
                </header>
                <strong class="node-id">{preview.id}</strong>
                {#if showExcerpt && preview.role !== null}
                  <span class="node-role">{preview.role}</span>
                {/if}
                {#if showExcerpt && preview.instruction_start !== null}
                  <p class="node-instruction">{preview.instruction_start}</p>
                {/if}
              </article>
            {/if}
          {/each}
        </div>
      {/each}
    </div>
  {/if}
</section>

<style>
  .workflow-graph {
    position: relative;
  }

  .graph-edges {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    overflow: visible;
    color: var(--line);
  }

  .graph-layers {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(9rem, 1fr);
    gap: 1.5rem;
    align-items: start;
    position: relative;
  }

  .graph-layer {
    display: grid;
    gap: 0.75rem;
  }

  .graph-node {
    display: grid;
    gap: 0.25rem;
    width: 100%;
    min-height: 44px;
    border: 1px solid var(--line);
    border-left-width: 0.45rem;
    border-radius: 0.75rem;
    padding: 0.65rem 0.75rem;
    background: var(--paper);
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: default;
  }

  button.graph-node {
    cursor: pointer;
  }

  .graph-node.current {
    background: color-mix(in srgb, currentColor 8%, var(--paper));
  }

  .graph-node[data-state="queued"] {
    border-left-style: dashed;
    border-left-color: var(--queued);
  }

  .graph-node[data-state="working"] {
    border-left-color: var(--working);
  }

  .graph-node[data-state="needs_you"] {
    border-left-color: var(--danger);
  }

  .graph-node[data-state="succeeded"] {
    border-left-color: var(--accent);
  }

  .graph-node[data-state="failed"],
  .graph-node[data-state="interrupted"] {
    border-left-color: var(--warning);
  }

  .graph-node[data-state="cancelled"] {
    border-left-color: var(--queued);
  }

  .graph-node-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .kind-mark {
    color: var(--muted);
  }

  .node-id {
    font-size: 1.05rem;
  }

  .node-role {
    color: var(--muted);
    font-size: 0.85rem;
  }

  .node-instruction {
    margin: 0.1rem 0 0;
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .muted {
    color: var(--muted);
  }

  @media (max-width: 40rem) {
    .graph-layers {
      grid-auto-flow: row;
      grid-auto-columns: unset;
    }
  }
</style>
