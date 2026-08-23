<script lang="ts" context="module">
  /**
   * The five tabs a node carries (mockup v5 §03), in the order it draws them.
   *
   * A node keeps its whole history: what it produced, what it read, what it
   * was asked, what it printed while it ran, and what proves all of that. The
   * tabs exist even where the house cannot fill them yet — a tab that says
   * plainly what is missing and who owns it is the honest shape; a tab that
   * is silently absent is not.
   */
  export const NODE_TABS = ["result", "input", "prompt", "log", "evidence"] as const;

  export type NodeTab = (typeof NODE_TABS)[number];

  /** Everything about this run that only the Evidence tab is allowed to show. */
  export type RunEvidence = {
    runId: string;
    workflowRevisionHash: string;
    runConfigurationRevisionHash: string;
    terminalHash: string | null;
  };
</script>

<script lang="ts">
  import type { NodeDetail, RunV3 } from "../api/client";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import {
    emptyOutputCopy,
    emptyPromptCopy,
    emptyWhoCopy,
    notRecordedCopy,
    runPageCopy
  } from "../lib/runPageCopy";
  import { runHeaderCopy } from "../lib/runPages";
  import { whenFacts } from "../lib/runProjection";
  import { stateLabels } from "./StateMark.svelte";
  import InfoHint from "./InfoHint.svelte";
  import ProofAnchor from "./ProofAnchor.svelte";

  export let detail: NodeDetail;
  export let onClose: () => void;
  /** The earlier nodes this one reads, as the published document declares them. */
  export let readsFrom: readonly string[] = [];
  export let runEvidence: RunEvidence;
  /**
   * The rail's own word for a retried attempt on this node, or null where the
   * rail names none -- the same fact `WorkflowGraphDrawing` already reads off
   * the run, never a second count this panel keeps for itself.
   */
  export let railAttempt: RunV3["node_rail"][number]["attempt"] = null;

  /**
   * The facts line that replaces the "Done" chip (operator ruling 23.08.):
   * state word, then started/ended/duration exactly as the run head already
   * says them, then the attempt ordinal -- but only once there has been more
   * than one, so a node's first and only attempt says nothing extra. A loop's
   * round number belongs beside it and is a named gap: the read API carries
   * no round datum yet (`RunResourceV3` has none), so this line never guesses
   * one.
   */
  $: nodeFacts = whenFacts(detail.started_at ?? null, detail.ended_at ?? null, new Date());
  $: nodeFactLine = [
    wrapDisplayCopy(stateLabels[detail.state]),
    nodeFacts.startedExact === null
      ? null
      : `${wrapDisplayCopy(runPageCopy.started)} ${nodeFacts.startedExact}`,
    nodeFacts.endedExact === null
      ? null
      : `${wrapDisplayCopy(runPageCopy.ended)} ${nodeFacts.endedExact}`,
    nodeFacts.durationWords === null
      ? null
      : `${wrapDisplayCopy(runPageCopy.duration)} ${nodeFacts.durationWords}`,
    railAttempt !== null && railAttempt.ordinal > 1
      ? `${wrapDisplayCopy(runPageCopy.attempt)} ${railAttempt.ordinal}`
      : null
  ]
    .filter((part): part is string => part !== null)
    .join(" · ");

  const tabLabels: Record<NodeTab, string> = {
    result: runPageCopy.tabResult,
    input: runPageCopy.tabInput,
    prompt: runPageCopy.tabPrompt,
    log: runPageCopy.tabLog,
    evidence: runPageCopy.tabEvidence
  };

  /**
   * Which tab a node opens on, decided by what it is doing — never "the first
   * one" (operator ruling 23.08.).
   *
   * A node that produced something opens on that. A node that stopped opens on
   * Result too, because that is where the refusal that stopped it stands — the
   * mockup opens a failure on its log, and the log does not exist yet (#104),
   * so opening there would greet the operator with an empty tab instead of the
   * reason. Anything still ahead of its work opens on what it was asked.
   */
  const OPENING_TAB: Record<NodeDetail["state"], NodeTab> = {
    succeeded: "result",
    failed: "result",
    cancelled: "result",
    interrupted: "result",
    needs_you: "prompt",
    working: "prompt",
    queued: "prompt"
  };

  let openedNodeId: string | null = null;
  let tab: NodeTab = "result";

  $: if (openedNodeId !== detail.node_id) {
    openedNodeId = detail.node_id;
    tab = OPENING_TAB[detail.state];
  }

  /**
   * Three situations this panel must never blur into each other.
   *
   * **Refused** is a judgement: something read this node's material and said no,
   * and the run stops here. **Waiting** is absence: nothing has judged anything,
   * the work this node reads simply has not arrived. **Ran** is the ordinary
   * case. The server separates the first two at its own owner -- a refusal is
   * only ever a schema owner's words -- and a panel that showed a waiting node
   * as refused would report a run that has not started as one that stopped.
   *
   * A fourth situation never reaches this component: a store disagreeing with
   * itself answers as a problem, and the page shows that instead of this panel.
   */
  $: situation =
    detail.refusal !== null
      ? "refused"
      : detail.answer === null && detail.job_base64 === null
        ? "waiting"
        : "ran";

  /**
   * A wait node the operator already answered, whose answer this reader still
   * cannot see -- #511's gap, not an empty node.
   *
   * A wait node never carries a receipt (nothing ran it) and never carries a
   * completion payload (its answer lands through a different door than an
   * agent's), so a wait node's `provenance` and `answer` are null for its
   * whole life. An agent node that reached `succeeded` always carries both --
   * anything else is the corrupt-store situation this panel never receives.
   * So this exact shape, once a node has ended successfully, names a wait
   * gate the operator already closed rather than a node that wrote nothing.
   */
  $: endedWaitAnswerGap =
    detail.state === "succeeded" && detail.answer === null && detail.provenance === null;

  /**
   * The bytes are decoded here and nowhere else.
   *
   * The wire carries base64 so arbitrary provider output never passes through a
   * UTF-8 decode on its way out of the store. A reader wants to read it, so the
   * decode happens at the last possible moment -- and the hash beside it is the
   * server's, not one this page computed, so what is shown can be checked
   * against the receipt rather than trusted.
   */
  function decoded(base64: string): string {
    return new TextDecoder().decode(
      Uint8Array.from(atob(base64), (character) => character.charCodeAt(0))
    );
  }
</script>

<aside class="node-panel" aria-labelledby="node-panel-title">
  <header>
    <h2 id="node-panel-title">{detail.node_id}</h2>
    <button type="button" class="close" on:click={onClose} aria-label="Close node detail">
      ×
    </button>
  </header>
  <p class="node-facts">{nodeFactLine}</p>

  <div class="node-tabs" role="tablist" aria-label={wrapDisplayCopy(runPageCopy.tabsLabel)}>
    {#each NODE_TABS as candidate (candidate)}
      <button
        type="button"
        role="tab"
        id={`node-tab-${candidate}`}
        class="node-tab"
        class:on={tab === candidate}
        aria-selected={tab === candidate}
        aria-controls={`node-tabpanel-${candidate}`}
        on:click={() => { tab = candidate; }}
      >{wrapDisplayCopy(tabLabels[candidate])}</button>
    {/each}
  </div>

  <div
    class="node-tabpanel"
    role="tabpanel"
    id={`node-tabpanel-${tab}`}
    aria-labelledby={`node-tab-${tab}`}
  >
    {#if tab === "result"}
      {#if situation === "refused"}
        <p class="refusal" role="alert">
          <strong>Stopped here:</strong>
          {detail.refusal}
        </p>
      {:else if situation === "waiting"}
        <p class="waiting" role="status">
          Waiting for the work before it. Nothing has been refused.
        </p>
      {/if}
      {#if detail.answer === null}
        {#if endedWaitAnswerGap}
          <p class="wait-answer-gap">
            {wrapDisplayCopy(runPageCopy.waitAnswerNotReadable)}
            <span class="result-source">{runPageCopy.waitAnswerNotReadableSource}</span>
          </p>
        {:else}
          <p class="muted">{wrapDisplayCopy(emptyOutputCopy(detail.state))}</p>
        {/if}
      {:else}
        <pre class="exact">{decoded(detail.answer.value_base64)}</pre>
      {/if}
    {:else if tab === "input"}
      {#if readsFrom.length === 0}
        <p class="muted">{wrapDisplayCopy(runPageCopy.inputNone)}</p>
      {:else}
        <p class="reads-from">
          <span class="reads-from-label">{wrapDisplayCopy(runPageCopy.inputReads)}</span>
          {#each readsFrom as source (source)}
            <span class="reads-from-node">{source}</span>
          {/each}
        </p>
        <p class="muted">{wrapDisplayCopy(runPageCopy.inputElsewhere)}</p>
      {/if}
    {:else if tab === "prompt"}
      {#if detail.job_base64 === null}
        <p class="muted">{wrapDisplayCopy(emptyPromptCopy(detail.state))}</p>
      {:else}
        <pre class="exact">{decoded(detail.job_base64)}</pre>
      {/if}
    {:else if tab === "log"}
      <p class="muted">{wrapDisplayCopy(runPageCopy.processLogInLease)}</p>
      <p class="muted">{wrapDisplayCopy(runPageCopy.logAbsent)}</p>
    {:else}
      <section aria-labelledby="node-panel-who">
        <h3 id="node-panel-who">{wrapDisplayCopy(runPageCopy.who)}</h3>
        {#if detail.provenance === null}
          <p class="muted">{wrapDisplayCopy(emptyWhoCopy(detail.state))}</p>
        {:else}
          <p class="who">
            {detail.provenance.role} · {detail.provenance.provider_id} ·
            {detail.provenance.executor_revision}
          </p>
          <p class="who-fact">
            {wrapDisplayCopy(runPageCopy.declaredModel)}
            <span>{detail.provenance.model}</span>
          </p>
        {/if}
        <p class="who-fact">
          {wrapDisplayCopy(runPageCopy.resolvedModel)}
          <InfoHint
            label={runPageCopy.resolvedModelMissingWhy}
            exact={runPageCopy.resolvedModelMissingExact}
          />
          <span class="muted">{wrapDisplayCopy(notRecordedCopy(detail.state))}</span>
        </p>
        <p class="who-fact">
          {wrapDisplayCopy(runPageCopy.usage)}
          <InfoHint label={runPageCopy.usageMissingWhy} exact={runPageCopy.usageMissingExact} />
          <span class="muted">{wrapDisplayCopy(notRecordedCopy(detail.state))}</span>
        </p>
      </section>

      <section class="run-evidence-list" aria-labelledby="node-panel-run-evidence">
        <h3 id="node-panel-run-evidence">{wrapDisplayCopy(runPageCopy.evidenceRun)}</h3>
        {#if detail.provenance !== null}
          <ProofAnchor
            label={wrapDisplayCopy(runPageCopy.receiptHash)}
            seals={runPageCopy.sealsReceipt}
            value={detail.provenance.receipt_hash}
          />
        {/if}
        {#if detail.job_hash !== null}
          <ProofAnchor
            label={wrapDisplayCopy(runPageCopy.promptHash)}
            seals={runPageCopy.sealsPrompt}
            value={detail.job_hash}
          />
        {/if}
        {#if detail.answer !== null}
          <ProofAnchor
            label={wrapDisplayCopy(runPageCopy.outputHash)}
            seals={runPageCopy.sealsOutput}
            value={detail.answer.value_hash}
          />
        {/if}
        <ProofAnchor
          label={wrapDisplayCopy(runHeaderCopy.runIdLabel)}
          seals={runHeaderCopy.sealsRunId}
          value={runEvidence.runId}
        />
        <ProofAnchor
          label={wrapDisplayCopy(runPageCopy.workflowRevision)}
          seals={runPageCopy.sealsWorkflow}
          value={runEvidence.workflowRevisionHash}
        />
        <ProofAnchor
          label={wrapDisplayCopy(runPageCopy.runConfiguration)}
          seals={runPageCopy.sealsConfiguration}
          value={runEvidence.runConfigurationRevisionHash}
        />
        <!-- A run that has not landed has no terminal fingerprint, so it gets no
             row: a label with "not yet" where a value belongs is a placeholder,
             not a fact (operator ruling 23.08.). -->
        {#if runEvidence.terminalHash !== null}
          <ProofAnchor
            label={wrapDisplayCopy(runPageCopy.terminalHash)}
            seals={runPageCopy.sealsTerminal}
            value={runEvidence.terminalHash}
          />
        {/if}
        <p class="muted">{wrapDisplayCopy(runPageCopy.evidenceGap)}</p>
      </section>
    {/if}
  </div>
</aside>

<style>
  .node-panel {
    display: grid;
    gap: var(--space-3);
    padding: var(--space-4);
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    background: var(--panel2);
  }

  .node-panel header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }

  .node-facts {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-variant-numeric: tabular-nums;
  }

  .close {
    border: 0;
    background: transparent;
    font-size: var(--text-lg);
    line-height: 1;
    cursor: pointer;
    color: inherit;
  }

  h2 {
    margin: 0;
    font-size: var(--text-md);
  }

  h3 {
    margin: 0 0 var(--space-1);
    font-size: var(--text-2xs);
    text-transform: uppercase;
    letter-spacing: var(--tracking-label);
    color: var(--ink-dim);
  }

  /* One line that scrolls, never a second line: a tab that wraps below the
     rule reads as a heading for what follows it, not as a tab. */
  .node-tabs {
    display: flex;
    flex-wrap: nowrap;
    gap: var(--space-1);
    overflow-x: auto;
    border-bottom: var(--edge) solid var(--line);
  }

  .node-tab {
    flex: none;
  }

  .node-tab {
    border: 0;
    border-radius: 0;
    padding: var(--space-2) var(--space-3);
    color: var(--ink-dim);
    background: transparent;
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  .node-tab.on {
    color: var(--ink);
    border-bottom: var(--edge-strong) solid var(--accent);
    margin-bottom: calc(var(--edge) * -1);
  }

  .node-tabpanel {
    display: grid;
    gap: var(--space-3);
  }

  .refusal {
    margin: 0;
    padding: var(--space-3) var(--space-4);
    border-radius: var(--r);
    border-left: var(--edge-mark) solid var(--signal-attention-mark);
    background: color-mix(in srgb, var(--signal-attention-mark) var(--wash), var(--panel2));
    color: var(--signal-attention);
    font-weight: var(--weight-medium);
  }

  .waiting {
    margin: 0;
    color: var(--ink-dim);
  }

  .wait-answer-gap {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    color: var(--ink-dim);
  }

  /* The same quiet origin pill the chat transcript uses for a house line that
     names the vision or issue behind it -- one look reads as one owner. */
  .result-source {
    justify-self: start;
    border: var(--edge) solid var(--line);
    border-radius: var(--r-pill);
    padding: 0 var(--space-2);
    color: var(--ink-dim);
    background: var(--chip);
    font-size: var(--text-2xs);
  }

  .exact {
    margin: 0;
    padding: var(--space-3);
    border-radius: var(--r);
    background: var(--chip);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: var(--text-sm);
  }

  section {
    display: grid;
    gap: var(--space-3);
  }

  /*
   * Each fingerprint is its own group -- label, value, and the sentence that
   * names what it seals. The gap between one group's sentence and the next
   * group's label has to read as clearly wider than the gap ProofAnchor
   * already keeps inside a single group, or the sentence reads as belonging
   * to whichever label sits closest rather than to its own value (operator
   * finding 23.08.).
   */
  .run-evidence-list {
    gap: var(--space-5);
  }

  .reads-from {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
  }

  .reads-from-label {
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  .reads-from-node {
    border: var(--edge) solid var(--line);
    border-radius: var(--r);
    padding: 0 var(--space-2);
    background: var(--chip);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  .who {
    margin: 0;
  }

  .who-fact {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--text-sm);
  }

  .muted {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }
</style>
