<script lang="ts" context="module">
  let nextMarker = 0;

  /**
   * Form carries type, colour carries state (mockup v5 §03/§04): a circle is
   * an agent, a square an action, a hexagon a wait. Only those three kinds
   * have a declared shape. `deterministic` and `subworkflow` keep the plain
   * circle this drawing always drew for them; the subworkflow shape is unfixed
   * until a real composed-workflow node exists to fix it against (ADR 0006),
   * and an unmarked circle commits to nothing.
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
  import { stateGlyphs, stateLabels } from "./StateMark.svelte";

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
  export let currentNodeId: string | null = null;
  export let selectedNodeId: string | null = null;
  export let onSelect: ((nodeId: string) => void) | null = null;

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
      const to = host.querySelector(`[data-node-id="${CSS.escape(preview.id)}"] .pipe-shape`);
      if (!(to instanceof HTMLElement)) continue;
      const toBox = to.getBoundingClientRect();
      for (const dependency of preview.depends_on) {
        const from = host.querySelector(`[data-node-id="${CSS.escape(dependency)}"] .pipe-shape`);
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

<div class="workflow-graph card">
  <details class="graph-help">
    <summary class="reveal-affordance">What the shapes mean</summary>
    <ul class="graph-legend" aria-label="Node shapes and the loop marker">
      {#each KIND_LEGEND_ENTRIES as kind (kind)}
        <li><span class="kind-mark kind-mark-{kind}" aria-hidden="true"></span>{kindLegendLabels[kind]}</li>
      {/each}
      <li><span class="kind-mark kind-mark-loop" aria-hidden="true"></span>Loop</li>
    </ul>
  </details>
  <section class="graph-canvas" bind:this={host} aria-label="Workflow">
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
      {#snippet stageBody(preview: WorkflowGraphPreview, state: NodeState | undefined)}
        <span class="pipe-shape" aria-hidden="true"><i>{state === undefined ? "" : stateGlyphs[state]}</i></span>
        <b class="pipe-name">{preview.id}</b>
      {/snippet}
      {#snippet layerCard(slot: LayerSlot)}
        <div class="graph-layer" data-layer={slot.index}>
          {#each slot.nodes as preview (preview.id)}
            {@const state = stateById.get(preview.id)}
            {#if onSelect !== null}
              <button
                type="button"
                class="pipe-stage"
                class:current={preview.id === currentNodeId}
                class:live-work={nodeIsLiveWork(state)}
                data-node-id={preview.id}
                data-node-kind={preview.kind}
                data-layer={slot.index}
                data-state={state}
                data-live={nodeIsLiveWork(state) ? "true" : undefined}
                aria-label={nodeLabel(preview.id, state)}
                aria-expanded={selectedNodeId === preview.id}
                on:click={() => onSelect?.(preview.id)}
              >{@render stageBody(preview, state)}</button>
            {:else}
              <span
                class="pipe-stage"
                data-node-id={preview.id}
                data-node-kind={preview.kind}
                data-layer={slot.index}
              >{@render stageBody(preview, undefined)}</span>
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
</div>

<style>
  /* Framed as a panel, `.card`'s own border and ground, so the graph reads as
     an object on the page rather than shapes floating over bare ground -- a
     single node draws exactly as held as a whole chain (operator ruling
     23.08.). The help disclosure sits outside the scrollable canvas so it
     never scrolls away with a wide graph. */
  .workflow-graph {
    display: grid;
    gap: var(--space-3);
  }

  /* A tap target no smaller than any other control's, the same floor
     `.event-log summary` and `.revision-details summary` already hold to. */
  .graph-help summary {
    display: flex;
    align-items: center;
    min-height: var(--tap);
    cursor: pointer;
  }

  .graph-canvas {
    position: relative;
    overflow-x: auto;
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
    display: flex;
    align-items: flex-start;
    gap: var(--pipe-link);
    position: relative;
    width: max-content;
    min-width: 100%;
  }

  .graph-layer {
    display: grid;
    justify-items: center;
    gap: var(--space-3);
  }

  /*
   * A loop's body is drawn as one dashed box around its whole line of
   * members (mockup v5 §03/§04), never coloured by any node's state -- the
   * box names structure the document declared, not a run's progress through
   * it.
   */
  .loop-box {
    display: flex;
    align-items: flex-start;
    gap: var(--pipe-link);
    position: relative;
    border: var(--edge) dashed var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-5) var(--space-2) var(--space-2);
  }

  .loop-box-label {
    position: absolute;
    top: calc(var(--space-3) * -1);
    left: var(--space-4);
    background: var(--ground);
    padding: 0 var(--space-2);
    font-size: var(--text-2xs);
    color: var(--ink-dim);
    white-space: nowrap;
  }

  /*
   * A node is a small shape on a line, not a card: the mockup's pipe. Form is
   * the kind, colour is the state, the name sits quietly underneath, and
   * everything else -- role, prompt, output, receipt -- waits for a click.
   */
  .pipe-stage {
    display: grid;
    justify-items: center;
    align-content: start;
    width: var(--pipe-stage);
    padding: 0;
    border: 0;
    border-radius: var(--r);
    color: var(--signal-quiet);
    background: transparent;
    font: inherit;
    text-align: center;
  }

  button.pipe-stage {
    cursor: pointer;
  }

  .pipe-shape {
    display: grid;
    place-items: center;
    width: var(--pipe-node);
    height: var(--pipe-node);
    position: relative;
    border: var(--pipe-stroke) solid currentColor;
    border-radius: 50%;
    background: var(--panel2);
    color: inherit;
    font-size: var(--text-sm);
    font-weight: var(--weight-heavy);
  }

  .pipe-stage[data-node-kind="action"] .pipe-shape {
    border-radius: var(--r);
  }

  /* A hexagon cannot be drawn with a border, so the clip is doubled: the outer
     shape is the colour, the inner one punches the panel back out of it. */
  .pipe-stage[data-node-kind="wait"] .pipe-shape {
    border: 0;
    border-radius: 0;
    background: currentColor;
    clip-path: polygon(25% 0, 75% 0, 100% 50%, 75% 100%, 25% 100%, 0 50%);
  }

  .pipe-stage[data-node-kind="wait"] .pipe-shape > i {
    position: absolute;
    inset: var(--pipe-stroke);
    display: grid;
    place-items: center;
    clip-path: polygon(25% 0, 75% 0, 100% 50%, 75% 100%, 25% 100%, 0 50%);
    background: var(--panel2);
    font-style: normal;
  }

  .pipe-shape > i {
    font-style: normal;
    line-height: 1;
  }

  .pipe-name {
    margin-top: var(--space-2);
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
    overflow-wrap: anywhere;
  }

  .pipe-stage[data-state="working"] {
    color: var(--signal-live);
  }

  .pipe-stage[data-state="needs_you"] {
    color: var(--signal-attention-mark);
  }

  .pipe-stage[data-state="failed"],
  .pipe-stage[data-state="interrupted"] {
    color: var(--signal-failure);
  }

  /* Where the run stands is said in ink and weight. An underline means a
     link everywhere else in the house, and may not mean a second thing here. */
  .pipe-stage.current .pipe-name {
    color: var(--ink);
    font-weight: var(--weight-heavy);
  }

  .pipe-stage.live-work .pipe-shape::after {
    content: "";
    position: absolute;
    inset: calc(var(--pipe-stroke) * -2.5);
    border: var(--pipe-stroke) solid currentColor;
    border-radius: 50%;
    opacity: 0.55;
    animation: pipe-pulse 1.6s ease-out infinite;
  }

  @keyframes pipe-pulse {
    0% {
      transform: scale(0.82);
      opacity: 0.55;
    }

    100% {
      transform: scale(1.25);
      opacity: 0;
    }
  }

  .kind-mark {
    display: inline-block;
    width: var(--mark-sm);
    height: var(--mark-sm);
    border: var(--edge-strong) solid var(--ink-dim);
    background: transparent;
    flex: none;
  }

  .kind-mark-agent {
    border-radius: 50%;
  }

  .kind-mark-action {
    border-radius: var(--r-sm);
  }

  .kind-mark-wait {
    border: none;
    background: var(--ink-dim);
    clip-path: polygon(25% 0, 75% 0, 100% 50%, 75% 100%, 25% 100%, 0 50%);
  }

  /* The legend's loop mark is the loop box itself in miniature: a dashed
     frame around several nodes, not a second reading of the action square. */
  .kind-mark-loop {
    width: calc(var(--mark-sm) * 1.75);
    border-style: dashed;
    border-radius: var(--r-sm);
  }

  .graph-legend {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-4);
    margin: var(--space-2) 0 0;
    padding: 0;
    list-style: none;
    font-size: var(--text-xs);
    color: var(--ink-dim);
  }

  .graph-legend li {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
  }

  .muted {
    color: var(--ink-dim);
  }
</style>
