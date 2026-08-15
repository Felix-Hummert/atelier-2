<script lang="ts">
  import {
    decodeCanonicalBase64,
    type Run,
    type RunV2,
    type RunEvent,
    type WorkflowGraph
  } from "../api/client";
  import { projectNodeRail, type NodeProjection } from "../lib/runProjection";
  import InfoHint from "./InfoHint.svelte";
  import StateMark from "./StateMark.svelte";

  export let run: Run;
  export let graph: WorkflowGraph;
  export let events: readonly RunEvent[];
  export let workingHumanNodeIds: ReadonlySet<string> = new Set();

  $: rail = projectNodeRail(run, graph, events, workingHumanNodeIds);

  function stateLabel(node: NodeProjection): string {
    return {
      queued: "Queued",
      working: "Working",
      needs_you: "Needs you",
      done: "Done"
    }[node.state];
  }

  function eventLabel(event: RunEvent): string {
    return event.event.replaceAll("_", " ");
  }

  function agentView(node: NodeProjection["node"]): {
    role: string;
    binding: RunV2["agent_bindings"][number] | null;
    attempt: RunV2["agent_attempts"][number] | null;
  } | null {
    if (node.type !== "agent" || !("role" in node) || !("workflow_format_version" in run)) return null;
    return {
      role: node.role,
      binding: run.agent_bindings.find((binding) => binding.role === node.role) ?? null,
      attempt: node.node_id === run.current_node.node_id ? run.agent_attempts.at(-1) ?? null : null
    };
  }

  function context(projection: NodeProjection): { label: string; bytes: number; hash: string; exact: string } | null {
    const event = projection.last_event;
    if (
      projection.node.node_id === run.current_node.node_id &&
      run.waiting.type === "WAITING_RECONCILIATION"
    ) {
      return null;
    }
    if (event === null) return null;
    if (event.event === "AGENT_COMPLETED") {
      return {
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
      return {
        label: "Answer",
        bytes: new globalThis.TextEncoder().encode(event.answer).byteLength,
        hash: event.answer_hash,
        exact: event.answer
      };
    }
    if (event.event === "SUBWORKFLOW_COMPLETED") {
      const exact = String(event.result);
      return {
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
      {@const agent = agentView(projection.node)}
      <li>
        <article
          class="node-card node-{projection.state}"
          aria-label={`${projection.node.node_id} — ${stateLabel(projection)}`}
        >
          <header class="node-header">
            <span class="node-kind">{projection.node.type}</span>
            <StateMark
              state={projection.state}
              animated={!workingHumanNodeIds.has(projection.node.node_id)}
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
            {#if agent.attempt !== null}<p class="latest-event"><span>Attempt {agent.attempt.attempt_ordinal}</span><strong>{agent.attempt.state.replaceAll("_", " ").toLowerCase()}</strong></p>{/if}
          {/if}
          {#if value !== null}
            <div class="context-row">
              <span><strong>{value.label}</strong><small>{value.bytes} bytes</small></span>
              <code>{value.hash}</code>
              <InfoHint label={`${value.label} info`} exact={value.exact} />
            </div>
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
