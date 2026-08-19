import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, RunV1, RunV3, WorkflowRevisionDetail } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import {
  completedRun,
  publicReference,
  revisionHash,
  startedRun,
  waitingInputRun,
  waitingReconciliationRun,
  workflowRevision
} from "../support/workflowV1";

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

function openAt(pathname: string, overrides: Partial<CockpitApi> = {}) {
  window.history.replaceState(null, "", pathname);
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub(overrides),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

const openProject = (runs: Array<RunV1 | RunV3>, overrides: Partial<CockpitApi> = {}) =>
  openAt("/atelier/project", {
    listRuns: vi.fn(async () => ({ items: runs, next_after: null })),
    ...overrides
  });

function listedV3Run(changes: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/two-agents",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "review",
    node_rail: [{ node_id: "review", state: "working", attempt: null }],
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: null,
    ...changes
  };
}

function listedV3Revision(name = "Two agents in a line"): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: revisionHash,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: ["builder"],
      orders: [],
      node_previews: [
        {
          id: "review",
          kind: "agent",
          role: "builder",
          instruction_start: "Do the one thing.",
          depends_on: []
        }
      ],
      name,
      description: null
    }
  };
}

describe("the project answers what is happening here", () => {
  it("heads the level with the one project of this installation", async () => {
    openProject([startedRun()]);

    expect((await screen.findByRole("heading", { name: "This workshop" })).isConnected).toBe(true);
  });

  it("groups the runs by what each one is doing, and omits a group nothing is in", async () => {
    openProject([
      startedRun({ public_run_reference: "run1.YQ", run_id: "alpha" }),
      waitingInputRun({ public_run_reference: "run1.Yg", run_id: "beta" }),
      startedRun({ public_run_reference: "run1.Yw", run_id: "gamma" })
    ]);

    const running = await screen.findByRole("region", { name: "Running" });
    expect(within(running).getAllByRole("link")).toHaveLength(2);
    expect(within(await screen.findByRole("region", { name: "Waiting for you" })).getAllByRole("link")).toHaveLength(1);
    expect(screen.queryByRole("region", { name: "Done" })).toBeNull();
  });

  it("lets a row carry the move a human owes and the group carry the state", async () => {
    openProject([
      startedRun({ public_run_reference: "run1.YQ", run_id: "alpha" }),
      waitingInputRun({ public_run_reference: "run1.Yg", run_id: "beta" }),
      waitingReconciliationRun({ public_run_reference: "run1.Yw", run_id: "gamma" }),
      completedRun({ public_run_reference: "run1.ZA", run_id: "delta" })
    ]);

    const waiting = await screen.findByRole("region", { name: "Waiting for you" });
    expect(within(waiting).getByText("Answer").isConnected).toBe(true);
    expect(within(waiting).getByText("Reconcile").isConnected).toBe(true);

    for (const group of ["Running", "Done"] as const) {
      const region = screen.getByRole("region", { name: group });
      expect(within(region).getByText(group === "Running" ? "alpha" : "delta").isConnected).toBe(
        true
      );
      expect(within(region).getByText(THE_ONE_PROJECT).isConnected).toBe(true);
    }
  });

  it("leads down into a run of this project", async () => {
    openProject([startedRun()]);

    const running = await screen.findByRole("region", { name: "Running" });
    await fireEvent.click(within(running).getByRole("link"));

    expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`);
  });

  it("keeps confirmed runs visible when a refresh fails, and says what failed", async () => {
    const listRuns = vi.fn().mockResolvedValue({ items: [startedRun()], next_after: null });
    openProject([], { listRuns });
    await screen.findByRole("region", { name: "Running" });

    listRuns.mockRejectedValueOnce(new Error("offline"));
    await fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect((await screen.findByRole("alert")).textContent).toContain("offline");
    expect(screen.getByRole("region", { name: "Running" }).isConnected).toBe(true);
  });

  it("says it is still looking instead of showing a project with nothing in it", async () => {
    openProject([], { listRuns: vi.fn(() => new Promise<never>(() => undefined)) });

    expect((await screen.findByText("Looking…")).isConnected).toBe(true);
    expect(screen.queryByRole("region", { name: "Running" })).toBeNull();
  });
});

describe("the project lists runs as the operator can scan them", () => {
  it("names the sort and keeps the newest activity first even when the durable list answers oldest first", async () => {
    openProject(
      [
        listedV3Run({
          run_id: "older",
          public_run_reference: "run1.b2xkZXI",
          started_at: "2026-08-18T14:00:00Z"
        }),
        listedV3Run({
          run_id: "newer",
          public_run_reference: "run1.bmV3ZXI",
          started_at: "2026-08-18T16:00:00Z"
        })
      ],
      { getWorkflowRevision: vi.fn(async () => listedV3Revision()) }
    );

    const running = await screen.findByRole("region", { name: "Running" });
    expect(screen.getByText("Newest first.").isConnected).toBe(true);
    const rows = within(running).getAllByRole("link");
    expect(rows[0]?.textContent).toContain("newer");
    expect(rows[0]?.textContent).not.toContain("older");
    expect(rows[1]?.textContent).toContain("older");
  });

  it("puts the local date and time on the row instead of behind a hover", async () => {
    openProject([listedV3Run()], {
      getWorkflowRevision: vi.fn(async () => listedV3Revision())
    });

    const row = await screen.findByRole("link", { name: /v3\/two-agents/ });
    const stamp = row.querySelector("time");

    expect(stamp?.getAttribute("datetime")).toBe("2026-08-18T15:00:00Z");
    expect(stamp?.textContent).toContain("2026");
    expect(screen.queryByRole("button", { name: "Exact time" })).toBeNull();
  });

  it("shows the project and the published workflow name on the row", async () => {
    openProject([listedV3Run()], {
      getWorkflowRevision: vi.fn(async () => listedV3Revision("Two agents in a line"))
    });

    const row = await screen.findByRole("link", { name: /v3\/two-agents/ });

    expect(row.textContent).toContain(THE_ONE_PROJECT);
    expect(row.textContent).toContain("Two agents in a line");
  });
});

describe("the queue names what does not exist yet", () => {
  it("names the absent ranking and offers the one action possible today, once", async () => {
    openProject([startedRun()]);

    const queue = await screen.findByRole("region", { name: "Queue" });

    expect(
      within(queue).getByText("This project has no priority and no assignment yet.").isConnected
    ).toBe(true);
    expect(within(queue).queryByText(/order|first|next|schedul|priorit\w+ is/i)).toBeNull();
    expect(screen.getAllByRole("link", { name: "Start a run" })).toHaveLength(1);

    await fireEvent.click(within(queue).getByRole("link", { name: "Start a run" }));

    expect((await screen.findByRole("heading", { name: "Choose a workflow" })).isConnected).toBe(true);
  });

  it("hints at no rule, no source, and no assignment the system does not have", async () => {
    openProject([startedRun()]);
    const queue = await screen.findByRole("region", { name: "Queue" });

    expect(within(queue).queryByRole("button")).toBeNull();
    expect(screen.queryByRole("region", { name: /Rules|Sources|Settings|Library/ })).toBeNull();
  });
});

describe("every level names the way back up", () => {
  it("proves(every-level-names-the-way-back-up): walks the named way from the run up to the project and from the project up into the studio", async () => {
    const feed = new FakeRunEventFeed();
    openAt(`/atelier/runs/${publicReference}`, {
      getRun: vi.fn(async () => startedRun()),
      getWorkflowRevision: vi.fn(async () => workflowRevision()),
      openRunEvents: feed.open,
      listRuns: vi.fn(async () => ({ items: [startedRun()], next_after: null }))
    });
    await screen.findByRole("heading", { name: "Run run" });

    const trail = screen.getByRole("navigation", { name: "Where you are" });
    await fireEvent.click(within(trail).getByRole("link", { name: "This workshop" }));

    expect((await screen.findByRole("heading", { name: "This workshop" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/project");

    await fireEvent.click(
      within(screen.getByRole("navigation", { name: "Where you are" })).getByRole("link", {
        name: "Studio"
      })
    );

    expect((await screen.findByRole("heading", { name: "Studio" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
  });
});
