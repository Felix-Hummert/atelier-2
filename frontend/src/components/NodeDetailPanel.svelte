<script lang="ts">
  import type { NodeDetail } from "../api/client";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import {
    emptyOutputCopy,
    emptyPromptCopy,
    emptyWhoCopy,
    runPageCopy,
    usageMissingCopy
  } from "../lib/runPageCopy";
  import InfoHint from "./InfoHint.svelte";
  import ProofAnchor from "./ProofAnchor.svelte";
  import StateMark from "./StateMark.svelte";
  import When from "./When.svelte";

  export let detail: NodeDetail;
  export let onClose: () => void;

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
    <div>
      <p class="eyebrow">Node</p>
      <h2 id="node-panel-title">{detail.node_id}</h2>
    </div>
    <div class="standing">
      <StateMark state={detail.state} />
      <button type="button" class="close" on:click={onClose} aria-label="Close node detail">
        ×
      </button>
    </div>
  </header>

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

  <section aria-labelledby="node-panel-prompt">
    <h3 id="node-panel-prompt">{wrapDisplayCopy(runPageCopy.prompt)}</h3>
    {#if detail.job_base64 === null}
      <p class="muted">{wrapDisplayCopy(emptyPromptCopy(detail.state))}</p>
    {:else}
      <pre class="exact">{decoded(detail.job_base64)}</pre>
      {#if detail.job_hash !== null}
        <p class="hash">
          <ProofAnchor
            label={wrapDisplayCopy(runPageCopy.promptHash)}
            seals={runPageCopy.sealsPrompt}
            value={detail.job_hash}
            compact={true}
          />
        </p>
      {/if}
    {/if}
  </section>

  <section aria-labelledby="node-panel-output">
    <h3 id="node-panel-output">{wrapDisplayCopy(runPageCopy.output)}</h3>
    {#if detail.answer === null}
      <p class="muted">{wrapDisplayCopy(emptyOutputCopy(detail.state))}</p>
    {:else}
      <pre class="exact">{decoded(detail.answer.value_base64)}</pre>
      <p class="hash">
        <ProofAnchor
          label={wrapDisplayCopy(runPageCopy.outputHash)}
          seals={runPageCopy.sealsOutput}
          value={detail.answer.value_hash}
          compact={true}
        />
      </p>
    {/if}
  </section>

  <section aria-labelledby="node-panel-who">
    <h3 id="node-panel-who">{wrapDisplayCopy(runPageCopy.who)}</h3>
    {#if detail.provenance === null}
      <p class="muted">{wrapDisplayCopy(emptyWhoCopy(detail.state))}</p>
    {:else}
      <p class="who">
        {detail.provenance.role} · {detail.provenance.provider_id} ·
        {detail.provenance.model} · {detail.provenance.executor_revision}
      </p>
      <p class="hash">
        <ProofAnchor
          label={wrapDisplayCopy(runPageCopy.receiptHash)}
          seals={runPageCopy.sealsReceipt}
          value={detail.provenance.receipt_hash}
          compact={true}
        />
      </p>
    {/if}
    {#if detail.started_at}
      <p class="when-ran">
        {wrapDisplayCopy(runPageCopy.duration)}
        <When startedAt={detail.started_at} endedAt={detail.ended_at ?? null} kind="duration" />
      </p>
    {/if}
    <p class="not-recorded">
      {wrapDisplayCopy(runPageCopy.usage)}
      <InfoHint
        label="Why usage is missing"
        exact="No receipt records usage, so this panel cannot say what the attempt cost. Time is recorded beside the attempt, not on the receipt."
      />
      <span class="muted">{wrapDisplayCopy(usageMissingCopy(detail.state))}</span>
    </p>
  </section>
</aside>

<style>
  .node-panel { display: grid; gap: 0.9rem; padding: 1rem; border-radius: 0.5rem; background: color-mix(in srgb, currentColor 5%, transparent); }
  .node-panel header { display: flex; align-items: baseline; justify-content: space-between; gap: 0.75rem; }
  .standing { display: flex; align-items: center; gap: 0.75rem; }
  .close { border: 0; background: transparent; font-size: 1.25rem; line-height: 1; cursor: pointer; color: inherit; }
  h2 { margin: 0; }
  h3 { margin: 0 0 0.3rem; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.04em; opacity: 0.75; }
  .refusal { margin: 0; padding: 0.6rem 0.75rem; border-radius: 0.4rem; border-left: 4px solid var(--warning); background: color-mix(in srgb, var(--warning) 12%, transparent); color: var(--warning); font-weight: 500; }
  .waiting { margin: 0; padding: 0.6rem 0.75rem; border-radius: 0.4rem; opacity: 0.75; }
  .exact { margin: 0; padding: 0.6rem; border-radius: 0.4rem; background: color-mix(in srgb, currentColor 7%, transparent); white-space: pre-wrap; overflow-wrap: anywhere; }
  .hash { margin: 0.3rem 0 0; display: flex; align-items: center; gap: 0.3rem; flex-wrap: wrap; overflow-wrap: anywhere; font-size: 0.85rem; opacity: 0.8; }
  .who { margin: 0; }
  .not-recorded { margin: 0.5rem 0 0; display: flex; align-items: center; gap: 0.3rem; font-size: 0.9rem; }
  .muted { opacity: 0.7; }
</style>
