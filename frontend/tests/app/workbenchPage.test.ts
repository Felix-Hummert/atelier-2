import type * as SvelteTestingLibrary from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { encodePublicRunReference } from "../../src/api/client";
import type { CockpitApi, RunV3, WorkflowRevisionDetail } from "../../src/api/client";
import { conductorConversationCopy } from "../../src/lib/conductorConversation";
import { conductorChatCopy } from "../../src/lib/conductorChatCopy";
import { railCopy } from "../../src/lib/railCopy";
import { retryLabel } from "../../src/lib/readStateCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import { workbenchQuestions } from "../../src/lib/workbenchQuestions";
import {
  describeWorkbenchControl,
  questionForWorkbenchControl,
  unansweredWorkbenchControls,
  workbenchInteractiveSelector,
  workbenchStageSelector
} from "../support/workbenchControls";
import { FakeRunEventFeed, PAGE_CURSORS } from "../support/cockpitApi";
import { cancellableBlock, notCancellableBlock } from "../support/runV3";
import {
  startedRun,
  waitingInput,
  waitingInputRun,
  waitingReconciliationRun
} from "../support/runV3";

/**
 * The Workbench's conversation is owned by the `chatTranscript` module, not the
 * page component (issue #556), so it survives the page being torn down and
 * rebuilt by in-app rail navigation. That means it also survives from one
 * test to the next unless each test gets a fresh module instance -- exactly
 * what a real reload gives the operator. `vi.resetModules()` plus a fresh
 * dynamic import of testing-library alongside the app keeps every piece
 * bound to the same reloaded Svelte runtime; mixing a freshly reset
 * component with a stale `render` from a different runtime instance fails.
 */
let testingLibrary: typeof SvelteTestingLibrary;
let openChat: (overrides?: Partial<CockpitApi>) => void;
let reportConnectionLost: () => void;
let reportConnectionRestored: () => void;
let restartNoticeCopy: string;

async function bootApp(): Promise<{
  testingLibrary: typeof SvelteTestingLibrary;
  openChat: (overrides?: Partial<CockpitApi>) => void;
  reportConnectionLost: () => void;
  reportConnectionRestored: () => void;
  restartNoticeCopy: string;
}> {
  vi.resetModules();
  const library = await import("@testing-library/svelte");
  const { default: App } = await import("../../src/App.svelte");
  const { MutationJournal } = await import("../../src/lib/mutationJournal");
  const { cockpitApiStub } = await import("../support/cockpitApi");
  // Loaded from the same reset module graph App.svelte binds to, so reporting
  // here reaches the exact store the composer reads (#700).
  const connection = await import("../../src/lib/connectionState");

  return {
    testingLibrary: library,
    openChat: (overrides: Partial<CockpitApi> = {}) =>
      library.render(App, {
        props: {
          cockpitApi: cockpitApiStub(overrides),
          mutationJournal: new MutationJournal(sessionStorage)
        }
      }),
    reportConnectionLost: connection.reportConnectionLost,
    reportConnectionRestored: connection.reportConnectionRestored,
    restartNoticeCopy: connection.restartNoticeCopy
  };
}

beforeEach(async () => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/chat");

  ({ testingLibrary, openChat, reportConnectionLost, reportConnectionRestored, restartNoticeCopy } =
    await bootApp());
});

afterEach(() => testingLibrary.cleanup());

async function say(words: string): Promise<void> {
  const { fireEvent, screen } = testingLibrary;
  await fireEvent.input(screen.getByLabelText(workbenchPageCopy.composerLabel), {
    target: { value: words }
  });
  await fireEvent.click(screen.getByRole("button", { name: workbenchPageCopy.send }));
}

describe("the workbench door", () => {
  it("teaches where work starts today instead of leaving an empty room, with no button duplicating the rail's own door", async () => {
    openChat();
    const { screen } = testingLibrary;

    expect((await screen.findByRole("heading", { name: "Workbench" })).isConnected).toBe(true);
    expect(screen.getByText(workbenchPageCopy.emptyDescription).isConnected).toBe(true);
    // The rail already carries a door to Workflows; the empty state names it
    // in a sentence rather than repeating it as a second button (#579).
    expect(screen.queryByRole("link", { name: "Open Workflows" })).toBeNull();
  });

  it("keeps what was said and answers that nothing was started, naming no board or issue number", async () => {
    openChat();
    const { screen, within } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });

    await say("Finish the preview door");

    const transcript = screen.getByRole("list", { name: workbenchPageCopy.transcriptLabel });
    expect(within(transcript).getByText(/Finish the preview door/).isConnected).toBe(true);
    // No invented answer, no pretence that anything started, and no internal
    // vision or issue number leaked into the operator's own conversation
    // (Adressaten-Regel, operator ruling 23.08.).
    const answer = within(transcript).getByText(workbenchPageCopy.conductorAbsent);
    expect(answer.textContent).not.toMatch(/#\d/);
  });

  it("empties the composer after sending, so the same words cannot be sent twice by accident", async () => {
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });

    await say("start two runs");

    expect(screen.getByLabelText(workbenchPageCopy.composerLabel)).toHaveProperty("value", "");
  });

  it("takes no turn at all for a blank message", async () => {
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });

    await say("   ");

    expect(screen.queryByRole("list", { name: workbenchPageCopy.transcriptLabel })).toBeNull();
    expect(screen.getByText(workbenchPageCopy.emptyTitle).isConnected).toBe(true);
  });

  it("keeps the conversation across a rail change and back, since that is not leaving the page", async () => {
    openChat();
    const { screen, within } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    await say("Finish the preview door");

    // Rail navigation tears down and rebuilds the Workbench page component the
    // same way `{#if route.page === "chat"}` does in App.svelte, while the
    // module that now owns the conversation stays loaded across that swap.
    testingLibrary.cleanup();
    openChat();
    await screen.findByRole("heading", { name: "Workbench" });

    const transcript = screen.getByRole("list", { name: workbenchPageCopy.transcriptLabel });
    expect(within(transcript).getByText(/Finish the preview door/).isConnected).toBe(true);
    expect(
      within(transcript).getByText(workbenchPageCopy.conductorAbsent).isConnected
    ).toBe(true);
  });

  it("starts a fresh, empty conversation after a reload", async () => {
    openChat();
    await testingLibrary.screen.findByRole("heading", { name: "Workbench" });
    await say("Finish the preview door");
    testingLibrary.cleanup();

    // A reload re-executes the whole module graph from scratch: a second
    // reset plus a second fresh boot is that reload, and the conductor's
    // module-owned conversation comes back empty.
    ({ testingLibrary, openChat } = await bootApp());
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });

    expect(screen.queryByRole("list", { name: workbenchPageCopy.transcriptLabel })).toBeNull();
    expect(screen.getByText(workbenchPageCopy.emptyTitle).isConnected).toBe(true);
  });

  it("disables Send and shows the restart line while the connection is lost, not the no-conductor refusal (#700)", async () => {
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    await testingLibrary.fireEvent.input(screen.getByLabelText(workbenchPageCopy.composerLabel), {
      target: { value: "Finish the preview door" }
    });

    reportConnectionLost();
    await testingLibrary.waitFor(() => {
      expect(screen.getByRole("button", { name: workbenchPageCopy.send })).toHaveProperty(
        "disabled",
        true
      );
    });

    // The ear (HEART) names its own state in one sentence; the shell's top
    // banner stays silent on this one room so the fact is said exactly once,
    // never as a page-local echo of the same line (#700).
    expect(screen.getAllByText(restartNoticeCopy)).toHaveLength(1);
    expect(document.querySelector(".composer-hint")?.textContent).toBe(restartNoticeCopy);
    expect(screen.queryByText(workbenchPageCopy.composerHint)).toBeNull();
    // Nothing was sent: the word stays exactly where it was typed.
    expect(screen.getByLabelText(workbenchPageCopy.composerLabel)).toHaveProperty(
      "value",
      "Finish the preview door"
    );
    expect(screen.queryByRole("list", { name: workbenchPageCopy.transcriptLabel })).toBeNull();
  });

  it("re-enables Send and restores the ordinary hint once the connection returns, with no reload", async () => {
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    reportConnectionLost();
    await testingLibrary.waitFor(() => {
      expect(screen.getByRole("button", { name: workbenchPageCopy.send })).toHaveProperty(
        "disabled",
        true
      );
    });

    reportConnectionRestored();

    await testingLibrary.waitFor(() => {
      expect(screen.getByRole("button", { name: workbenchPageCopy.send })).toHaveProperty(
        "disabled",
        false
      );
    });
    expect(screen.queryByText(restartNoticeCopy)).toBeNull();
    // Whichever ordinary hint the composer settles on (which conductor state
    // that is is not this test's question), it is back to something other
    // than the restart line.
    const hint = document.querySelector(".composer-hint");
    expect(hint?.textContent).not.toBe(restartNoticeCopy);
  });
});

/**
 * A connected conductor's own conversation, real reducer and journal
 * included: the shared fixture below is the smallest published loop
 * (`resolveConductorConnection`'s own shape check) any of these three
 * scenarios needs to actually connect.
 */
describe("the workbench conductor conversation", () => {
  const conductorRevisionHash = "9".repeat(64);
  const conductorConfigurationHash = "8".repeat(64);
  const conductorRole = "conductor";
  const conductorProjectReference = "project1";
  const conductorPublicRunReference = encodePublicRunReference(
    "workbench/conductor-conversation"
  );

  function conductorRevisionDetail(): WorkflowRevisionDetail {
    return {
      workflow_revision_hash: conductorRevisionHash,
      document_base64: "YQ==",
      graph: {
        workflow_format_version: 3,
        executable: true,
        not_executable_reason: null,
        node_count: 2,
        agent_roles: [conductorRole],
        orders: [],
        wait_answer_schemas: [
          {
            node_id: "next_message",
            schema: { ref: "message", revision: conductorRevisionHash },
            kind: "string",
            values: null
          }
        ],
        node_previews: [
          { id: "next_message", kind: "wait", role: null, instruction_start: null, depends_on: [] },
          {
            id: "conduct",
            kind: "agent",
            role: conductorRole,
            instruction_start: "Answer the operator",
            depends_on: ["next_message"]
          }
        ],
        loops: [
          {
            id: "conversation",
            member_node_ids: ["next_message", "conduct"],
            maximum_rounds: 3,
            repeat_while: null
          }
        ],
        name: "Conductor",
        description: null
      }
    } as WorkflowRevisionDetail;
  }

  /** Every read `resolveConductorConnection` (conductorEpisode.ts) makes to bind one live conductor. */
  function conductorConnectionOverrides(): Partial<CockpitApi> {
    return {
      getRevisionByName: vi.fn(async () => ({
        display_name: "conductor",
        lineage_id: "7".repeat(64),
        workflow_revision_hash: conductorRevisionHash,
        revision_number: 1
      })),
      getWorkflowRevision: vi.fn(async () => conductorRevisionDetail()),
      listProjects: vi.fn(async () => ({
        items: [{ public_project_reference: conductorProjectReference }]
      })),
      resolveProjectModels: vi.fn(async () => ({
        project_id: "conductor-project",
        public_project_reference: conductorProjectReference,
        workflow_revision_hash: conductorRevisionHash,
        resolutions: [
          {
            role: conductorRole,
            agent_configuration_revision_hash: conductorConfigurationHash,
            source: "chosen-now" as const,
            model_id: "conductor-model",
            declared_difficulty: 1 as const,
            default_difficulty: null,
            uncast_reason: null,
            family_differs_from: null
          }
        ]
      })),
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [
          {
            agent_configuration_revision_hash: conductorConfigurationHash,
            provider_id: "test",
            model: "conductor-model",
            auth_mode: "subscription" as const,
            auth_profile_revision_hash: "6".repeat(64),
            executor_revision: "immediate/v1",
            requested_capability: "headless" as const,
            startable: true,
            structurally_startable: true,
            not_startable_reason: null
          }
        ],
        next_after_revision_hash: null
      }))
    };
  }

  function conductorRunFixture(overrides: Partial<RunV3> = {}): RunV3 {
    return {
      workflow_format_version: 3,
      run_id: "workbench/conductor-conversation",
      workflow_name: "conductor conversation",
      public_run_reference: conductorPublicRunReference,
      workflow_revision_hash: conductorRevisionHash,
      agent_binding_set_hash: "5".repeat(64),
      run_configuration_revision_hash: "4".repeat(64),
      agent_bindings: [],
      orders: [],
      state_version: 1,
      state: "WAITING_INPUT",
      current_node_id: "next_message",
      current_node_execution_id: "3".repeat(64),
      node_rail: [
        { node_id: "next_message", state: "needs_you", attempt: null },
        { node_id: "conduct", state: "queued", attempt: null }
      ],
      cancellation: notCancellableBlock("waiting-for-you"),
      terminal_hash: null,
      latest_event_cursor: null,
      started_at: "2026-08-18T15:00:00Z",
      ended_at: null,
      ...overrides
    };
  }

  /** Only the run this room already knows about, and only for the state it is served under. */
  function listRunsForConductor(run: RunV3 | null): CockpitApi["listRuns"] {
    return vi.fn(async (_after?: string, state?: string) => ({
      items: run !== null && (state === undefined || state === run.state) ? [run] : [],
      next_after: null
    }));
  }

  async function completedAnswerEvent(
    publicRunReference: string,
    workflowRevisionHash: string,
    answer: string,
    sequence: number
  ): Promise<Record<string, unknown>> {
    const output = JSON.stringify({ answer });
    const outputDigest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(output));
    return {
      workflow_format_version: 3,
      // The decoder checks the cursor's own run reference against the event's
      // (client.ts, "event cursor, run reference, and sequence disagree"),
      // so this fixture's cursor must carry the same reference as the run.
      cursor: `event1.${publicRunReference.slice("run1.".length)}.${sequence}`,
      sequence,
      public_run_reference: publicRunReference,
      workflow_revision_hash: workflowRevisionHash,
      node_id: "conduct",
      node_execution_id: "2".repeat(64),
      event_hash: "1".repeat(64),
      node_rail: [
        { node_id: "next_message", state: "succeeded", attempt: null },
        { node_id: "conduct", state: "succeeded", attempt: null }
      ],
      event: "AGENT_COMPLETED",
      output_base64: btoa(output),
      output_hash: [...new Uint8Array(outputDigest)]
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join(""),
      attempt_id: "e".repeat(64),
      attempt_ordinal: 1
    };
  }

  it("carries one link back to the conversation's own run, however many rounds it holds", async () => {
    const feed = new FakeRunEventFeed();
    const run = conductorRunFixture();
    openChat({
      ...conductorConnectionOverrides(),
      listRuns: listRunsForConductor(run),
      getRun: vi.fn(async () => run),
      openRunEvents: feed.open
    });
    const { screen, waitFor } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    await screen.findByText(conductorConversationCopy.composerHint);
    await waitFor(() => expect(feed.handlers).not.toBeNull());

    feed.handlers?.opened();
    feed.handlers?.event(
      JSON.stringify(
        await completedAnswerEvent(run.public_run_reference, run.workflow_revision_hash, "Guten Tag!", 1)
      )
    );

    const conversation = await screen.findByRole("link", {
      name: conductorChatCopy.openEpisode
    });
    expect(conversation.getAttribute("href")).toBe(`/atelier/runs/${run.public_run_reference}`);

    // A second round is a second reply in the same run, so it adds no second
    // way to open that run: the link belongs to the conversation, not to a line.
    feed.handlers?.event(
      JSON.stringify(
        await completedAnswerEvent(
          run.public_run_reference,
          run.workflow_revision_hash,
          "Und noch etwas",
          2
        )
      )
    );
    await screen.findByText("Und noch etwas");
    expect(screen.getAllByRole("link", { name: conductorChatCopy.openEpisode })).toHaveLength(1);
  });

  it("keeps the first round's reply and adds the second, answering the wait each round has open", async () => {
    const feed = new FakeRunEventFeed();
    const waitingFirstRound = conductorRunFixture();
    const waitingSecondRound = conductorRunFixture({
      state_version: 2,
      current_node_execution_id: "a".repeat(64)
    });
    let standing = waitingFirstRound;
    // The durable route answers a wait with 202 and moves the run on its own
    // (verified against `tests/e2e/serve_cockpit.py`), and it fences the answer
    // to the exact execution it was written for -- so this double refuses an
    // answer aimed at a round the run has already left.
    const staleWaitRefusal = "that wait is no longer open";
    const answer = vi.fn<CockpitApi["answer"]>(async (mutation) => {
      if (mutation.expected_node_execution_id !== standing.current_node_execution_id) {
        throw new Error(staleWaitRefusal);
      }
      const accepted = standing;
      standing = waitingSecondRound;
      return { status: 202, value: accepted };
    });
    openChat({
      ...conductorConnectionOverrides(),
      listRuns: listRunsForConductor(waitingFirstRound),
      getRun: vi.fn(async () => standing),
      answer,
      openRunEvents: feed.open
    });
    const { screen, waitFor } = testingLibrary;
    /** Sending ends where the composer takes focus back, ready for the next message. */
    async function sendAndSettle(words: string): Promise<void> {
      const composer = screen.getByLabelText(workbenchPageCopy.composerLabel);
      composer.blur();
      await say(words);
      await waitFor(() => expect(document.activeElement).toBe(composer));
    }

    await screen.findByRole("heading", { name: "Workbench" });
    await screen.findByText(conductorConversationCopy.composerHint);
    await waitFor(() => expect(feed.handlers).not.toBeNull());
    feed.handlers?.opened();

    await sendAndSettle("Erste Nachricht");
    feed.handlers?.event(
      JSON.stringify(
        await completedAnswerEvent(
          waitingFirstRound.public_run_reference,
          waitingFirstRound.workflow_revision_hash,
          "Erste Antwort",
          1
        )
      )
    );
    await screen.findByText("Erste Antwort");

    await waitFor(() =>
      expect(screen.getByRole("button", { name: workbenchPageCopy.send })).toHaveProperty(
        "disabled",
        false
      )
    );
    await sendAndSettle("Zweite Nachricht");

    // The second message went out as its own round's answer, aimed at the wait
    // the run stands in now rather than at the one round 1 already closed.
    // Read from the call and from the room, because the transcript below is fed
    // by the stream either way and would look the same had this answer been
    // refused -- a refusal leaves the composer holding the words back with the
    // refusal on screen.
    expect(answer).toHaveBeenCalledTimes(2);
    expect(answer.mock.calls[1]?.[0].expected_node_execution_id).toBe(
      waitingSecondRound.current_node_execution_id
    );
    expect(screen.getByLabelText(workbenchPageCopy.composerLabel)).toHaveProperty("value", "");
    expect(screen.queryByText(staleWaitRefusal)).toBeNull();

    feed.handlers?.event(
      JSON.stringify(
        await completedAnswerEvent(
          waitingFirstRound.public_run_reference,
          waitingFirstRound.workflow_revision_hash,
          "Zweite Antwort",
          2
        )
      )
    );

    // The second round's reply is added to the transcript, not a replacement
    // of the first: both rounds stand.
    await screen.findByText("Zweite Antwort");
    expect(screen.queryByText("Erste Antwort")).not.toBeNull();
  });

  // Finding 1 (#959 review): a retry of the same open wait with edited text
  // can conflict with its own earlier, differently-worded attempt still in
  // the journal (mutationJournal.ts, "mutation identity already belongs to a
  // different exact request") -- the composer must unlock and keep the words
  // instead of leaving `conductorDeliveryBusy` stuck true forever.
  it("keeps the composer usable and restores the message when the journal refuses a retried wait answer", async () => {
    const run = conductorRunFixture();
    openChat({
      ...conductorConnectionOverrides(),
      listRuns: listRunsForConductor(run),
      getRun: vi.fn(async () => run)
    });
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    await screen.findByText(conductorConversationCopy.composerHint);

    // A previous, uncommitted attempt at this same wait already sits in the
    // journal with different content -- exactly what a retry after a
    // non-definitive failure leaves behind.
    const { MutationJournal, waitMutation } = await import("../../src/lib/mutationJournal");
    const { encodeWaitAnswer } = await import("../../src/lib/waitAnswer");
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(
      await waitMutation(
        run.public_run_reference,
        run.workflow_revision_hash,
        run.current_node_id,
        run.current_node_execution_id,
        // The conductor's message wait is a string schema (#1091): a real
        // earlier attempt would have been journaled verbatim, not JSON-quoted.
        encodeWaitAnswer("an earlier, different attempt", true)
      )
    );

    await say("the retried answer");

    await screen.findByText(/mutation identity already belongs to a different exact request/);
    expect(screen.getByLabelText(workbenchPageCopy.composerLabel)).toHaveProperty(
      "value",
      "the retried answer"
    );
    expect(screen.getByRole("button", { name: workbenchPageCopy.send })).toHaveProperty(
      "disabled",
      false
    );
  });

  // Finding 2 (#959 review): the old guard allowed only WAITING_INPUT and
  // COMPLETED, so a FAILED conversation blocked Send forever and survived a
  // reload via `restoreConductorConversation`. Every terminal state now
  // behaves like COMPLETED: a message after it starts a fresh conversation.
  it("starts a fresh conversation instead of staying stuck after a run that failed, including across a reload", async () => {
    const failedRun = conductorRunFixture({
      state: "FAILED",
      current_node_id: "conduct",
      node_rail: [
        { node_id: "next_message", state: "succeeded", attempt: null },
        { node_id: "conduct", state: "failed", attempt: null }
      ],
      cancellation: notCancellableBlock("already-ended"),
      terminal_hash: "1".repeat(64),
      ended_at: "2026-08-18T15:05:00Z"
    });
    const startedConversation = conductorRunFixture({
      public_run_reference: encodePublicRunReference("workbench/conductor-conversation-2"),
      state_version: 0,
      state: "STARTED",
      current_node_id: "next_message",
      node_rail: [
        { node_id: "next_message", state: "queued", attempt: null },
        { node_id: "conduct", state: "queued", attempt: null }
      ],
      cancellation: cancellableBlock()
    });

    // The remembered run is how a reload finds a conversation again
    // (`restoreConductorConversation`) -- exactly the door through which the
    // deadlock survived a reload before this fix.
    const { rememberConductorRun } = await import("../../src/lib/conductorConversation");
    rememberConductorRun(sessionStorage, failedRun.public_run_reference);

    const start = vi.fn(async () => ({ status: 201, value: startedConversation }));
    const getRun = vi.fn(async (reference: string) =>
      reference === failedRun.public_run_reference ? failedRun : startedConversation
    );
    openChat({
      ...conductorConnectionOverrides(),
      listRuns: listRunsForConductor(null),
      getRun,
      start
    });
    const { screen, waitFor } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    await screen.findByText(conductorConversationCopy.endedHint);
    expect(screen.getByRole("button", { name: workbenchPageCopy.send })).toHaveProperty(
      "disabled",
      false
    );

    await say("Let's try again");

    expect(screen.getByLabelText(workbenchPageCopy.composerLabel)).toHaveProperty("value", "");
    await waitFor(() => expect(start).toHaveBeenCalled());
    // The fresh run is genuinely a new, distinct conversation in flight --
    // not the same failed one pretending to have recovered.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: workbenchPageCopy.send })).toHaveProperty(
        "disabled",
        true
      );
    });
  });
});

/**
 * The room the workshop opens on (ADR 0019 §1). What the Board used to hold
 * lives here now: the decisions that want a person, the runs that are moving,
 * and the one number the rail carries.
 */
describe("the workbench is the room the workshop opens on", () => {
  function listRunsByState(runs: readonly RunV3[]) {
    return vi.fn(async (_after?: string, state?: string) => ({
      items: state === undefined ? [...runs] : runs.filter((run) => run.state === state),
      next_after: null
    }));
  }

  function openRoom(runs: readonly RunV3[] = [], overrides: Partial<CockpitApi> = {}): void {
    window.history.replaceState(null, "", "/atelier");
    openChat({ listRuns: listRunsByState(runs), ...overrides });
  }

  // The identifier stays "the-workshop-opens-in-the-studio" (acceptance/131):
  // the room it names is the Workbench since ADR 0019 retired the Board.
  it("proves(the-workshop-opens-in-the-studio): opens the bare atelier path in the Workbench instead of a list of runs", async () => {
    openRoom();
    const { screen } = testingLibrary;

    expect((await screen.findByRole("heading", { name: "Workbench" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Board" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Runs" })).toBeNull();
    expect(window.location.pathname).toBe("/atelier");
  });

  // Same identifier note (acceptance/131): the inbox is the Workbench's pinned
  // stage and the rows beneath it.
  it("proves(the-inbox-names-every-run-that-waits-for-a-human): names every run in a durable waiting state, across every page the list holds, and leads to each in one click", async () => {
    // "Across everything" is only true while the reading spans the durable
    // pages: a run that waits on the second page is exactly the one a
    // single-page read would lose.
    openRoom([], {
      listRuns: vi.fn(async (after?: string, state?: string) => {
        if (state === "WAITING_INPUT") {
          return after === undefined
            ? {
                items: [waitingInputRun({ public_run_reference: "run1.Yg" })],
                next_after: PAGE_CURSORS[0] ?? null
              }
            : { items: [waitingInputRun({ public_run_reference: "run1.YQ" })], next_after: null };
        }
        if (state === "WAITING_RECONCILIATION") {
          return {
            items: [waitingReconciliationRun({ public_run_reference: "run1.Yw" })],
            next_after: null
          };
        }
        return { items: [], next_after: null };
      })
    });
    const { fireEvent, screen, waitFor, within } = testingLibrary;

    // A waiting decision stands pinned in the Open-decisions region, one card
    // per run; a reconciliation this room cannot answer inline stays a row.
    const pinnedRegion = await screen.findByRole("region", { name: "Open decisions" });
    await waitFor(() => {
      expect(within(pinnedRegion).getAllByRole("listitem")).toHaveLength(2);
    });
    expect(screen.getByText(/Reconcile →/).isConnected).toBe(true);
    expect(screen.queryByText(/Running/)).toBeNull();

    await fireEvent.click(screen.getByText(/Reconcile →/));
    await waitFor(() => expect(window.location.pathname).toBe("/atelier/runs/run1.Yw"));
  });

  it("lays what is moving on the shelf beneath, one click from its graph", async () => {
    openRoom([startedRun({ public_run_reference: "run1.YQ", run_id: "rebuild the index" })]);
    const { fireEvent, screen, waitFor } = testingLibrary;

    const row = await screen.findByRole("link", { name: /rebuild the index/ });
    // The node at work is the row's own fact; no state word repeats the mark
    // beside it (ADR 0019 §3).
    expect(row.textContent).toContain("agent");
    expect(row.textContent).not.toContain("Running");

    await fireEvent.click(row);
    await waitFor(() => expect(window.location.pathname).toBe("/atelier/runs/run1.YQ"));
  });

  // The three state lists are asked at once and answered separately, so a run
  // that opens a wait while the started list is still on the wire comes back in
  // two of them.
  it("shows a run once when two of the three reads answer with it, and keeps the fresher truth", async () => {
    openRoom([], {
      listRuns: vi.fn(async (_after?: string, state?: string) => {
        if (state === "STARTED") {
          return {
            items: [startedRun({ public_run_reference: "run1.YQ", run_id: "moving run" })],
            next_after: null
          };
        }
        if (state === "WAITING_INPUT") {
          return {
            items: [
              waitingInputRun({
                public_run_reference: "run1.YQ",
                run_id: "moving run",
                state_version: 2
              })
            ],
            next_after: null
          };
        }
        return { items: [], next_after: null };
      })
    });
    const { screen, waitFor, within } = testingLibrary;

    // The fresher read waits, so the run stands once -- as a pinned decision,
    // never also as a moving row.
    const pinnedRegion = await screen.findByRole("region", { name: "Open decisions" });
    await waitFor(() => {
      expect(within(pinnedRegion).getAllByText(/moving run/)).toHaveLength(1);
    });
    expect(screen.queryByRole("link", { name: /moving run/ })).toBeNull();
  });

  /**
   * The room holds the attention stream the Board used to hold, so a decision
   * that opens while the operator is sitting here arrives where it belongs.
   * The frame is only a nudge: what the room shows is the canonical read.
   */
  const waitingDecisionQuestion = "Ship it, or hold it back?";
  const waitingDecisionRevisionHash = "a".repeat(64);

  function waitingV3Run(overrides: Partial<RunV3> = {}): RunV3 {
    return {
      workflow_format_version: 3,
      run_id: "v3/decide",
      public_run_reference: "run1.YQ",
      workflow_revision_hash: waitingDecisionRevisionHash,
      workflow_name: "decide",
      agent_binding_set_hash: "b".repeat(64),
      run_configuration_revision_hash: "c".repeat(64),
      agent_bindings: [],
      orders: [],
      state_version: 1,
      state: "WAITING_INPUT",
      current_node_id: "approve",
      node_rail: [{ node_id: "approve", state: "needs_you", attempt: null }],
      // A resting Wait is operator-cancellable (#668).
      cancellation: cancellableBlock(),
      terminal_hash: null,
      latest_event_cursor: null,
      started_at: "2026-08-18T15:00:00Z",
      ended_at: null,
      ...overrides,
      current_node_execution_id: overrides.current_node_execution_id ?? waitingDecisionRevisionHash
    };
  }

  function waitingV3Revision(): WorkflowRevisionDetail {
    return {
      workflow_revision_hash: waitingDecisionRevisionHash,
      document_base64: "YQ==",
      graph: {
        workflow_format_version: 3,
        executable: true,
        not_executable_reason: null,
        node_count: 1,
        agent_roles: [],
        orders: [],
        wait_answer_schemas: [
          {
            node_id: "approve",
            schema: { ref: "decision", revision: "e".repeat(64) },
            kind: "boolean",
            values: null
          }
        ],
        node_previews: [
          { id: "approve", kind: "wait", role: null, instruction_start: null, depends_on: [] }
        ],
        loops: [],
        name: "Approve once",
        description: null
      }
    } as WorkflowRevisionDetail;
  }

  function waitingV3QuestionDetail() {
    return {
      run_id: "v3/decide",
      public_run_reference: "run1.YQ",
      node_id: "approve",
      state: "needs_you",
      job_base64: btoa(waitingDecisionQuestion),
      job_hash: "e".repeat(64),
      answer: null,
      provenance: null,
      refusal: null
    };
  }

  function openWaitingCard(runs: readonly RunV3[], overrides: Partial<CockpitApi> = {}): void {
    openRoom(runs, {
      getNodeDetail: vi.fn(async () => waitingV3QuestionDetail() as never),
      getWorkflowRevision: vi.fn(async () => waitingV3Revision()),
      ...overrides
    });
  }

  it("shows a decision that opens while the operator is looking, without a reload", async () => {
    const feed = new FakeRunEventFeed();
    const opened = waitingInputRun({ public_run_reference: "run1.YQ", run_id: "opened while here" });
    const getRun = vi.fn(async () => opened);
    openRoom([], { openAttentionEvents: feed.openAttention, getRun });
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    feed.handlers?.opened();

    feed.handlers?.event(
      JSON.stringify(waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" }))
    );

    expect((await screen.findByText(/opened while here/)).isConnected).toBe(true);
    expect(getRun).toHaveBeenCalledWith("run1.YQ");
    // The rail's number counts the same truth, from the same read.
    expect((await screen.findByLabelText(`1 ${railCopy.needsYouCountSuffix}`)).isConnected).toBe(
      true
    );
    expect(window.location.pathname).toBe("/atelier");
  });

  it("keeps an open decision card through a stream drop and takes the next one after recover, without a reload", async () => {
    const feed = new FakeRunEventFeed();
    const first = waitingV3Run({
      public_run_reference: "run1.YQ",
      run_id: "still waiting"
    });
    const recovered = waitingV3Run({
      public_run_reference: "run1.Yg",
      run_id: "after recover"
    });
    const getRun = vi.fn(async (reference: string) =>
      reference === recovered.public_run_reference ? recovered : first
    );
    openWaitingCard([first], { openAttentionEvents: feed.openAttention, getRun });
    const { screen, waitFor } = testingLibrary;

    expect(
      (await screen.findByRole("region", { name: waitingDecisionQuestion })).isConnected
    ).toBe(true);
    feed.handlers?.opened();

    await say("keep this conversation");
    expect(
      screen.getByRole("list", { name: workbenchPageCopy.transcriptLabel }).isConnected
    ).toBe(true);
    const pathname = window.location.pathname;

    feed.handlers?.disconnected();
    expect(screen.getByRole("region", { name: waitingDecisionQuestion }).isConnected).toBe(true);
    expect(screen.queryByText("Reconnecting")).toBeNull();
    expect(
      screen.getByRole("list", { name: workbenchPageCopy.transcriptLabel }).isConnected
    ).toBe(true);
    expect(screen.getByLabelText(workbenchPageCopy.composerLabel).isConnected).toBe(true);

    feed.handlers?.opened();
    feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, {
          public_run_reference: recovered.public_run_reference,
          cursor: "event1.Yg.1"
        })
      )
    );

    await waitFor(() => {
      expect(screen.getByText(/still waiting/).isConnected).toBe(true);
      expect(screen.getByText(/after recover/).isConnected).toBe(true);
      expect(screen.getAllByRole("region", { name: waitingDecisionQuestion })).toHaveLength(2);
    });
    expect(getRun).toHaveBeenCalledWith(recovered.public_run_reference);
    expect(window.location.pathname).toBe(pathname);
  });

  it("says plainly when the run behind an event could not be read, and offers one move", async () => {
    const feed = new FakeRunEventFeed();
    const getRun = vi.fn().mockRejectedValueOnce(new Error("run missing"));
    openRoom([], { openAttentionEvents: feed.openAttention, getRun });
    const { fireEvent, screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    feed.handlers?.opened();

    feed.handlers?.event(
      JSON.stringify(waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" }))
    );
    expect((await screen.findByText("run missing")).isConnected).toBe(true);

    // The one move repeats exactly the read that failed, and nothing else.
    getRun.mockResolvedValueOnce(
      waitingInputRun({ public_run_reference: "run1.YQ", run_id: "read on the second ask" })
    );
    await fireEvent.click(screen.getByRole("button", { name: workbenchPageCopy.retryEvent }));

    expect((await screen.findByText(/read on the second ask/)).isConnected).toBe(true);
    expect(screen.queryByText("run missing")).toBeNull();
  });

  it("names a row by the catalog's workflow name, and falls back to the run id when the catalog names nothing", async () => {
    openRoom(
      [
        startedRun({
          public_run_reference: "run1.YQ",
          run_id: "named",
          workflow_revision_hash: "b".repeat(64)
        }),
        startedRun({ public_run_reference: "run1.Yg", run_id: "unnamed" })
      ],
      {
        listWorkflowRevisions: vi.fn(async () => ({
          items: [
            {
              workflow_revision_hash: "b".repeat(64),
              workflow_format_version: 3 as const,
              executable: true,
              not_executable_reason: null,
              name: "Preview door",
              description: null
            }
          ],
          next_after_revision_hash: null
        }))
      }
    );
    const { screen } = testingLibrary;

    expect((await screen.findByText("Preview door")).isConnected).toBe(true);
    expect(screen.getByText("unnamed").isConnected).toBe(true);
  });

  it("carries the ochre count in the rail only while something waits, and never a fabricated zero", async () => {
    openRoom([startedRun({ public_run_reference: "run1.YQ" })]);
    const { screen, within } = testingLibrary;

    await screen.findByRole("link", { name: /run/ });
    const rail = screen.getByRole("navigation", { name: "Workshop" });
    expect(within(rail).queryByLabelText(/needs you/)).toBeNull();

    testingLibrary.cleanup();
    openRoom([waitingReconciliationRun({ public_run_reference: "run1.Yw" })]);

    const counted = await screen.findByLabelText(`1 ${railCopy.needsYouCountSuffix}`);
    expect(counted.textContent).toBe("1");
  });

  it("asks the durable list by every non-terminal state, and reads the workflow catalog beside it", async () => {
    const listRuns = listRunsByState([startedRun()]);
    const listWorkflowRevisions = vi.fn(async () => ({
      items: [],
      next_after_revision_hash: null
    }));
    window.history.replaceState(null, "", "/atelier");
    openChat({ listRuns, listWorkflowRevisions });
    const { screen } = testingLibrary;
    await screen.findByRole("link", { name: /run/ });

    expect(listRuns.mock.calls.map(([, state]) => state).sort()).toEqual([
      "STARTED",
      "WAITING_INPUT",
      "WAITING_RECONCILIATION"
    ]);
    expect(listWorkflowRevisions).toHaveBeenCalled();
  });

  // The identifier stays "an-empty-area-names-the-one-next-action"
  // (acceptance/131); the one action possible today is the Catalog.
  it("proves(an-empty-area-names-the-one-next-action): names the one next action possible today, and offers it once", async () => {
    openRoom();
    const { fireEvent, screen } = testingLibrary;

    await screen.findByRole("heading", { name: workbenchPageCopy.emptyTitle });
    expect(screen.getAllByRole("link", { name: workbenchPageCopy.emptyStart })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("link", { name: workbenchPageCopy.emptyStart }));

    expect((await screen.findByRole("heading", { name: "Catalog" })).isConnected).toBe(true);
  });

  it("proves(every-rendered-workbench-control-is-inventoried): every rendered Workbench control is inventoried with a question-shaped entry, and a control without an entry fails", async () => {
    const ids = Object.values(workbenchQuestions).map((entry) => entry.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const entry of Object.values(workbenchQuestions)) {
      expect(entry.question.endsWith("?")).toBe(true);
    }
    const { screen } = testingLibrary;

    openRoom([startedRun({ public_run_reference: "run1.YQ" })]);
    await screen.findByRole("link", { name: /run/ });
    expectWorkbenchControlsAreInventoried([
      workbenchQuestions.openRun.id,
      workbenchQuestions.saySomething.id,
      workbenchQuestions.emptyStart.id
    ]);

    testingLibrary.cleanup();
    openRoom([], { listRuns: vi.fn().mockRejectedValue(new Error("wire detail")) });
    await screen.findByRole("button", {
      name: retryLabel(workbenchPageCopy.runsLabel)
    });
    expectWorkbenchControlsAreInventoried([
      workbenchQuestions.reloadWorkbenchRuns.id,
      workbenchQuestions.saySomething.id,
      workbenchQuestions.emptyStart.id
    ]);

    const stage = document.querySelector(workbenchStageSelector);
    if (stage === null) {
      throw new Error("the Workbench stage is missing");
    }
    const stray = document.createElement("button");
    stray.setAttribute("aria-label", "Exact time");
    stage.append(stray);
    const unanswered = unansweredWorkbenchControls(stage).map(describeWorkbenchControl);
    expect(unanswered).toEqual(["button Exact time"]);
  });

  function expectWorkbenchControlsAreInventoried(expected: readonly string[]): void {
    const stage = document.querySelector(workbenchStageSelector);
    if (stage === null) {
      throw new Error("the Workbench stage is missing");
    }
    const unanswered = unansweredWorkbenchControls(stage);
    expect(
      unanswered.map(describeWorkbenchControl),
      unanswered.map(describeWorkbenchControl).join("; ")
    ).toEqual([]);
    const present = [...stage.querySelectorAll(workbenchInteractiveSelector)].map((element) => {
      const found = questionForWorkbenchControl(element);
      if (found === null) {
        throw new Error(`unmapped Workbench control: ${describeWorkbenchControl(element)}`);
      }
      return found.id;
    });
    expect(new Set(present)).toEqual(new Set(expected));
  }
});
