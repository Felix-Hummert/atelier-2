<script lang="ts" context="module">
  let nextMarker = 0;

  /**
   * Form carries type, the same way it does for state (`StateMark`): a shape
   * class on `.kind-mark`, never a color alone. Only the three kinds the
   * target mockup (docs/requirements/0003-ziel-ui-mockup-v5.html §03/§04)
   * names a shape for — circle, square, hexagon — get one here.
   * `deterministic` and `subworkflow` keep the plain dot this component always
   * drew; the subworkflow shape is unfixed until a real composed-workflow node
   * exists to fix it against (ADR 0006), and a dot commits to nothing.
   */
  const KIND_LEGEND_ENTRIES = ["agent", "action", "wait"] as const;

  export const kindLegendLabels: Record<(typeof KIND_LEGEND_ENTRIES)[number], string> = {
    agent: "Agent",
    action: "Action",
    wait: "Wait"
  };

  /**
   * One declared loop, exactly as the wire projects it (`WorkflowLoopResourceV3`).
   *
   * `member_node_ids` names the loop's body by the ids `previews` already
   * carries — the drawing groups the matching nodes rather than holding a
   * second copy of them.
   */
  export type WorkflowGraphLoop = {
    id: string;
    member_node_ids: readonly string[];
    maximum_rounds: number;
    repeat_while: { node: string; verdict: string } | null;
  };
</script>

<script lang="ts">
  import { onMount, tick } from "svelte";

  import { nodeIsLiveWork } from "../lib/liveWatch";
  import type { NodeState } from "../lib/runProjection";
  import { layerWorkflowGraph } from "../lib/workflowGraph";
  import StateMark, { stateLabels } from "./StateMark.svelte";

  type WorkflowGraphPreview = {
    id: string;
    kind: "agent" | "deterministic" | "wait" | "subworkflow" | "action";
    role: string | null;
    instruction_start: string | null;
    depends_on: readonly string[];
  };

  export let previews: readonly WorkflowGraphPreview[];
  export let loops: readonly WorkflowGraphLoop[] = [];
  export let rail: readonly { node_id: string; state: NodeState }[] = [];
  export let nodeReasons: ReadonlyMap<string, string> = new Map();
  export let currentNodeId: string | null = null;
  export let selectedNodeId: string | null = null;
  export let onSelect: ((nodeId: string) => void) | null = null;
  export let showExcerpt = false;
  export let showLegend = false;

  const markerId = `workflow-graph-arrow-${nextMarker++}`;

  type EdgePath = { key: string; d: string };
  type LayerSlot = { index: number; nodes: readonly WorkflowGraphPreview[] };
  type LayerSegment =
    | { kind: "loop"; key: string; loop: WorkflowGraphLoop; slots: LayerSlot[] }
    | { kind: "plain"; key: string; slot: LayerSlot };

  let host: HTMLElement;
  let edgePaths: EdgePath[] = [];

  $: layered = layerWorkflowGraph(previews);
  $: stateById = new Map(rail.map((entry) => [entry.node_id, entry.state]));
  $: segments = layered.ok === true ? segmentLayers(layered.layers, loops) : [];
  $: scheduleEdges(layered, previews);

  function scheduleEdges(next: typeof layered, nodes: typeof previews): void {
    void next;
    void nodes;
    void tick().then(applyEdges);
  }

  function nodeLabel(id: string, state: NodeState | undefined): string {
    return state === undefined ? id : `${id} — ${stateLabels[state]}`;
  }

  /**
   * Every loop's declared member, read back the other way: which loop, if
   * any, owns this node id. A node belongs to at most one loop -- the
   * document that declared two is refused before it publishes -- so the last
   * write here can never overwrite a different owner.
   */
  function loopByMemberId(
    declaredLoops: readonly WorkflowGraphLoop[]
  ): ReadonlyMap<string, WorkflowGraphLoop> {
    return new Map(
      declaredLoops.flatMap((loop) =>
        loop.member_node_ids.map((memberId) => [memberId, loop] as const)
      )
    );
  }

  /** The one loop every node of this layer belongs to, or none where they differ. */
  function layerLoop(
    nodes: readonly WorkflowGraphPreview[],
    owners: ReadonlyMap<string, WorkflowGraphLoop>
  ): WorkflowGraphLoop | null {
    const first = nodes[0];
    if (first === undefined) return null;
    const loop = owners.get(first.id);
    if (loop === undefined) return null;
    return nodes.every((node) => owners.get(node.id)?.id === loop.id) ? loop : null;
  }

  /**
   * The topological layers, regrouped so consecutive layers of one loop's
   * body share a single wrapping box -- the shape the target mockup draws
   * (docs/requirements/0003-ziel-ui-mockup-v5.html §03/§04): one dashed box
   * around a loop's whole body, not one per member.
   *
   * A layer that mixes a loop's member with an unrelated node stays outside
   * any box, and once that split leaves a loop's box unable to hold its whole
   * declared body, every fragment of it is unboxed rather than drawn as a
   * box around only part of the loop -- which of the two the box would
   * belong to is not this drawing's decision, so it draws the honest
   * picture, no box, rather than a box that names less than the document
   * declared.
   */
  function segmentLayers(
    layers: readonly (readonly WorkflowGraphPreview[])[],
    declaredLoops: readonly WorkflowGraphLoop[]
  ): LayerSegment[] {
    const owners = loopByMemberId(declaredLoops);
    const provisional: LayerSegment[] = [];
    layers.forEach((nodes, index) => {
      const loop = layerLoop(nodes, owners);
      const slot: LayerSlot = { index, nodes };
      const previous = provisional[provisional.length - 1];
      if (loop !== null && previous?.kind === "loop" && previous.loop.id === loop.id) {
        previous.slots.push(slot);
        return;
      }
      provisional.push(
        loop === null
          ? { kind: "plain", key: `layer-${index}`, slot }
          : { kind: "loop", key: `loop-${loop.id}-${index}`, loop, slots: [slot] }
      );
    });
    return unboxIncompleteLoops(provisional);
  }

  /**
   * Every "loop" segment whose slots hold fewer nodes than the loop declares, unboxed.
   *
   * Coverage is keyed by loop id in a `Map`, the way every other id lookup in
   * this file already is -- never a plain object, whose key is an authored
   * string an author is free to write as `__proto__` and reach the prototype
   * accessor instead of a data slot.
   */
  function unboxIncompleteLoops(segments: readonly LayerSegment[]): LayerSegment[] {
    const loopSegments = segments.filter(
      (segment): segment is Extract<LayerSegment, { kind: "loop" }> => segment.kind === "loop"
    );
    const coveredNodeCounts = new Map(
      [...new Set(loopSegments.map((segment) => segment.loop.id))].map((loopId) => [
        loopId,
        loopSegments
          .filter((segment) => segment.loop.id === loopId)
          .reduce(
            (total, segment) =>
              total + segment.slots.reduce((sum, slot) => sum + slot.nodes.length, 0),
            0
          )
      ])
    );
    return segments.flatMap((segment) => {
      if (segment.kind !== "loop") return [segment];
      if (coveredNodeCounts.get(segment.loop.id) === segment.loop.member_node_ids.length) {
        return [segment];
      }
      return segment.slots.map(
        (slot): LayerSegment => ({ kind: "plain", key: `layer-${slot.index}`, slot })
      );
    });
  }

  /** "until <verdict> · max <n>", or "max <n>" where the document declares no verdict exit. */
  function loopLabel(loop: WorkflowGraphLoop): string {
    const bound = `max ${loop.maximum_rounds}`;
    return loop.repeat_while === null
      ? `↻ ${bound}`
      : `↻ until ${loop.repeat_while.verdict} · ${bound}`;
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
  {#if showLegend}
    <ul class="graph-legend" aria-label="Node shapes and the loop marker">
      {#each KIND_LEGEND_ENTRIES as kind (kind)}
        <li><span class="kind-mark kind-mark-{kind}" aria-hidden="true"></span>{kindLegendLabels[kind]}</li>
      {/each}
      <li><span class="kind-mark kind-mark-loop" aria-hidden="true"></span>Loop</li>
    </ul>
  {/if}
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
    {#snippet layerCard(slot: LayerSlot)}
      <div class="graph-layer" data-layer={slot.index}>
        {#each slot.nodes as preview (preview.id)}
          {@const state = stateById.get(preview.id)}
          {@const label = nodeLabel(preview.id, state)}
          {@const reason = nodeReasons.get(preview.id)}
          {#if onSelect !== null}
            <button
              type="button"
              class="graph-node"
              class:current={preview.id === currentNodeId}
              class:live-work={nodeIsLiveWork(state)}
              data-node-id={preview.id}
              data-layer={slot.index}
              data-state={state}
              data-live={nodeIsLiveWork(state) ? "true" : undefined}
              aria-label={label}
              aria-expanded={selectedNodeId === preview.id}
              on:click={() => onSelect?.(preview.id)}
            >
              <header class="graph-node-header">
                <span class="node-kind">{preview.kind}</span>
                {#if state !== undefined}
                  <StateMark {state} />
                {:else}
                  <span class="kind-mark kind-mark-{preview.kind}" aria-hidden="true"></span>
                {/if}
              </header>
              <strong class="node-id">{preview.id}</strong>
              {#if showExcerpt && preview.role !== null}
                <span class="node-role">{preview.role}</span>
              {/if}
              {#if showExcerpt && preview.instruction_start !== null}
                <p class="node-instruction">{preview.instruction_start}</p>
              {/if}
              {#if reason !== undefined}
                <p class="node-reason" role="alert">{reason}</p>
              {/if}
            </button>
          {:else}
            <article
              class="graph-node"
              class:current={preview.id === currentNodeId}
              class:live-work={nodeIsLiveWork(state)}
              data-node-id={preview.id}
              data-layer={slot.index}
              data-state={state}
              data-live={nodeIsLiveWork(state) ? "true" : undefined}
              aria-label={label}
            >
              <header class="graph-node-header">
                <span class="node-kind">{preview.kind}</span>
                {#if state !== undefined}
                  <StateMark {state} />
                {:else}
                  <span class="kind-mark kind-mark-{preview.kind}" aria-hidden="true"></span>
                {/if}
              </header>
              <strong class="node-id">{preview.id}</strong>
              {#if showExcerpt && preview.role !== null}
                <span class="node-role">{preview.role}</span>
              {/if}
              {#if showExcerpt && preview.instruction_start !== null}
                <p class="node-instruction">{preview.instruction_start}</p>
              {/if}
              {#if reason !== undefined}
                <p class="node-reason" role="alert">{reason}</p>
              {/if}
            </article>
          {/if}
        {/each}
      </div>
    {/snippet}
    <div class="graph-layers">
      {#each segments as segment (segment.key)}
        {#if segment.kind === "loop"}
          {@const labelId = `${markerId}-loop-${segment.loop.id}`}
          <div class="loop-box" role="group" aria-labelledby={labelId}>
            <span class="loop-box-label" id={labelId}>{loopLabel(segment.loop)}</span>
            {#each segment.slots as slot (slot.index)}
              {@render layerCard(slot)}
            {/each}
          </div>
        {:else}
          {@render layerCard(segment.slot)}
        {/if}
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

  /*
   * A loop's body is drawn as one dashed box around its whole line of
   * members (docs/requirements/0003-ziel-ui-mockup-v5.html §03/§04), never
   * colored by any node's state -- the box names structure the document
   * declared, not a run's progress through it.
   */
  .loop-box {
    display: flex;
    align-items: flex-start;
    gap: 1.5rem;
    position: relative;
    border: 1.5px dashed var(--line);
    border-radius: 0.8rem;
    padding: 1.15rem 0.75rem 0.75rem;
  }

  .loop-box > .graph-layer {
    flex: 1 1 9rem;
    min-width: 9rem;
  }

  .loop-box-label {
    position: absolute;
    top: -0.65rem;
    left: 0.75rem;
    background: var(--paper);
    padding: 0 0.4rem;
    font-size: 0.72rem;
    color: var(--muted);
    white-space: nowrap;
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

  .graph-node.live-work {
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--working) 45%, transparent);
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
    display: inline-block;
    width: 0.7rem;
    height: 0.7rem;
    border: 1.5px solid var(--muted);
    background: transparent;
    flex: none;
  }

  .kind-mark-agent {
    border-radius: 50%;
  }

  .kind-mark-action {
    border-radius: 0.15rem;
  }

  .kind-mark-wait {
    border: none;
    background: var(--muted);
    clip-path: polygon(25% 0, 75% 0, 100% 50%, 75% 100%, 25% 100%, 0 50%);
  }

  .kind-mark-deterministic,
  .kind-mark-subworkflow {
    border-radius: 50%;
    width: 0.5rem;
    height: 0.5rem;
  }

  .kind-mark-loop {
    border-style: dashed;
    border-radius: 0.2rem;
  }

  .graph-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.85rem;
    margin: 0 0 0.75rem;
    padding: 0;
    list-style: none;
    font-size: 0.78rem;
    color: var(--muted);
  }

  .graph-legend li {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
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

  .node-reason {
    margin: 0.25rem 0 0;
    color: var(--warning);
    font-size: 0.85rem;
    overflow-wrap: anywhere;
  }

  .muted {
    color: var(--muted);
  }

  @media (max-width: 40rem) {
    .graph-layers {
      grid-auto-flow: row;
      grid-auto-columns: unset;
    }

    .loop-box {
      flex-direction: column;
    }
  }
</style>
