<script lang="ts">
  import { onMount, tick } from "svelte";

  import { CockpitRequestError, type CockpitApi, type WorkflowRevisionPage } from "../api/client";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import {
    MutationJournal,
    createRunId as makeRunId,
    publicationMutation,
    startMutation,
    type JournalEntry,
    type PublishMutation,
    type StartMutation
  } from "../lib/mutationJournal";
  import { confirmResource, startLoading, type RetainedResource } from "../lib/runProjection";

  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let navigate: (path: string) => void;
  export let createRunId: () => string = makeRunId;

  let revisions: RetainedResource<WorkflowRevisionPage> = {
    confirmed: null,
    request: { state: "idle" }
  };
  let mode: "saved" | "publish" = "saved";
  let exactYaml = "";
  let draft: { revisionHash: string; runId: string } | null = null;
  let failureMessage: string | null = null;
  let publicationOpen = false;
  let publicationTrigger: HTMLButtonElement;
  let publicationDialog: HTMLDivElement;
  let pending: JournalEntry[] = [];
  let busy = false;

  function chooseSaved(revisionHash: string): void {
    draft = { revisionHash, runId: createRunId() };
    failureMessage = null;
  }

  function changeWorkflowSource(): void {
    draft = null;
    failureMessage = null;
  }

  onMount(async () => {
    await Promise.all([loadRevisions(), loadPending()]);
  });

  async function loadRevisions(): Promise<void> {
    revisions = startLoading(revisions);
    try {
      revisions = confirmResource(revisions, await cockpitApi.listWorkflowRevisions());
    } catch (error) {
      showFailure(error, "The workflow list could not be loaded.");
      revisions = { ...revisions, request: { state: "idle" } };
    }
  }

  async function loadPending(): Promise<void> {
    try {
      pending = (await mutationJournal.entries()).filter(
        (entry) => entry.kind === "publish" || entry.kind === "start"
      );
    } catch (error) {
      showFailure(error, "The saved exact requests could not be read.");
    }
  }

  async function reviewPublication(): Promise<void> {
    failureMessage = null;
    if (exactYaml.length === 0) {
      failureMessage = "Enter the exact workflow YAML before publishing.";
      return;
    }
    publicationOpen = true;
    await tick();
    publicationDialog.focus();
  }

  async function closePublication(): Promise<void> {
    publicationOpen = false;
    await tick();
    publicationTrigger?.focus();
  }

  async function confirmPublication(): Promise<void> {
    publicationOpen = false;
    busy = true;
    failureMessage = null;
    let prepared: PublishMutation | null = null;
    try {
      prepared = await publicationMutation(exactYaml);
      await mutationJournal.prepare(prepared);
      await deliverPublication(prepared);
    } catch (error) {
      if (prepared !== null) await recordDeliveryFailure(prepared.mutation_id, error);
      showFailure(error, "The workflow could not be published.");
    } finally {
      busy = false;
      await loadPending();
    }
  }

  async function startDraft(): Promise<void> {
    if (draft === null) return;
    busy = true;
    failureMessage = null;
    const mutation = startMutation(draft.runId, draft.revisionHash);
    let prepared = false;
    try {
      await mutationJournal.prepare(mutation);
      prepared = true;
      await deliverStart(mutation);
    } catch (error) {
      if (prepared) await recordDeliveryFailure(mutation.mutation_id, error);
      showFailure(error, "The run start could not be confirmed.");
    } finally {
      busy = false;
      await loadPending();
    }
  }

  async function retry(entry: JournalEntry): Promise<void> {
    busy = true;
    failureMessage = null;
    try {
      if (entry.kind === "publish") await deliverPublication(entry);
      if (entry.kind === "start") await deliverStart(entry);
    } catch (error) {
      await recordDeliveryFailure(entry.mutation_id, error);
      showFailure(error, "The exact retry could not be confirmed.");
    } finally {
      busy = false;
      await loadPending();
    }
  }

  async function discard(mutationId: string): Promise<void> {
    await mutationJournal.discard(mutationId);
    await loadPending();
  }

  async function deliverPublication(mutation: PublishMutation): Promise<void> {
    const result = await cockpitApi.publish(mutation);
    const resolved = await mutationJournal.resolve(mutation.mutation_id, {
      type: "publication_response",
      status: result.status,
      target: mutation.target,
      request_body_base64: mutation.body_base64,
      revision_hash: result.value.revision_hash,
      document_base64: result.value.document_base64
    });
    if (!resolved) throw new Error("The publication response did not prove the exact request.");
    draft = { revisionHash: result.value.revision_hash, runId: createRunId() };
  }

  async function deliverStart(mutation: StartMutation): Promise<void> {
    const result = await cockpitApi.start(mutation);
    const resolved = await mutationJournal.resolve(mutation.mutation_id, {
      type: "start_response",
      status: result.status,
      target: mutation.target,
      request_body_base64: mutation.body_base64,
      run_id: result.value.run_id,
      public_run_reference: result.value.public_run_reference,
      workflow_revision_hash: result.value.workflow_revision_hash
    });
    if (!resolved) throw new Error("The start response did not prove the exact request.");
    navigate(`/atelier/runs/${result.value.public_run_reference}`);
  }

  async function recordDeliveryFailure(mutationId: string, error: unknown): Promise<void> {
    if (error instanceof CockpitRequestError && error.definitive_failure) {
      await mutationJournal.discard(mutationId);
      return;
    }
    if (await mutationJournal.get(mutationId)) await mutationJournal.markUncertain(mutationId);
  }

  function showFailure(error: unknown, fallback: string): void {
    failureMessage = error instanceof Error ? error.message : fallback;
  }

  function handleEscape(event: KeyboardEvent): void {
    if (publicationOpen && event.key === "Escape") {
      event.preventDefault();
      void closePublication();
    }
  }
</script>

<svelte:window onkeydown={handleEscape} />

<section aria-labelledby="new-title">
  <a class="back-link" href="/atelier/runs" onclick={(event) => { event.preventDefault(); navigate("/atelier/runs"); }}>← Runs</a>
  <p class="eyebrow">New durable work</p>
  <h1 id="new-title">Choose a workflow</h1>

  {#if failureMessage !== null}<ProblemNotice message={failureMessage} />{/if}

  {#if pending.length > 0}
    <section class="pending" aria-labelledby="pending-title">
      <h2 id="pending-title">Exact requests awaiting confirmation</h2>
      {#each pending as entry (entry.mutation_id)}
        <div class="pending-row">
          <span><strong>{entry.kind === "publish" ? "Publication" : "Run start"}</strong><small>{entry.mutation_id}</small></span>
          <span class="actions"><button type="button" disabled={busy} onclick={() => retry(entry)}>Retry</button><button class="quiet" type="button" disabled={busy} onclick={() => discard(entry.mutation_id)}>Discard</button></span>
        </div>
      {/each}
    </section>
  {/if}

  <fieldset class="mode-picker">
    <legend>Workflow source</legend>
    <label><input type="radio" name="source" value="saved" bind:group={mode} onchange={changeWorkflowSource} /> Saved workflow</label>
    <label><input type="radio" name="source" value="publish" bind:group={mode} onchange={changeWorkflowSource} /> Publish YAML</label>
  </fieldset>

  {#if mode === "saved"}
    <fieldset class="revision-picker">
      <legend>Saved workflow</legend>
      {#each revisions.confirmed?.items ?? [] as revision (revision.revision_hash)}
        <label>
          <input type="radio" name="saved-revision" value={revision.revision_hash} onchange={() => chooseSaved(revision.revision_hash)} />
          <code>{revision.revision_hash}</code>
        </label>
      {/each}
      {#if revisions.request.state === "loading"}<p class="status" role="status">Loading saved workflows…</p>{/if}
      {#if revisions.confirmed?.items.length === 0}<p class="muted">No saved workflows yet.</p>{/if}
    </fieldset>
  {:else}
    <div class="field">
      <label for="workflow-yaml">Exact workflow YAML</label>
      <textarea id="workflow-yaml" rows="12" bind:value={exactYaml} spellcheck="false"></textarea>
      <button bind:this={publicationTrigger} type="button" disabled={busy} onclick={reviewPublication}>Review publication</button>
    </div>
  {/if}

  {#if draft !== null}
    <section class="start-card" aria-labelledby="start-title">
      <div><p class="eyebrow">Ready</p><h2 id="start-title">Run ID</h2><code>{draft.runId}</code></div>
      <button class="primary" type="button" disabled={busy} onclick={startDraft}>Start</button>
    </section>
  {/if}
</section>

{#if publicationOpen}
  <div class="modal-backdrop">
    <div bind:this={publicationDialog} class="dialog" role="dialog" aria-modal="true" aria-labelledby="publish-title" tabindex="-1">
      <p class="eyebrow">Exact bytes</p>
      <h2 id="publish-title">Publish this exact workflow?</h2>
      <p>The YAML will be stored exactly as written. The browser does not reinterpret it.</p>
      <div class="dialog-actions">
        <button class="quiet" type="button" onclick={closePublication}>Cancel</button>
        <button class="primary" type="button" onclick={confirmPublication}>Publish</button>
      </div>
    </div>
  </div>
{/if}
