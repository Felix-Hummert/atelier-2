import type * as SvelteTestingLibrary from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AnyRun, CockpitApi } from "../../src/api/client";
import { railCopy } from "../../src/lib/railCopy";
import { retryLabel } from "../../src/lib/readStateCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import {
  describeWorkbenchControl,
  questionForWorkbenchControl,
  workbenchInteractiveSelector,
  workbenchQuestions,
  workbenchStageSelector,
  unansweredWorkbenchControls
} from "../../src/lib/workbenchQuestions";
import { FakeRunEventFeed, PAGE_CURSORS } from "../support/cockpitApi";
import {
  startedRun,
  waitingInput,
  waitingInputRun,
  waitingReconciliationRun
} from "../support/workflowV1";

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

  it("links a conductor episode to its run, whose trail leads back to this room", async () => {
    const { takeConductorTurn, markConductorRun } = await import("../../src/lib/chatTranscript");
    const { conductorChatCopy } = await import("../../src/lib/conductorChatCopy");
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });

    const pendingId = takeConductorTurn("Starte die Kanarienprobe", conductorChatCopy.reading);
    expect(pendingId).not.toBeNull();
    markConductorRun(pendingId ?? "", "run1.cnVu");

    const episode = await screen.findByRole("link", { name: conductorChatCopy.openEpisode });
    expect(episode.getAttribute("href")).toBe("/atelier/runs/run1.cnVu");
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
 * The room the workshop opens on (ADR 0019 §1). What the Board used to hold
 * lives here now: the decisions that want a person, the runs that are moving,
 * and the one number the rail carries.
 */
describe("the workbench is the room the workshop opens on", () => {
  function listRunsByState(runs: readonly AnyRun[]) {
    return vi.fn(async (_after?: string, state?: string) => ({
      items: state === undefined ? [...runs] : runs.filter((run) => run.state === state),
      next_after: null
    }));
  }

  function openRoom(runs: readonly AnyRun[] = [], overrides: Partial<CockpitApi> = {}): void {
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
    const { fireEvent, screen, waitFor } = testingLibrary;

    const waiting = await screen.findAllByText(/Answer →/);
    expect(waiting).toHaveLength(2);
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
    const { screen } = testingLibrary;

    expect(await screen.findAllByRole("link", { name: /moving run/ })).toHaveLength(1);
    expect(screen.getByText(/Answer →/).isConnected).toBe(true);
  });

  /**
   * The room holds the attention stream the Board used to hold, so a decision
   * that opens while the operator is sitting here arrives where it belongs.
   * The frame is only a nudge: what the room shows is the canonical read.
   */
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
              workflow_format_version: 1 as const,
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

  // The identifier stays "studio-elements-answer-named-questions"
  // (acceptance/435): the stage it measures is the Workbench's.
  it("proves(studio-elements-answer-named-questions): every interactive Workbench control is listed against one named user question", async () => {
    const ids = Object.values(workbenchQuestions).map((entry) => entry.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const entry of Object.values(workbenchQuestions)) {
      expect(entry.question.endsWith("?")).toBe(true);
    }
    const { screen } = testingLibrary;

    openRoom([startedRun({ public_run_reference: "run1.YQ" })]);
    await screen.findByRole("link", { name: /run/ });
    expectWorkbenchControlsAnswerNamedQuestions([
      workbenchQuestions.openRun.id,
      workbenchQuestions.saySomething.id,
      workbenchQuestions.emptyStart.id
    ]);

    testingLibrary.cleanup();
    openRoom([], { listRuns: vi.fn().mockRejectedValue(new Error("wire detail")) });
    await screen.findByRole("button", {
      name: retryLabel(workbenchQuestions.reloadWorkbenchRuns.readLabel)
    });
    expectWorkbenchControlsAnswerNamedQuestions([
      workbenchQuestions.reloadWorkbenchRuns.id,
      workbenchQuestions.saySomething.id,
      workbenchQuestions.emptyStart.id
    ]);
  });

  function expectWorkbenchControlsAnswerNamedQuestions(expected: readonly string[]): void {
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
