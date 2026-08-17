<script lang="ts">
  import {
    decodeCanonicalBase64,
    type Run,
    type RunV2,
    type RunEvent,
    type ExecutableWorkflowGraph
  } from "../api/client";
  import { applyInteractionOverlay, isStilled } from "../lib/interactionOverlay";
  import {
    projectNodeRail,
    type AgentOutputProjection,
    type NodeProjection
  } from "../lib/runProjection";
  import InfoHint from "./InfoHint.svelte";
  import StateMark, { stateLabels } from "./StateMark.svelte";

  export let run: Run;
  export let graph: ExecutableWorkflowGraph;
  export let events: readonly RunEvent[];
  export let agentOutputs: ReadonlyMap<string, AgentOutputProjection> = new Map();
  export let openFormNodeIds: ReadonlySet<string> = new Set();

  $: rail = applyInteractionOverlay(projectNodeRail(run, graph, events), openFormNodeIds);

  function attemptLabel(attempt: NonNullable<NodeProjection["attempt"]>): string {
    return attempt.state === null
      ? stateLabels.succeeded.toLowerCase()
      : attempt.state.replaceAll("_", " ").toLowerCase();
  }

  function eventLabel(event: RunEvent): string {
    return event.event.replaceAll("_", " ");
  }

  function agentView(projection: NodeProjection): {
    role: string;
    binding: RunV2["agent_bindings"][number] | null;
    attempt: NodeProjection["attempt"];
  } | null {
    const node = projection.node;
    if (node.type !== "agent" || !("role" in node) || !("workflow_format_version" in run)) return null;
    return {
      role: node.role,
      binding: run.agent_bindings.find((binding) => binding.role === node.role) ?? null,
      attempt: projection.attempt
    };
  }

  type ContextView =
    | { kind: "context"; label: string; bytes: number; hash: string; exact: string }
    | { kind: "agent_output"; hash: string; output: AgentOutputProjection };

  function context(projection: NodeProjection): ContextView | null {
    const event = projection.last_event;
    if (
      projection.node.node_id === run.current_node.node_id &&
      run.waiting.type === "WAITING_RECONCILIATION"
    ) {
      return null;
    }
    if (event === null) return null;
    if (event.event === "AGENT_COMPLETED") {
      if ("workflow_format_version" in event) {
        const output = agentOutputs.get(event.cursor);
        return output === undefined
          ? null
          : { kind: "agent_output", hash: event.output_hash, output };
      }
      return {
        kind: "context",
        label: "Output",
        bytes: new globalThis.TextEncoder().encode(event.output).byteLength,
        hash: event.payload_hash,
        exact: event.output
      };
    }
    if (event.event === "ACTION_RECONCILIATION_REQUIRED") {
      return encodedContext("Request", event.request_base64, event.request_hash);
    }
    if (event.event === "ACTION_RECONCILIATION_RESOLVED" || event.event === "ACTION_COMPLETED") {
      return encodedContext("Result", event.receipt.result_base64, event.receipt.result_hash);
    }
    if (event.event === "WAIT_ANSWERED") {
      // A version 3 wait admits whatever its declared schema admits, so its
      // answer travels base64 like every other opaque value; the older formats
      // answer with the decimal text their node's `answer_type` names, and that
      // text is what a reader should see rather than an encoding of it.
      return "answer_base64" in event
        ? encodedContext("Answer", event.answer_base64, event.answer_hash)
        : {
            kind: "context",
            label: "Answer",
            bytes: new globalThis.TextEncoder().encode(event.answer).byteLength,
            hash: event.answer_hash,
            exact: event.answer
          };
    }
    if (event.event === "SUBWORKFLOW_COMPLETED") {
      const exact = String(event.result);
      return {
        kind: "context",
        label: "Result",
        bytes: new globalThis.TextEncoder().encode(exact).byteLength,
        hash: event.result_hash,
        exact
      };
    }
    return null;
  }

  function encodedContext(label: string, encoded: string, hash: string) {
    return {
      kind: "context" as const,
      label,
      bytes: decodeCanonicalBase64(encoded)?.byteLength ?? 0,
      hash,
      exact: encoded
    };
  }
</script>

<section class="node-rail" aria-labelledby="node-rail-title">
  <h2 id="node-rail-title">Workflow</h2>
  <ol>
    {#each rail as projection (projection.node.node_id)}
      {@const value = context(projection)}
      {@const agent = agentView(projection)}
      <li>
        <article
          class="node-card node-{projection.state}"
          aria-label={`${projection.node.node_id} — ${stateLabels[projection.state]}`}
        >
          <header class="node-header">
            <span class="node-kind">{projection.node.type}</span>
            <StateMark
              state={projection.state}
              animated={!isStilled(projection.node.node_id, openFormNodeIds)}
            />
          </header>
          <h3>{projection.node.node_id}</h3>
          {#if projection.node.type === "agent"}
            <p class="work-item"><span>Work item</span><strong>{projection.node.job}</strong></p>
          {/if}
          {#if agent !== null}
            <p class="agent-binding">
              <strong>{agent.role}</strong>
              {#if agent.binding === null}<span>Binding missing</span>{:else}<span>{agent.binding.provider_id} · {agent.binding.model}</span><span>{agent.binding.auth_mode === "api_key" ? "API key" : "Subscription"} · {agent.binding.executor_revision}</span>{/if}
            </p>
            {#if agent.attempt !== null}
              <p class="latest-event">
                <span>Attempt {agent.attempt.ordinal}</span>
                <strong>{attemptLabel(agent.attempt)}</strong>
              </p>
            {/if}
          {/if}
          {#if value !== null}
            {#if value.kind === "context"}
              <div class="context-row">
                <span><strong>{value.label}</strong><small>{value.bytes} bytes</small></span>
                <code>{value.hash}</code>
                <InfoHint label={`${value.label} info`} exact={value.exact} />
              </div>
            {:else}
              <section class="verified-output" aria-label="Verified output">
                <div class="context-row">
                  <span><strong>Output</strong><small>{value.output.byte_count} bytes</small></span>
                  <strong>✓ Verified</strong>
                </div>
                <dl class="request-summary">
                  <div><dt>Format</dt><dd>{value.output.kind === "utf8" ? "UTF-8" : value.output.kind === "binary" ? "Binary" : "Empty"}</dd></div>
                  <div><dt>SHA-256</dt><dd><code>{value.hash}</code></dd></div>
                </dl>
                {#if value.output.kind === "binary"}
                  <details><summary class="button">Details</summary><code class="exact-answer">{value.output.value}</code></details>
                {:else}
                  <output class="exact-answer">{value.output.kind === "empty" ? "Empty" : value.output.value}</output>
                {/if}
              </section>
            {/if}
          {/if}
          <p class="latest-event">
            <span>Latest event</span>
            <strong>{projection.last_event === null ? "None" : eventLabel(projection.last_event)}</strong>
          </p>
        </article>
      </li>
    {/each}
  </ol>
</section>
