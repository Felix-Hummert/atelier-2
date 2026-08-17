<script lang="ts">
  import { onMount, tick } from "svelte";

  import {
    CockpitRequestError,
    type AuthProfileInput,
    type CockpitApi,
    type RunV2,
    type WorkflowRevisionDetail,
    type WorkflowRevisionPage,
    type WorkflowRevisionSummary
  } from "../api/client";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import {
    MutationJournal,
    createRunId as makeRunId,
    publicationMutation,
    requestedStartAgentBindings,
    startMutation,
    startMutationV2,
    type JournalEntry,
    type PublishMutation,
    type StartMutation
  } from "../lib/mutationJournal";
  import { readEveryRevision } from "../lib/runPages";
  import { confirmResource, startLoading, type RetainedResource } from "../lib/runProjection";

  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let navigate: (path: string) => void;
  export let createRunId: () => string = makeRunId;

  interface BindingDraft {
    role: string;
    profileId: string;
    revisionNumber: string;
    providerId: string;
    authMode: "" | AuthProfileInput["auth_mode"];
    model: string;
    executorRevision: string;
    error: string | null;
  }

  interface RunDraft {
    revision: WorkflowRevisionDetail;
    runId: string;
    bindings: BindingDraft[];
  }

  let revisions: RetainedResource<WorkflowRevisionPage> = {
    confirmed: null,
    request: { state: "idle" }
  };
  let mode: "saved" | "publish" = "saved";
  let exactYaml = "";
  let draft: RunDraft | null = null;
  let failureMessage: string | null = null;
  let publicationOpen = false;
  let publicationTrigger: HTMLButtonElement;
  let publicationDialog: HTMLDivElement;
  let pending: JournalEntry[] = [];
  let operation: "load" | "publish" | "start" | "retry" | null = null;
  $: busy = operation !== null;
  let selectionGeneration = 0;

  /**
   * Whether this cockpit can carry a run of that revision.
   *
   * It asks one thing: can the server execute this document. The picker used to
   * ask a second -- whether this cockpit could draw the run -- and refused every
   * version 3 revision on that ground. The run page reads one now, so the extra
   * condition is gone and this reads what it enforces.
   */
  function cockpitCanShow(revision: WorkflowRevisionSummary): boolean {
    return revision.executable;
  }

  async function chooseSaved(revisionHash: string): Promise<void> {
    const generation = ++selectionGeneration;
    operation = "load";
    failureMessage = null;
    try {
      const revision = await cockpitApi.getWorkflowRevision(revisionHash);
      if (generation === selectionGeneration) prepareDraft(revision);
    } catch (error) {
      if (generation === selectionGeneration) showFailure(error, "The workflow could not be loaded.");
    } finally {
      if (generation === selectionGeneration) operation = null;
    }
  }

  function changeWorkflowSource(): void {
    selectionGeneration += 1;
    operation = null;
    draft = null;
    failureMessage = null;
  }

  onMount(async () => {
    await Promise.all([loadRevisions(), loadPending()]);
  });

  async function loadRevisions(): Promise<void> {
    revisions = startLoading(revisions);
    try {
      const reading = await readEveryRevision((after) => cockpitApi.listWorkflowRevisions(after));
      revisions = confirmResource(revisions, {
        items: reading.revisions,
        next_after_revision_hash: null
      });
      if (!reading.complete) {
        showFailure(
          new Error(`Some saved workflows could not be read: ${reading.unreadable}.`),
          "The workflow list is incomplete."
        );
      }
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
    operation = "publish";
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
      operation = null;
      await loadPending();
    }
  }

  /**
   * Whether a run of this revision is started by binding agent roles.
   *
   * Version 2 and version 3 both are, through the same bound start request; a
   * version 1 document names no roles at all. Asking that rather than asking for
   * a version number is what let version 3 through here: the three places below
   * were each written as "is this version 2", and each was really asking this.
   */
  function bindsAgentRoles(graph: WorkflowRevisionDetail["graph"]): boolean {
    return graph.format_version === 2 || graph.format_version === 3;
  }

  /**
   * Every agent role this revision declares, once each.
   *
   * A version 3 revision answers with them directly; a version 2 one is read out
   * of the nodes it puts on the wire; a version 1 one declares none. This is a
   * different question from `bindsAgentRoles` above and stays its own: a version 2
   * document with no agent node still starts through the bound request, so "which
   * roles" and "which start request" must not be collapsed into one answer.
   */
  function agentRolesOf(graph: WorkflowRevisionDetail["graph"]): string[] {
    if (graph.format_version === 3) return [...graph.agent_roles];
    if (graph.format_version === 2) {
      return [
        ...new Set(
          graph.nodes.filter((node) => node.type === "agent").map((node) => node.role)
        )
      ];
    }
    return [];
  }

  let publishedGraphs: Record<string, WorkflowRevisionDetail["graph"]> = {};
  let revealingHash: string | null = null;

  function publishedNodeCount(graph: WorkflowRevisionDetail["graph"] | undefined): number | null {
    return graph?.format_version === 3 ? graph.node_count : null;
  }

  function publishedNodePreviews(
    graph: WorkflowRevisionDetail["graph"] | undefined
  ): Extract<WorkflowRevisionDetail["graph"], { format_version: 3 }>["node_previews"] | null {
    return graph?.format_version === 3 ? graph.node_previews : null;
  }

  function publishedAgentRoles(graph: WorkflowRevisionDetail["graph"] | undefined): string[] | null {
    return graph?.format_version === 3 ? [...graph.agent_roles] : null;
  }

  function kindMark(kind: "agent" | "deterministic" | "wait" | "subworkflow" | "action"): string {
    if (kind === "agent") return "●";
    if (kind === "wait") return "○";
    if (kind === "action") return "■";
    if (kind === "deterministic") return "◆";
    return "▣";
  }

  function publishedRevisionFacts(
    revision: WorkflowRevisionSummary,
    graph: WorkflowRevisionDetail["graph"] | undefined
  ): string {
    const parts = [`format ${revision.format_version}`];
    const nodeCount = publishedNodeCount(graph);
    const roles = publishedAgentRoles(graph);
    if (nodeCount !== null) parts.push(`${nodeCount} nodes`);
    if (roles !== null) {
      parts.push(`roles: ${roles.length === 0 ? "none" : roles.join(", ")}`);
    }
    if (revision.executable) parts.push("executable");
    else parts.push(`Cannot be started: ${revision.not_executable_reason}`);
    return parts.join(" · ");
  }

  async function revealPublishedGraph(revisionHash: string): Promise<void> {
    if (publishedGraphs[revisionHash] !== undefined) return;
    revealingHash = revisionHash;
    try {
      const revision = await cockpitApi.getWorkflowRevision(revisionHash);
      publishedGraphs = { ...publishedGraphs, [revisionHash]: revision.graph };
    } catch (error) {
      showFailure(error, "The workflow could not be loaded.");
    } finally {
      if (revealingHash === revisionHash) revealingHash = null;
    }
  }

  async function startDraft(): Promise<void> {
    if (draft === null) return;
    const selected = draft;
    let mutation: StartMutation | null = null;
    if (bindsAgentRoles(selected.revision.graph)) {
      if (!validateBindings(selected.bindings)) return;
      operation = "start";
      failureMessage = null;
      const bindings = selected.bindings.map((binding) => ({ ...binding }));
      const publishedBindings = await publishBindings(bindings);
      if (publishedBindings === null) {
        operation = null;
        return;
      }
      mutation = startMutationV2(
        selected.runId,
        selected.revision.revision_hash,
        publishedBindings.map(({ role, agent_configuration_revision_hash }) => ({
          role,
          agent_configuration_revision_hash
        }))
      );
    } else {
      mutation = startMutation(selected.runId, selected.revision.revision_hash);
    }
    operation = "start";
    failureMessage = null;
    let prepared = false;
    try {
      await mutationJournal.prepare(mutation);
      prepared = true;
      await deliverStart(mutation);
    } catch (error) {
      if (prepared) await recordDeliveryFailure(mutation.mutation_id, error);
      showFailure(error, "The run start could not be confirmed.");
    } finally {
      operation = null;
      await loadPending();
    }
  }

  async function retry(entry: JournalEntry): Promise<void> {
    operation = "retry";
    failureMessage = null;
    try {
      if (entry.kind === "publish") await deliverPublication(entry);
      if (entry.kind === "start") await deliverStart(entry);
    } catch (error) {
      await recordDeliveryFailure(entry.mutation_id, error);
      showFailure(error, "The exact retry could not be confirmed.");
    } finally {
      operation = null;
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
    prepareDraft(result.value);
  }

  function prepareDraft(revision: WorkflowRevisionDetail): void {
    const roles = agentRolesOf(revision.graph);
    draft = {
      revision,
      runId: createRunId(),
      bindings: roles.map((role) => ({
        role,
        profileId: "",
        revisionNumber: "",
        providerId: "",
        authMode: "",
        model: "",
        executorRevision: "",
        error: null
      }))
    };
  }

  async function publishBindings(bindings: BindingDraft[]): Promise<RunV2["agent_bindings"] | null> {
    const published: RunV2["agent_bindings"] = [];
    for (const binding of bindings) {
      try {
        const authInput = {
          profile_id: binding.profileId,
          revision_number: Number(binding.revisionNumber),
          provider_id: binding.providerId,
          auth_mode: requireAuthMode(binding.authMode)
        };
        const auth = await cockpitApi.publishAuthProfile(authInput);
        if (!sameFields(auth.value, authInput)) throw new Error("The auth response changed these fields.");
        const configurationInput = {
          model: binding.model,
          auth_profile_revision_hash: auth.value.auth_profile_revision_hash,
          executor_revision: binding.executorRevision
        };
        const configuration = await cockpitApi.publishAgentConfiguration(configurationInput);
        if (!sameFields(configuration.value, {
          ...configurationInput,
          provider_id: binding.providerId,
          auth_mode: binding.authMode
        })) throw new Error("The configuration response changed these fields.");
        published.push({
          role: binding.role,
          agent_configuration_revision_hash: configuration.value.agent_configuration_revision_hash,
          ...auth.value,
          model: binding.model,
          executor_revision: binding.executorRevision
        });
      } catch (error) {
        setBindingError(binding.role, error instanceof Error ? error.message : "Binding failed.");
        return null;
      }
    }
    return published;
  }

  function validateBindings(bindings: BindingDraft[]): boolean {
    let valid = true;
    for (const binding of bindings) {
      const revisionNumber = Number(binding.revisionNumber);
      const complete =
        binding.profileId.length > 0 &&
        /^(?:[1-9][0-9]*)$/.test(binding.revisionNumber) &&
        Number.isSafeInteger(revisionNumber) &&
        binding.providerId.length > 0 &&
        binding.authMode !== "" &&
        binding.model.length > 0 &&
        binding.executorRevision.length > 0;
      binding.error = complete ? null : "Complete every field.";
      valid &&= complete;
    }
    draft = draft === null ? null : { ...draft, bindings: [...bindings] };
    return valid;
  }

  function requireAuthMode(value: BindingDraft["authMode"]): AuthProfileInput["auth_mode"] {
    if (value === "") throw new Error("Auth mode is required.");
    return value;
  }

  function sameFields(actual: object, expected: object): boolean {
    return Object.entries(expected).every(([key, value]) => actual[key as keyof typeof actual] === value);
  }

  function setBindingError(role: string, error: string | null): void {
    if (draft === null) return;
    draft = {
      ...draft,
      bindings: draft.bindings.map((binding) => binding.role === role ? { ...binding, error } : binding)
    };
  }

  async function deliverStart(mutation: StartMutation): Promise<void> {
    const result = await cockpitApi.start(mutation);
    const expectedBindings = requestedStartAgentBindings(mutation);
    const returnedBindings = "workflow_format_version" in result.value
      ? result.value.agent_bindings
      : null;
    if (
      expectedBindings !== null &&
      (returnedBindings === null ||
        returnedBindings.length !== expectedBindings.length ||
        expectedBindings.some((binding) => {
          const returnedBinding = returnedBindings.find((candidate) => candidate.role === binding.role);
          return returnedBinding?.agent_configuration_revision_hash !==
            binding.agent_configuration_revision_hash;
        }))
    ) throw new Error("The start response changed the exact role bindings.");
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
  <a class="back-link" href="/atelier/project" onclick={(event) => { event.preventDefault(); navigate("/atelier/project"); }}>← Project</a>
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
    <label><input type="radio" name="source" value="saved" bind:group={mode} disabled={busy} onchange={changeWorkflowSource} /> Saved workflow</label>
    <label><input type="radio" name="source" value="publish" bind:group={mode} disabled={busy} onchange={changeWorkflowSource} /> Publish YAML</label>
  </fieldset>

  {#if mode === "saved"}
    <fieldset class="revision-picker">
      <legend>Saved workflow</legend>
      {#each revisions.confirmed?.items ?? [] as revision (revision.revision_hash)}
        <label class="revision-option" class:unstartable={!cockpitCanShow(revision)}>
          <input type="radio" name="saved-revision" value={revision.revision_hash} disabled={busy || !cockpitCanShow(revision)} onchange={() => { void chooseSaved(revision.revision_hash); }} />
          <span class="revision-label">
            {#if revision.name === null}
              <code class="revision-hash">{revision.revision_hash}</code>
              <span class="muted">unnamed — format {revision.format_version} declares no name</span>
            {:else}
              <strong class="revision-name">{revision.name}</strong>
              {#if revision.description !== null}<span class="revision-description">{revision.description}</span>{/if}
            {/if}
            {#if !revision.executable}
              <span class="revision-refusal">Cannot be started: {revision.not_executable_reason}</span>
            {/if}
          </span>
        </label>
        {#if revision.name !== null}
          <details class="revision-details">
            <summary aria-label={`Details for ${revision.name}`} onclick={() => { void revealPublishedGraph(revision.revision_hash); }}>Details</summary>
            <p class="revision-facts">
              {publishedRevisionFacts(revision, publishedGraphs[revision.revision_hash])}
              {#if revealingHash === revision.revision_hash} · Loading workflow…{/if}
            </p>
            {#if publishedNodePreviews(publishedGraphs[revision.revision_hash]) !== null}
              <ul class="revision-nodes">
                {#each publishedNodePreviews(publishedGraphs[revision.revision_hash]) ?? [] as preview (preview.id)}
                  <li class="revision-node" data-kind={preview.kind}>
                    <span class="revision-node-mark" aria-hidden="true">{kindMark(preview.kind)}</span>
                    <div>
                      <span class="node-kind">{preview.kind}</span>
                      <strong>{preview.id}</strong>
                      {#if preview.role !== null}<span class="revision-node-role">{preview.role}</span>{/if}
                      {#if preview.instruction_start !== null}
                        <p class="revision-node-instruction">{preview.instruction_start}</p>
                      {/if}
                    </div>
                  </li>
                {/each}
              </ul>
            {/if}
            <code class="revision-hash">{revision.revision_hash}</code>
          </details>
        {/if}
      {/each}
      {#if revisions.request.state === "loading"}<p class="status" role="status">Loading saved workflows…</p>{/if}
      {#if operation === "load"}<p class="status" role="status">Loading workflow…</p>{/if}
      {#if revisions.confirmed?.items.length === 0}<p class="muted">No saved workflows yet.</p>{/if}
    </fieldset>
  {:else}
    <div class="field">
      <label for="workflow-yaml">Exact workflow YAML</label>
      <textarea id="workflow-yaml" rows="12" bind:value={exactYaml} spellcheck="false" disabled={busy}></textarea>
      <button bind:this={publicationTrigger} type="button" disabled={busy} onclick={reviewPublication}>Review publication</button>
    </div>
  {/if}

  {#if operation === "publish"}<p class="status" role="status">Publishing workflow…</p>
  {:else if operation === "retry"}<p class="status" role="status">Retrying exact request…</p>{/if}

  {#if draft !== null}
    {#if bindsAgentRoles(draft.revision.graph) && draft.bindings.length > 0}
      <section class="binding-list" aria-labelledby="binding-list-title">
        <p class="eyebrow">Agent setup</p>
        <h2 id="binding-list-title">Bindings</h2>
        {#each draft.bindings as binding (binding.role)}
          <article class="node-card binding-card" class:node-queued={operation !== "start" && binding.error === null} class:node-working={operation === "start" && binding.error === null} class:node-needs_you={binding.error !== null} aria-label={`Binding ${binding.role}`}>
            <header class="node-header">
              <span class="node-kind">Agent role</span><h3>{binding.role}</h3>
            </header>
            <div class="binding-grid">
              <label>Profile ID<input type="text" bind:value={binding.profileId} oninput={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null} /></label>
              <label>Revision<input type="text" inputmode="numeric" bind:value={binding.revisionNumber} oninput={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null} /></label>
              <label>Provider<input type="text" bind:value={binding.providerId} oninput={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null} /></label>
              <label>Auth mode<select bind:value={binding.authMode} onchange={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null}><option value="">Choose</option><option value="subscription">Subscription</option><option value="api_key">API key</option></select></label>
              <label>Model<input type="text" bind:value={binding.model} oninput={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null} /></label>
              <label>Executor<input type="text" bind:value={binding.executorRevision} oninput={() => setBindingError(binding.role, null)} disabled={busy} aria-invalid={binding.error !== null} /></label>
            </div>
            {#if binding.error !== null}<p class="binding-error" role="alert">{binding.error}</p>{/if}
          </article>
        {/each}
      </section>
    {/if}
    <!-- Only a version 3 revision can be unexecutable: the older formats carry no
         such field, because everything they can express this build runs. -->
    {#if draft.revision.graph.format_version === 3 && !draft.revision.graph.executable}
      <section class="start-card unstartable" aria-labelledby="start-title">
        <div>
          <p class="eyebrow">Published</p>
          <h2 id="start-title">{draft.revision.graph.name}</h2>
          {#if draft.revision.graph.description !== null}<p class="muted">{draft.revision.graph.description}</p>{/if}
          <p class="revision-refusal">Cannot be started: {draft.revision.graph.not_executable_reason}</p>
        </div>
      </section>
    {:else}
      <section class="start-card" aria-labelledby="start-title">
        <div><p class="eyebrow">{operation === "start" ? "Starting" : "Ready"}</p><h2 id="start-title">Run ID</h2><code>{draft.runId}</code></div>
        <button class="primary" type="button" disabled={busy} onclick={startDraft}>Start</button>
      </section>
    {/if}
    {#if operation === "start"}<p class="status" role="status">Starting the exact run…</p>{/if}
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
