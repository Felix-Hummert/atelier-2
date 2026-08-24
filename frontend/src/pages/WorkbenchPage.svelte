<script lang="ts">
  import { onMount, tick } from "svelte";

  import { isRunV3, type AnyRun, type CockpitApi, type RunV3 } from "../api/client";
  import PinnedDecision from "../components/PinnedDecision.svelte";
  import { resolveWorkflowName } from "../lib/boardRows";
  import { currentChatTranscript, sendChatTurn, type ChatMessage } from "../lib/chatTranscript";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import type { MutationJournal } from "../lib/mutationJournal";
  import { readEveryRevision, readEveryRun } from "../lib/runPages";
  import { workbenchPageCopy } from "../lib/workbenchPageCopy";

  /**
   * The Workbench: the composer, and above it a fixed "Needs you" region that
   * pins every open decision until it is answered. The pin is the whole point
   * (issue #580) -- a decision request once got lost in the growing stream, so
   * here it lives in its own non-scrolling region, never in the conversation
   * that scrolls beneath it.
   */
  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let navigate: (path: string) => void;

  type Pin = { run: RunV3; workflowName: string };

  let pins: readonly Pin[] = [];
  let transcript: readonly ChatMessage[] = currentChatTranscript();
  let typed = "";
  let composer: { focus(): void };

  const speakerLabels: Record<ChatMessage["speaker"], string> = {
    you: workbenchPageCopy.youLabel,
    house: workbenchPageCopy.houseLabel
  };

  onMount(() => {
    void loadPins();
  });

  /**
   * Every run stopped for a person, read the same way the Board reads it, so a
   * decision is pinned here the moment it opens -- not only if the operator
   * happened to be watching the Board when it did. A read that could not name a
   * workflow still pins the decision under the run's own id rather than losing
   * it, the same honesty `resolveWorkflowName` already holds to.
   */
  async function loadPins(): Promise<void> {
    const [waiting, revisions] = await Promise.all([
      readEveryRun((after) => cockpitApi.listRuns(after, "WAITING_INPUT")),
      readEveryRevision((after) => cockpitApi.listWorkflowRevisions(after))
    ]);
    const workflowNames = revisions.complete
      ? new Map(revisions.revisions.map((revision) => [revision.workflow_revision_hash, revision.name]))
      : null;
    pins = waiting.runs
      .filter(isRunV3)
      .map((run) => ({ run, workflowName: resolveWorkflowName(run, workflowNames) }));
  }

  /**
   * A decision answered on its pinned card carries its run on: still waiting
   * (an accepted-but-uncertain answer) keeps the pin with the fresher run;
   * anything else has moved past this gate, so the pin is retired. The pin
   * never lingers as a question the run no longer asks.
   */
  function onRunRead(read: AnyRun): void {
    if (isRunV3(read) && read.state === "WAITING_INPUT") {
      pins = pins.map((pin) =>
        pin.run.public_run_reference === read.public_run_reference ? { ...pin, run: read } : pin
      );
      return;
    }
    pins = pins.filter((pin) => pin.run.public_run_reference !== read.public_run_reference);
  }

  async function send(event: Event): Promise<void> {
    event.preventDefault();
    const sent = sendChatTurn(typed);
    if (sent === transcript) return;
    transcript = sent;
    typed = "";
    await tick();
    composer.focus();
  }

  /**
   * Enter sends, Shift+Enter keeps writing -- the shape every composer has, and
   * the reason the field is a textarea: a message to the house is often more
   * than one line.
   */
  function keydown(event: KeyboardEvent): void {
    if (event.key !== "Enter" || event.shiftKey) return;
    void send(event);
  }
</script>

<section class="workbench surface" aria-labelledby="workbench-title">
  <header class="surface-head">
    <h1 id="workbench-title">{wrapDisplayCopy(workbenchPageCopy.title)}</h1>
  </header>

  <section class="needs-you" aria-labelledby="needs-you-title">
    <h2 id="needs-you-title" class="needs-you-title">{wrapDisplayCopy(workbenchPageCopy.needsYouTitle)}</h2>
    {#if pins.length === 0}
      <p class="needs-you-none">{wrapDisplayCopy(workbenchPageCopy.needsYouNone)}</p>
    {:else}
      <ul class="needs-you-list">
        {#each pins as pin (pin.run.public_run_reference)}
          <li>
            <PinnedDecision
              run={pin.run}
              workflowName={pin.workflowName}
              {cockpitApi}
              {mutationJournal}
              {onRunRead}
              {navigate}
            />
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  {#if transcript.length === 0}
    <div class="workbench-empty card empty-state">
      <h2>{wrapDisplayCopy(workbenchPageCopy.emptyTitle)}</h2>
      <p>{wrapDisplayCopy(workbenchPageCopy.emptyDescription)}</p>
    </div>
  {:else}
    <ol class="conversation" aria-label={wrapDisplayCopy(workbenchPageCopy.transcriptLabel)}>
      {#each transcript as message (message.id)}
        <li class="conversation-line conversation-line-{message.speaker}">
          <p class="conversation-message">
            <span class="conversation-speaker">{wrapDisplayCopy(speakerLabels[message.speaker])}</span>
            {message.text}
          </p>
        </li>
      {/each}
    </ol>
  {/if}

  <form class="composer" onsubmit={send}>
    <label class="composer-label" for="workbench-message">
      {wrapDisplayCopy(workbenchPageCopy.composerLabel)}
    </label>
    <div class="composer-row">
      <textarea
        id="workbench-message"
        rows="2"
        bind:value={typed}
        bind:this={composer}
        onkeydown={keydown}
      ></textarea>
      <button class="primary" type="submit">{wrapDisplayCopy(workbenchPageCopy.send)}</button>
    </div>
    <p class="composer-hint">{wrapDisplayCopy(workbenchPageCopy.composerHint)}</p>
  </form>
</section>

<style>
  /* The pinned region and the composer are the two fixtures of the Workbench:
     they hold to the top and bottom of the stage while the conversation scrolls
     between them, so an open decision never leaves the screen (issue #580). The
     stage's own ground shows through, so each fixture wears it to occlude the
     lines sliding under its edge. */
  .needs-you {
    position: sticky;
    top: 0;
    z-index: 1;
    display: grid;
    gap: var(--space-3);
    padding-block: var(--space-3);
    border-bottom: var(--edge) solid var(--line);
    background: var(--ground);
  }

  .needs-you-title {
    margin: 0;
    font-size: var(--text-2xs);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
    color: var(--signal-attention);
  }

  .needs-you-none {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-sm);
  }

  .needs-you-list {
    display: grid;
    gap: var(--space-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .needs-you-list li {
    min-width: 0;
  }

  .conversation {
    display: grid;
    gap: var(--space-3);
    max-width: var(--reading-width);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .conversation-line {
    display: flex;
    min-width: 0;
  }

  .conversation-line-you {
    justify-content: flex-end;
  }

  .conversation-message {
    display: grid;
    gap: var(--space-1);
    max-width: 92%;
    margin: 0;
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-3) var(--space-4);
    background: var(--panel2);
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
  }

  /* Your own line is the paper one shade deeper, mixed from the ground the
     workshop already uses rather than a grey pasted onto a warm house. */
  .conversation-line-you .conversation-message {
    border-color: color-mix(in srgb, var(--ink) 20%, var(--line));
    background: var(--chip);
  }

  .conversation-speaker {
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-strong);
  }

  .composer {
    position: sticky;
    bottom: 0;
    display: grid;
    gap: var(--space-2);
    max-width: var(--reading-width);
    padding-top: var(--space-3);
    border-top: var(--edge) solid var(--line);
    background: var(--ground);
  }

  .composer-label {
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  .composer-row {
    display: flex;
    align-items: flex-end;
    gap: var(--space-3);
  }

  .composer-row textarea {
    flex: 1;
    font-family: var(--sans);
    font-size: var(--text-sm);
  }

  .composer-hint {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }
</style>
