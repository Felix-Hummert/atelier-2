<script lang="ts">
  import { onMount } from "svelte";

  import type { AnyRun, CockpitApi } from "../api/client";
  import InboxRow from "../components/InboxRow.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import ProjectCard from "../components/ProjectCard.svelte";
  import { readEveryRun } from "../lib/runPages";
  import { confirmResource, startLoading, type RetainedResource } from "../lib/runProjection";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;

  type StudioHome = {
    running: AnyRun[];
    waiting: AnyRun[];
    landed: number;
  };

  let home: RetainedResource<StudioHome> = { confirmed: null, request: { state: "idle" } };
  let failureMessage: string | null = null;

  onMount(load);

  async function load(): Promise<void> {
    home = startLoading(home);
    failureMessage = null;
    try {
      const [started, waitingInput, waitingReconciliation, completed] = await Promise.all([
        readEveryRun((after) => cockpitApi.listRuns(after, "STARTED")),
        readEveryRun((after) => cockpitApi.listRuns(after, "WAITING_INPUT")),
        readEveryRun((after) => cockpitApi.listRuns(after, "WAITING_RECONCILIATION")),
        readEveryRun((after) => cockpitApi.listRuns(after, "COMPLETED"))
      ]);
      const unread = [started, waitingInput, waitingReconciliation, completed]
        .filter((reading) => !reading.complete)
        .map((reading) => ("unreadable" in reading ? reading.unreadable : ""))
        .filter((text) => text.length > 0);
      home = confirmResource(home, {
        running: started.runs,
        waiting: [...waitingInput.runs, ...waitingReconciliation.runs],
        landed: completed.runs.length
      });
      if (unread.length > 0) {
        failureMessage = `Some of this workshop could not be read, so what is below is incomplete: ${unread.join("; ")}.`;
      }
    } catch (error) {
      failureMessage = error instanceof Error ? error.message : "The workshop could not be read.";
      home = { ...home, request: { state: "idle" } };
    }
  }

  $: snapshot = home.confirmed;
  $: empty = snapshot !== null && snapshot.running.length === 0 && snapshot.waiting.length === 0 && snapshot.landed === 0;
</script>

<section class="studio-home" aria-labelledby="studio-title">
  <section class="studio-chat" aria-labelledby="chat-title">
    <div class="studio-chat-head">
      <h2 id="chat-title">Chat</h2>
      <p>about everything — when the conductor exists</p>
    </div>
    <p>
      The conductor is not built yet
      (<a href="https://github.com/FlexOr2/atelier-2/issues/7">#7</a>).
      When it is, it answers here — and the same door stands on the project and on the run.
    </p>
  </section>

  <div class="studio-board">
    <header class="page-header">
      <div>
        <p class="eyebrow">Atelier</p>
        <h1 id="studio-title">Studio</h1>
      </div>
      {#if snapshot !== null && !empty}
        <a class="button primary" href="/atelier/new" onclick={(event) => { event.preventDefault(); navigate("/atelier/new"); }}>Start</a>
      {/if}
    </header>

    {#if failureMessage !== null}
      <ProblemNotice message={failureMessage} />
    {/if}

    {#if snapshot !== null}
      <InboxRow runs={snapshot.waiting} {navigate} />

      {#if empty}
        <div class="empty">
          <h2>Nothing is running</h2>
          <p>A workflow becomes a run, and a run is what this workshop shows.</p>
          <a class="button primary" href="/atelier/new" onclick={(event) => { event.preventDefault(); navigate("/atelier/new"); }}>Start a run</a>
        </div>
      {:else}
        <h2 class="section-title">Projects</h2>
        <ProjectCard
          running={snapshot.running.length}
          waiting={snapshot.waiting.length}
          landed={snapshot.landed}
          {navigate}
        />
      {/if}
    {:else if home.request.state === "loading"}
      <p class="status" role="status">Looking…</p>
    {/if}
  </div>
</section>
