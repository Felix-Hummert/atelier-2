<script lang="ts">
  import type { CockpitApi, NodeDetail, RunV3 } from "../api/client";
  import type { StreamProjection } from "../lib/runProjection";
  import InfoHint from "./InfoHint.svelte";
  import NodeDetailPanel from "./NodeDetailPanel.svelte";
  import ProblemNotice from "./ProblemNotice.svelte";
  import StateMark from "./StateMark.svelte";

  export let run: RunV3;
  export let cockpitApi: CockpitApi;
  export let projection: StreamProjection | null = null;

  const PREVIEW_CHARACTERS = 120;

  /**
   * What arrived while the operator was watching, newest last.
   *
   * Only the two kinds that say a node is done with its turn: a completion and a
   * failure. The rest of the stream is real and is kept by the projection; what
   * belongs on this line is the answer to "is it moving", not the whole record.
   * The full output is the panel's (#238) -- here it is a preview, because a
   * page that pasted a whole agent answer into the timeline would bury the very
   * movement it exists to show.
   */
  $: arrived = (projection?.events ?? []).filter(
    (event) => event.event === "AGENT_COMPLETED" || event.event === "AGENT_FAILED"
  );

  function preview(cursor: string): string | null {
    const output = projection?.agent_outputs_by_cursor.get(cursor);
    if (output === undefined || output.kind === "empty") return null;
    const text = output.kind === "utf8" ? output.value : `${output.byte_count} bytes`;
    return text.length > PREVIEW_CHARACTERS
      ? `${text.slice(0, PREVIEW_CHARACTERS)}…`
      : text;
  }

  let openNodeId: string | null = null;
  let detail: NodeDetail | null = null;
  let failure: string | null = null;

  /**
   * One click asks the server, and the server answers the whole node.
   *
   * The panel deliberately does not assemble itself from the run, the events and
   * the receipts the page already holds: those are three sources for one answer,
   * and the derivation is exactly what the node read exists to end.
   */
  async function openNode(nodeId: string): Promise<void> {
    if (openNodeId === nodeId) {
      closeNode();
      return;
    }
    openNodeId = nodeId;
    detail = null;
    failure = null;
    try {
      const answered = await cockpitApi.getNodeDetail(run.public_run_reference, nodeId);
      if (openNodeId === nodeId) {
        detail = answered;
      }
    } catch (error) {
      if (openNodeId === nodeId) {
        failure = error instanceof Error ? error.message : String(error);
      }
    }
  }

  function closeNode(): void {
    openNodeId = null;
    detail = null;
    failure = null;
  }

  /**
   * The rail is rendered, not derived.
   *
   * A V1 or V2 run is folded here in the browser because its events arrive one
   * at a time and the page has to keep up. A V3 run carries the rail the server
   * already walked, along the one edge its author declared, so recomputing it
   * here would be a second owner of the same order -- and the browser's copy has
   * no way to walk a V3 graph anyway, because the wire carries no nodes for one.
   */
  $: rail = run.node_rail;
</script>

<section class="v3-run" aria-labelledby="v3-run-title">
  <header class="run-header">
    <div>
      <p class="eyebrow">Durable run</p>
      <h1 id="v3-run-title">Run {run.run_id}</h1>
    </div>
    <p class="standing" aria-label="Where this run stands">
      <StateMark state={run.state === "COMPLETED" ? "succeeded" : "working"} />
      <span class="following">
        {#if projection === null || projection.connection === "connecting"}
          Connecting…
        {:else if projection.connection === "complete"}
          Ended
        {:else if projection.connection === "failed"}
          Disconnected
        {:else}
          Following live
        {/if}
      </span>
    </p>
  </header>

  <ol class="rail">
    {#each rail as entry (entry.node_id)}
      <li class="rail-entry" class:current={entry.node_id === run.current_node_id}>
        <button
          type="button"
          class="node-button"
          aria-expanded={openNodeId === entry.node_id}
          on:click={() => void openNode(entry.node_id)}
        >
          <StateMark state={entry.state} />
          <span class="node-id">{entry.node_id}</span>
        </button>
      </li>
    {/each}
  </ol>

  {#if openNodeId !== null}
    {#if failure !== null}
      <ProblemNotice title="This node could not be read" message={failure} />
    {:else if detail !== null}
      <NodeDetailPanel {detail} onClose={closeNode} />
    {:else}
      <p class="muted">Reading {openNodeId}…</p>
    {/if}
  {/if}

  <section class="stream">
    <p class="eyebrow">As it happened</p>
    {#if arrived.length === 0}
      <p class="muted">No node has finished its turn yet.</p>
    {:else}
      <ol class="events" aria-label="Events as they arrive">
        {#each arrived as event (event.cursor)}
          <li class="event">
            <StateMark
              state={event.event === "AGENT_COMPLETED" ? "succeeded" : "failed"}
            />
            <span class="node-id">{event.node_id}</span>
            {#if preview(event.cursor) !== null}
              <code class="preview">{preview(event.cursor)}</code>
            {/if}
          </li>
        {/each}
      </ol>
    {/if}
  </section>

  <dl class="facts">
    <dt>Terminal hash</dt>
    <dd>
      {#if run.terminal_hash === null}
        <span class="muted">not yet</span>
      {:else}
        <code>{run.terminal_hash}</code>
      {/if}
    </dd>
    <dt>Run configuration</dt>
    <dd><code>{run.run_configuration_revision_hash}</code></dd>
    <dt>Workflow revision</dt>
    <dd><code>{run.workflow_revision_hash}</code></dd>
  </dl>
</section>

<style>
  .v3-run { display: grid; gap: 1rem; }
  .run-header { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: baseline; justify-content: space-between; }
  .standing { display: flex; align-items: center; gap: 0.75rem; margin: 0; }
  .following { opacity: 0.75; }
  .stream { display: grid; gap: 0.4rem; margin-top: 0.5rem; }
  .events { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.4rem; }
  .event { display: flex; align-items: baseline; gap: 0.6rem; }
  .preview { opacity: 0.8; overflow-wrap: anywhere; }
  .rail { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.4rem; }
  .rail-entry { display: flex; align-items: center; gap: 0.6rem; padding: 0.4rem 0.6rem; border-radius: 0.4rem; }
  .rail-entry.current { background: color-mix(in srgb, currentColor 8%, transparent); }
  .node-button { display: flex; align-items: center; gap: 0.6rem; width: 100%; border: 0; background: transparent; padding: 0; font: inherit; color: inherit; cursor: pointer; text-align: left; }
  .node-id { font-weight: 600; }
  .facts { display: grid; grid-template-columns: auto 1fr; gap: 0.3rem 1rem; margin: 0; }
  .facts dd { margin: 0; overflow-wrap: anywhere; }
  .muted { opacity: 0.7; }
</style>
