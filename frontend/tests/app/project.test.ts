import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  type CockpitApi,
  type OccupancyRevision,
  type RunV1,
  type RunV3,
  type WorkflowRevisionDetail
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { studioQuestions } from "../../src/lib/studioQuestions";
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
    listProjects: vi.fn(async () => ({ items: [{ public_project_reference: "project1.dGVzdA" }] })),
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

function listedV3Revision(
  name = "Two agents in a line",
  hash = revisionHash
): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: hash,
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
      loops: [],
      name,
      description: null
    }
  };
}

type V3Graph = Extract<WorkflowRevisionDetail["graph"], { workflow_format_version: 3 }>;

function withV3Graph(
  name: string,
  hash: string,
  graph: Partial<V3Graph>
): WorkflowRevisionDetail {
  const detail = listedV3Revision(name, hash);
  if (detail.graph.workflow_format_version !== 3) throw new Error("the V3 fixture changed");
  return { ...detail, graph: { ...detail.graph, ...graph } };
}

describe("the project answers what is happening here", () => {
  it("heads the level with the one project of this installation", async () => {
    openProject([startedRun()]);

    expect((await screen.findByRole("heading", { name: THE_ONE_PROJECT })).isConnected).toBe(true);
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

  it("shows every grouped run's owned standing mark and word", async () => {
    openProject([
      startedRun({ public_run_reference: "run1.YQ", run_id: "running" }),
      waitingInputRun({ public_run_reference: "run1.Yg", run_id: "waiting" }),
      { ...startedRun({ public_run_reference: "run1.Yw", run_id: "failed" }), state: "FAILED" },
      completedRun({ public_run_reference: "run1.ZA", run_id: "done" })
    ]);

    const expectedStandings: Array<[string, string]> = [
      ["running", "▲Running"], ["waiting", "⬢Waiting for you"], ["failed", "◇Failed"], ["done", "●Done"]
    ];
    for (const [run, standing] of expectedStandings) {
      expect((await screen.findByRole("link", { name: new RegExp(run) })).textContent).toContain(standing);
    }
  });

  it("leads down into a run of this project", async () => {
    openProject([startedRun()]);

    const running = await screen.findByRole("region", { name: "Running" });
    await fireEvent.click(within(running).getByRole("link"));

    expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`);
  });

  it("offers no manual refresh once the Project read is confirmed", async () => {
    openProject([startedRun()]);

    await screen.findByRole("region", { name: "Running" });

    expect(screen.queryByRole("button", { name: /project runs/ })).toBeNull();
  });

  it("says it is still looking instead of showing a project with nothing in it", async () => {
    openProject([], { listRuns: vi.fn(() => new Promise<never>(() => undefined)) });

    expect((await screen.findByText("Looking…")).isConnected).toBe(true);
    expect(screen.queryByRole("region", { name: "Running" })).toBeNull();
  });

  it("repeats the same unavailable Project read until success", async () => {
    const listRuns = vi
      .fn()
      .mockRejectedValueOnce(new Error("first private detail"))
      .mockRejectedValueOnce(new Error("second private detail"))
      .mockResolvedValueOnce({ items: [startedRun()], next_after: null });
    openProject([], { listRuns });

    await screen.findByText("Project runs unavailable");
    // A fresh query per click, never a held reference: Retry mounts its own
    // control each failed round (ReadState.svelte's pattern for #514), so the
    // operator clicks whatever Retry is on screen right now.
    await fireEvent.click(screen.getByRole("button", { name: "Retry project runs" }));
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/private detail/)).toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry project runs" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("button", { name: "Retry project runs" }));

    expect((await screen.findByRole("region", { name: "Running" })).isConnected).toBe(true);
    expect(listRuns).toHaveBeenCalledTimes(3);
    expect(screen.queryByRole("button", { name: /project runs/ })).toBeNull();
    expect(window.location.pathname).toBe("/atelier/project");
  });

  it("does not confirm a partial page as Project truth, and Retry replaces it with the complete read", async () => {
    const partial = startedRun({
      public_run_reference: "run1.cGFydGlhbA",
      run_id: "partial"
    });
    const complete = startedRun({ run_id: "complete" });
    const listRuns = vi
      .fn()
      .mockResolvedValueOnce({ items: [partial], next_after: "more" })
      .mockRejectedValueOnce(new Error("private later-page detail"))
      .mockResolvedValueOnce({ items: [complete], next_after: null });
    openProject([], { listRuns });

    await screen.findByText("Project runs incomplete");
    expect(screen.queryByText("partial")).toBeNull();
    expect(screen.queryByText(/private later-page detail/)).toBeNull();
    expect(listRuns).toHaveBeenNthCalledWith(2, "more");

    await fireEvent.click(screen.getByRole("button", { name: "Retry project runs" }));

    expect((await screen.findByText("complete")).isConnected).toBe(true);
    expect(screen.queryByText("Project runs incomplete")).toBeNull();
  });

  it("keeps runs and workflow names atomic: a name-lookup failure confirms nothing, and Retry confirms both together", async () => {
    const run = listedV3Run({ run_id: "confirmed run" });
    let nameReads = 0;
    const getWorkflowRevision = vi.fn(async (hash: string) => {
      nameReads += 1;
      if (nameReads === 1) throw new Error("private name detail");
      return listedV3Revision("Confirmed workflow", hash);
    });
    openProject([run], { getWorkflowRevision });

    await screen.findByText("Project runs unavailable");
    expect(screen.queryByRole("link", { name: /confirmed run/ })).toBeNull();
    expect(screen.queryByText(/private name detail/)).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Retry project runs" }));

    const confirmedRow = await screen.findByRole("link", { name: /confirmed run/ });
    expect(confirmedRow.textContent).toContain("Confirmed workflow");
    expect(screen.queryByText("Project runs unavailable")).toBeNull();
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
    expect(
      screen.queryByRole("button", { name: studioQuestions.lastLandingTime.hintLabel })
    ).toBeNull();
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

    expect(within(queue).getByText("No priority or assignment.").isConnected).toBe(true);
    expect(within(queue).queryByText(/order|first|next|schedul|priorit\w+ is/i)).toBeNull();
    expect(screen.getAllByRole("link", { name: "Start a run" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("link", { name: "Start a run" }));

    expect((await screen.findByRole("heading", { name: "Choose a workflow" })).isConnected).toBe(true);
  });

  it("hints at no rule, no source, and no assignment the system does not have", async () => {
    openProject([startedRun()]);
    const queue = await screen.findByRole("region", { name: "Queue" });

    expect(within(queue).queryByRole("button")).toBeNull();
    expect(screen.queryByRole("region", { name: /Rules|Sources|Settings|Library/ })).toBeNull();
  });

  it("keeps existing runs before the subordinate queue and occupancy controls", async () => {
    openProject([startedRun()]);

    const runGroup = await screen.findByRole("region", { name: "Running" });
    const queue = screen.getByRole("region", { name: "Queue" });
    const occupancy = screen.getByRole("region", { name: "Occupancy" });

    expect(runGroup.compareDocumentPosition(queue) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(runGroup.compareDocumentPosition(occupancy) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });
});

describe("project occupancy editor", () => {
  const projectReference = "project1.dGVzdA";
  const lineageId = "a".repeat(64);
  const workflowHash = "b".repeat(64);
  const knownHash = "c".repeat(64);
  const foreignHash = "d".repeat(64);

  function occupancy(bindings: Array<{ role: string; agent_configuration_revision_hash: string }>) {
    return {
      project_id: "test",
      public_project_reference: projectReference,
      lineage_id: lineageId,
      revision_number: 1,
      occupancy_revision_hash: "e".repeat(64),
      bindings
    };
  }

  function openOccupancyEditor(
    overrides: Partial<CockpitApi> = {},
    bindings: Array<{ role: string; agent_configuration_revision_hash: string }> = []
  ) {
    return openProject([], {
      listProjects: vi.fn(async () => ({ items: [{ public_project_reference: projectReference }] })),
      listWorkflowRevisions: vi.fn(async () => ({
        items: [{ workflow_revision_hash: workflowHash, workflow_format_version: 3 as const, executable: true, not_executable_reason: null, name: "occupancy-proof", description: null }],
        next_after_revision_hash: null
      })),
      getRevisionByName: vi.fn(async () => ({ display_name: "occupancy-proof", lineage_id: lineageId, workflow_revision_hash: workflowHash, revision_number: 1 })),
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [{ model: "operator", auth_profile_revision_hash: "f".repeat(64), executor_revision: "v1", provider_id: "fake", auth_mode: "api_key" as const, requested_capability: "headless" as const, agent_configuration_revision_hash: knownHash, startable: true, not_startable_reason: null }],
        next_after_revision_hash: null
      })),
      getWorkflowRevision: vi.fn(async () => listedV3Revision("occupancy-proof", workflowHash)),
      getProjectOccupancy: vi.fn(async () => occupancy(bindings)),
      ...overrides
    });
  }

  async function chooseWorkflow(): Promise<HTMLSelectElement> {
    const workflow = await screen.findByRole("combobox", { name: "Workflow occupancy" });
    await fireEvent.change(workflow, { target: { value: workflowHash } });
    return await screen.findByRole("combobox", { name: "Recommendation for builder" }) as HTMLSelectElement;
  }

  function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (reason?: unknown) => void;
    const promise = new Promise<T>((resolvePromise, rejectPromise) => {
      resolve = resolvePromise;
      reject = rejectPromise;
    });
    return { promise, resolve, reject };
  }

  it("changes an authored role to explicit None while preserving a foreign binding", async () => {
    const putProjectOccupancy = vi.fn(async (_project: string, _lineage: string, write: {
      input: {
        revision_number: number;
        bindings: Array<{ role: string; agent_configuration_revision_hash: string }>;
      };
      body: string;
    }) => ({ status: 201, value: { ...occupancy(write.input.bindings), revision_number: write.input.revision_number } }));
    openOccupancyEditor({ putProjectOccupancy }, [
        { role: "builder", agent_configuration_revision_hash: knownHash },
        { role: "foreign", agent_configuration_revision_hash: foreignHash }
      ]);

    const workflow = await screen.findByRole("combobox", { name: "Workflow occupancy" });
    await fireEvent.change(workflow, { target: { value: workflowHash } });
    const builder = await screen.findByRole("combobox", { name: "Recommendation for builder" });
    expect((builder as HTMLSelectElement).value).toBe(knownHash);
    await fireEvent.change(builder, { target: { value: "" } });
    const save = screen.getByRole("button", { name: "Save" });
    expect((save as HTMLButtonElement).disabled).toBe(false);
    await fireEvent.click(save);

    await waitFor(() => expect(putProjectOccupancy).toHaveBeenCalledWith(projectReference, lineageId, {
      projectReference,
      lineageId,
      input: {
        revision_number: 2,
        bindings: [{ role: "foreign", agent_configuration_revision_hash: foreignHash }]
      },
      body: JSON.stringify({
        revision_number: 2,
        bindings: [{ role: "foreign", agent_configuration_revision_hash: foreignHash }]
      })
    }));
  });

  it("shows unavailable rather than an editable partial editor when its complete base cannot load", async () => {
    openOccupancyEditor({
      listAgentConfigurationRevisions: vi.fn(async () => {
        throw new Error("private transport detail");
      })
    });

    expect((await screen.findByText("Project occupancy unavailable")).isConnected).toBe(true);
    expect(screen.queryByRole("combobox", { name: "Workflow occupancy" })).toBeNull();
    expect(screen.queryByText(/private transport detail/)).toBeNull();
  });

  it.each([
    ["the project list is not exactly one", {
      listProjects: vi.fn(async () => ({ items: [] }))
    }],
    ["a later workflow page is unreadable", {
      listWorkflowRevisions: vi
        .fn()
        .mockResolvedValueOnce({ items: [], next_after_revision_hash: workflowHash })
        .mockRejectedValueOnce(new Error("later workflow page"))
    }],
    ["a later agent page is unreadable", {
      listAgentConfigurationRevisions: vi
        .fn()
        .mockResolvedValueOnce({ items: [], next_after_revision_hash: knownHash })
        .mockRejectedValueOnce(new Error("later agent page"))
    }],
    ["the named catalog head disagrees with its list member", {
      getRevisionByName: vi.fn(async () => ({
        display_name: "occupancy-proof",
        lineage_id: lineageId,
        workflow_revision_hash: foreignHash,
        revision_number: 1
      }))
    }],
    ["the named catalog resolution is unavailable", {
      getRevisionByName: vi.fn(async () => {
        throw new Error("catalog private failure");
      })
    }]
  ])("does not offer a partial editor when %s", async (_case, overrides) => {
    openOccupancyEditor(overrides);

    expect((await screen.findByText("Project occupancy unavailable")).isConnected).toBe(true);
    expect(screen.queryByRole("combobox", { name: "Workflow occupancy" })).toBeNull();
  });

  it("treats only occupancy-missing as the saveable empty state", async () => {
    const missing = new CockpitRequestError("missing", {
      type: "urn:atelier2:problem:v1:occupancy-missing",
      title: "Occupancy not found",
      status: 404,
      detail: "No occupancy was saved."
    });
    const putProjectOccupancy = vi.fn(async (_project: string, _lineage: string, write: {
      input: { revision_number: number; bindings: Array<{ role: string; agent_configuration_revision_hash: string }> };
      body: string;
    }) => ({ status: 201, value: { ...occupancy(write.input.bindings), revision_number: write.input.revision_number } }));
    openOccupancyEditor({ getProjectOccupancy: vi.fn().mockRejectedValue(missing), putProjectOccupancy });

    const builder = await chooseWorkflow();
    expect((await screen.findByText("No project recommendations yet.")).isConnected).toBe(true);
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.change(builder, { target: { value: knownHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putProjectOccupancy).toHaveBeenCalledWith(
      projectReference,
      lineageId,
      expect.objectContaining({ input: { revision_number: 1, bindings: [{ role: "builder", agent_configuration_revision_hash: knownHash }] } })
    ));
  });

  it("does not turn another occupancy read failure into an empty editable truth", async () => {
    openOccupancyEditor({ getProjectOccupancy: vi.fn().mockRejectedValue(new Error("private read failure")) });

    const workflow = await screen.findByRole("combobox", { name: "Workflow occupancy" });
    await fireEvent.change(workflow, { target: { value: workflowHash } });
    expect((await screen.findByText("Project occupancy unavailable")).isConnected).toBe(true);
    expect(screen.queryByRole("combobox", { name: "Recommendation for builder" })).toBeNull();
    expect(screen.queryByText(/private read failure/)).toBeNull();
  });

  it("does not show role controls when the selected workflow detail is unavailable", async () => {
    openOccupancyEditor({ getWorkflowRevision: vi.fn().mockRejectedValue(new Error("private detail failure")) });

    await fireEvent.change(await screen.findByRole("combobox", { name: "Workflow occupancy" }), { target: { value: workflowHash } });
    expect((await screen.findByText("Project occupancy unavailable")).isConnected).toBe(true);
    expect(screen.queryByRole("combobox", { name: "Recommendation for builder" })).toBeNull();
    expect(screen.queryByText(/private detail failure/)).toBeNull();
  });

  it("replaces one authored binding while preserving unavailable authored and foreign bindings", async () => {
    const unavailableHash = "9".repeat(64);
    const putProjectOccupancy = vi.fn(async (_project: string, _lineage: string, write: {
      input: { revision_number: number; bindings: Array<{ role: string; agent_configuration_revision_hash: string }> };
      body: string;
    }) => ({ status: 201, value: { ...occupancy(write.input.bindings), revision_number: write.input.revision_number } }));
    openOccupancyEditor({
      putProjectOccupancy,
      getWorkflowRevision: vi.fn(async () =>
        withV3Graph("occupancy-proof", workflowHash, {
          node_count: 2,
          agent_roles: ["builder", "reviewer"],
          node_previews: [
            { id: "build", kind: "agent", role: "builder", instruction_start: "Build.", depends_on: [] },
            { id: "review", kind: "agent", role: "reviewer", instruction_start: "Review.", depends_on: [] }
          ]
        })
      ),
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [
          { model: "operator", auth_profile_revision_hash: "f".repeat(64), executor_revision: "v1", provider_id: "fake", auth_mode: "api_key" as const, requested_capability: "headless" as const, agent_configuration_revision_hash: knownHash, startable: true, not_startable_reason: null },
          { model: "reviewer", auth_profile_revision_hash: "a".repeat(64), executor_revision: "v1", provider_id: "fake", auth_mode: "api_key" as const, requested_capability: "headless" as const, agent_configuration_revision_hash: foreignHash, startable: true, not_startable_reason: null }
        ],
        next_after_revision_hash: null
      }))
    }, [
      { role: "builder", agent_configuration_revision_hash: unavailableHash },
      { role: "reviewer", agent_configuration_revision_hash: knownHash },
      { role: "external", agent_configuration_revision_hash: foreignHash }
    ]);

    await fireEvent.change(await screen.findByRole("combobox", { name: "Workflow occupancy" }), { target: { value: workflowHash } });
    const builder = await screen.findByRole("combobox", { name: "Recommendation for builder" }) as HTMLSelectElement;
    expect(builder.value).toBe(unavailableHash);
    await fireEvent.change(screen.getByRole("combobox", { name: "Recommendation for reviewer" }), { target: { value: foreignHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putProjectOccupancy).toHaveBeenCalledWith(projectReference, lineageId, expect.objectContaining({
      input: {
        revision_number: 2,
        bindings: [
          { role: "external", agent_configuration_revision_hash: foreignHash },
          { role: "builder", agent_configuration_revision_hash: unavailableHash },
          { role: "reviewer", agent_configuration_revision_hash: foreignHash }
        ]
      }
    })));
  });

  it("names a roleless workflow instead of inventing recommendations", async () => {
    openOccupancyEditor({
      getWorkflowRevision: vi.fn(async () =>
        withV3Graph("occupancy-proof", workflowHash, {
          agent_roles: [],
          node_previews: [
            { id: "wait", kind: "wait", role: null, instruction_start: null, depends_on: [] }
          ]
        })
      )
    });

    await fireEvent.change(await screen.findByRole("combobox", { name: "Workflow occupancy" }), { target: { value: workflowHash } });
    expect((await screen.findByText("This workflow declares no agent roles.")).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("names a complete empty agent catalog while keeping the explicit None choice", async () => {
    openOccupancyEditor({
      listAgentConfigurationRevisions: vi.fn(async () => ({ items: [], next_after_revision_hash: null }))
    });

    const builder = await chooseWorkflow();
    expect((await screen.findByText("No published agents yet.")).isConnected).toBe(true);
    expect(within(builder).getByRole("option", { name: "None" }).isConnected).toBe(true);
  });

  it("retries one frozen occupancy payload after an uncertain write", async () => {
    const input = { revision_number: 2, bindings: [{ role: "builder", agent_configuration_revision_hash: knownHash }] };
    const putProjectOccupancy = vi.fn()
      .mockRejectedValueOnce(new Error("transport detail"))
      .mockResolvedValueOnce({ status: 201, value: { ...occupancy(input.bindings), revision_number: input.revision_number } });
    openOccupancyEditor({ putProjectOccupancy });
    await fireEvent.change(await screen.findByRole("combobox", { name: "Workflow occupancy" }), { target: { value: workflowHash } });
    await screen.findByRole("combobox", { name: "Recommendation for builder" });
    await fireEvent.change(screen.getByRole("combobox", { name: "Recommendation for builder" }), { target: { value: knownHash } });
    const save = screen.getByRole("button", { name: "Save" });
    void fireEvent.click(save);
    void fireEvent.click(save);
    await screen.findByText("Occupancy save unconfirmed.");
    await fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(putProjectOccupancy).toHaveBeenNthCalledWith(
      2,
      projectReference,
      lineageId,
      expect.objectContaining({ input, body: JSON.stringify(input) })
    ));
    expect(putProjectOccupancy.mock.calls[0]).toEqual(putProjectOccupancy.mock.calls[1]);
  });

  it("locks the editor behind one deferred write and confirms only its exact response", async () => {
    const pending = deferred<{ status: number; value: OccupancyRevision }>();
    const putProjectOccupancy = vi.fn(() => pending.promise);
    openOccupancyEditor({ putProjectOccupancy });

    const builder = await chooseWorkflow();
    await fireEvent.change(builder, { target: { value: knownHash } });
    const save = screen.getByRole("button", { name: "Save" });
    void fireEvent.click(save);
    void fireEvent.click(save);

    await waitFor(() => expect((screen.getByRole("combobox", { name: "Workflow occupancy" }) as HTMLSelectElement).disabled).toBe(true));
    expect((screen.getByRole("combobox", { name: "Recommendation for builder" }) as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("Saving occupancy");
    expect(putProjectOccupancy).toHaveBeenCalledTimes(1);
    await fireEvent.change(screen.getByRole("combobox", { name: "Workflow occupancy" }), { target: { value: "" } });

    pending.resolve({ status: 201, value: { ...occupancy([{ role: "builder", agent_configuration_revision_hash: knownHash }]), revision_number: 2 } });
    expect((await screen.findByText("Saved")).isConnected).toBe(true);
    expect(screen.queryByText("Saving occupancy…")).toBeNull();
    expect(putProjectOccupancy).toHaveBeenCalledTimes(1);
    expect((screen.getByRole("combobox", { name: "Workflow occupancy" }) as HTMLSelectElement).value).toBe(workflowHash);
  });

  it("does not call a write for an unchanged complete occupancy", async () => {
    const putProjectOccupancy = vi.fn();
    openOccupancyEditor({ putProjectOccupancy }, [{ role: "builder", agent_configuration_revision_hash: knownHash }]);

    await chooseWorkflow();
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(putProjectOccupancy).not.toHaveBeenCalled();
  });

  it("refuses an unsafe next durable revision before it can write an unreadable value", async () => {
    const putProjectOccupancy = vi.fn();
    const getProjectOccupancy = vi.fn(async () => ({
      ...occupancy([{ role: "builder", agent_configuration_revision_hash: foreignHash }]),
      revision_number: Number.MAX_SAFE_INTEGER
    }));
    openOccupancyEditor({ getProjectOccupancy, putProjectOccupancy });

    const builder = await chooseWorkflow();
    await fireEvent.change(builder, { target: { value: knownHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByText("Project occupancy unavailable.")).isConnected).toBe(true);
    expect(putProjectOccupancy).not.toHaveBeenCalled();
  });

  it("does not claim Saved after a response with another durable identity", async () => {
    const putProjectOccupancy = vi.fn(async () => ({
      status: 200,
      value: { ...occupancy([{ role: "builder", agent_configuration_revision_hash: foreignHash }]), revision_number: 2 }
    }));
    openOccupancyEditor({ putProjectOccupancy });

    const builder = await chooseWorkflow();
    await fireEvent.change(builder, { target: { value: knownHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByText("Occupancy save unconfirmed.")).isConnected).toBe(true);
    expect(screen.queryByText("Saved")).toBeNull();
    expect(putProjectOccupancy).toHaveBeenCalledTimes(1);
  });

  it("does not claim Saved after a response with another revision number", async () => {
    const putProjectOccupancy = vi.fn(async () => ({
      status: 201,
      value: { ...occupancy([{ role: "builder", agent_configuration_revision_hash: knownHash }]), revision_number: 3 }
    }));
    openOccupancyEditor({ putProjectOccupancy });

    const builder = await chooseWorkflow();
    await fireEvent.change(builder, { target: { value: knownHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByText("Occupancy save unconfirmed.")).isConnected).toBe(true);
    expect(screen.queryByText("Saved")).toBeNull();
  });

  it.each([
    ["project", { public_project_reference: "project1.b3RoZXI" }],
    ["lineage", { lineage_id: foreignHash }]
  ])("rejects a response with only a wrong %s identity", async (_field, changed) => {
    const putProjectOccupancy = vi.fn(async () => ({
      status: 201,
      value: { ...occupancy([{ role: "builder", agent_configuration_revision_hash: knownHash }]), ...changed, revision_number: 2 }
    }));
    openOccupancyEditor({ putProjectOccupancy });

    const builder = await chooseWorkflow();
    await fireEvent.change(builder, { target: { value: knownHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByText("Occupancy save unconfirmed.")).isConnected).toBe(true);
    expect(screen.queryByText("Saved")).toBeNull();
  });

  it("matches response bindings as exact role-to-hash maps, not locale-dependent order", async () => {
    const composed = "é";
    const decomposed = "e\u0301";
    const getWorkflowRevision = vi.fn(async () =>
      withV3Graph("occupancy-proof", workflowHash, {
        node_count: 2,
        agent_roles: [decomposed, composed],
        node_previews: [
          { id: "first", kind: "agent", role: decomposed, instruction_start: "First.", depends_on: [] },
          { id: "second", kind: "agent", role: composed, instruction_start: "Second.", depends_on: [] }
        ]
      })
    );
    const putProjectOccupancy = vi.fn(async (_project: string, _lineage: string, write: {
      input: { revision_number: number; bindings: Array<{ role: string; agent_configuration_revision_hash: string }> };
      body: string;
    }) => ({ status: 201, value: { ...occupancy([...write.input.bindings].reverse()), revision_number: write.input.revision_number } }));
    openOccupancyEditor({
      getWorkflowRevision,
      putProjectOccupancy,
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [
          { model: "operator", auth_profile_revision_hash: "f".repeat(64), executor_revision: "v1", provider_id: "fake", auth_mode: "api_key" as const, requested_capability: "headless" as const, agent_configuration_revision_hash: knownHash, startable: true, not_startable_reason: null },
          { model: "reviewer", auth_profile_revision_hash: "a".repeat(64), executor_revision: "v1", provider_id: "fake", auth_mode: "api_key" as const, requested_capability: "headless" as const, agent_configuration_revision_hash: foreignHash, startable: true, not_startable_reason: null }
        ],
        next_after_revision_hash: null
      }))
    });

    await fireEvent.change(await screen.findByRole("combobox", { name: "Workflow occupancy" }), { target: { value: workflowHash } });
    await fireEvent.change(await screen.findByRole("combobox", { name: `Recommendation for ${composed}` }), { target: { value: knownHash } });
    await fireEvent.change(await screen.findByRole("combobox", { name: `Recommendation for ${decomposed}` }), { target: { value: foreignHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByText("Saved")).isConnected).toBe(true);
  });

  it.each([
    ["a definitive occupancy collision", new CockpitRequestError("collision", {
      type: "urn:atelier2:problem:v1:occupancy-revision-collision", title: "Occupancy revision collision", status: 409, detail: "The bytes differ."
    }, true)],
    ["durable corruption", new CockpitRequestError("corrupt", {
      type: "urn:atelier2:problem:v1:durable-state-corrupt", title: "Durable state is corrupt", status: 500, detail: "The stored bytes disagree."
    })]
  ])("does not offer blind Retry after %s", async (_case, failure) => {
    const putProjectOccupancy = vi.fn().mockRejectedValueOnce(failure);
    openOccupancyEditor({ putProjectOccupancy });

    const builder = await chooseWorkflow();
    await fireEvent.change(builder, { target: { value: knownHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByText("Project occupancy unavailable.")).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(putProjectOccupancy).toHaveBeenCalledTimes(1);
  });

  it("reloads the same conflicted workflow instead of retrying its write", async () => {
    const conflict = new CockpitRequestError("conflict", {
      type: "urn:atelier2:problem:v1:occupancy-revision-conflict",
      title: "Occupancy revision conflict",
      status: 409,
      detail: "The saved revision changed."
    });
    const getProjectOccupancy = vi
      .fn()
      .mockResolvedValueOnce(occupancy([]))
      .mockResolvedValueOnce(occupancy([{ role: "builder", agent_configuration_revision_hash: foreignHash }]));
    const putProjectOccupancy = vi.fn().mockRejectedValueOnce(conflict);
    openOccupancyEditor({ getProjectOccupancy, putProjectOccupancy });

    const builder = await chooseWorkflow();
    await fireEvent.change(builder, { target: { value: knownHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByText("Occupancy changed elsewhere.")).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    const reloaded = await screen.findByRole("combobox", { name: "Recommendation for builder" }) as HTMLSelectElement;
    await waitFor(() => expect(getProjectOccupancy).toHaveBeenCalledTimes(2));
    expect(reloaded.value).toBe(foreignHash);
    expect(putProjectOccupancy).toHaveBeenCalledTimes(1);
  });

  it("fences a late selection after Choose so its role cannot return", async () => {
    const detail = deferred<WorkflowRevisionDetail>();
    const loadedOccupancy = deferred<OccupancyRevision>();
    openOccupancyEditor({
      getWorkflowRevision: vi.fn(() => detail.promise),
      getProjectOccupancy: vi.fn(() => loadedOccupancy.promise)
    });

    const workflow = await screen.findByRole("combobox", { name: "Workflow occupancy" });
    await fireEvent.change(workflow, { target: { value: workflowHash } });
    await fireEvent.change(workflow, { target: { value: "" } });
    detail.resolve(listedV3Revision("occupancy-proof", workflowHash));
    loadedOccupancy.resolve(occupancy([{ role: "builder", agent_configuration_revision_hash: knownHash }]));

    await waitFor(() => expect(screen.queryByRole("combobox", { name: "Recommendation for builder" })).toBeNull());
    expect((workflow as HTMLSelectElement).value).toBe("");
  });

  it("keeps a late A detail and occupancy from replacing a newer B selection", async () => {
    const secondHash = "1".repeat(64);
    const secondLineage = "2".repeat(64);
    const firstDetail = deferred<WorkflowRevisionDetail>();
    const secondDetail = deferred<WorkflowRevisionDetail>();
    const firstOccupancy = deferred<OccupancyRevision>();
    const secondOccupancy = deferred<OccupancyRevision>();
    openOccupancyEditor({
      listWorkflowRevisions: vi.fn(async () => ({
        items: [
          { workflow_revision_hash: workflowHash, workflow_format_version: 3 as const, executable: true, not_executable_reason: null, name: "first", description: null },
          { workflow_revision_hash: secondHash, workflow_format_version: 3 as const, executable: true, not_executable_reason: null, name: "second", description: null }
        ],
        next_after_revision_hash: null
      })),
      getRevisionByName: vi.fn(async (name: string) => name === "first"
        ? { display_name: "first", lineage_id: lineageId, workflow_revision_hash: workflowHash, revision_number: 1 }
        : { display_name: "second", lineage_id: secondLineage, workflow_revision_hash: secondHash, revision_number: 1 }),
      getWorkflowRevision: vi.fn((hash: string) => hash === workflowHash ? firstDetail.promise : secondDetail.promise),
      getProjectOccupancy: vi.fn((_project: string, lineage: string) => lineage === lineageId ? firstOccupancy.promise : secondOccupancy.promise)
    });

    const workflow = await screen.findByRole("combobox", { name: "Workflow occupancy" });
    await fireEvent.change(workflow, { target: { value: workflowHash } });
    await fireEvent.change(workflow, { target: { value: secondHash } });
    secondDetail.resolve(withV3Graph("second", secondHash, {
      agent_roles: ["reviewer"],
      node_previews: [{ id: "review", kind: "agent", role: "reviewer", instruction_start: "Review.", depends_on: [] }]
    }));
    secondOccupancy.resolve({ ...occupancy([]), lineage_id: secondLineage });
    expect((await screen.findByRole("combobox", { name: "Recommendation for reviewer" })).isConnected).toBe(true);

    firstDetail.resolve(listedV3Revision("first", workflowHash));
    firstOccupancy.resolve(occupancy([{ role: "builder", agent_configuration_revision_hash: knownHash }]));
    await waitFor(() => expect(screen.queryByRole("combobox", { name: "Recommendation for builder" })).toBeNull());
    expect((workflow as HTMLSelectElement).value).toBe(secondHash);
  });

  it("fences a late Reload for one lineage after the operator chooses another", async () => {
    const secondHash = "3".repeat(64);
    const secondLineage = "4".repeat(64);
    const reloadedDetail = deferred<WorkflowRevisionDetail>();
    const reloadedOccupancy = deferred<OccupancyRevision>();
    const secondDetail = deferred<WorkflowRevisionDetail>();
    const secondOccupancy = deferred<OccupancyRevision>();
    let firstDetailReads = 0;
    let firstOccupancyReads = 0;
    const conflict = new CockpitRequestError("conflict", {
      type: "urn:atelier2:problem:v1:occupancy-revision-conflict",
      title: "Occupancy revision conflict",
      status: 409,
      detail: "The saved revision changed."
    });
    openOccupancyEditor({
      listWorkflowRevisions: vi.fn(async () => ({
        items: [
          { workflow_revision_hash: workflowHash, workflow_format_version: 3 as const, executable: true, not_executable_reason: null, name: "first", description: null },
          { workflow_revision_hash: secondHash, workflow_format_version: 3 as const, executable: true, not_executable_reason: null, name: "second", description: null }
        ],
        next_after_revision_hash: null
      })),
      getRevisionByName: vi.fn(async (name: string) => name === "first"
        ? { display_name: "first", lineage_id: lineageId, workflow_revision_hash: workflowHash, revision_number: 1 }
        : { display_name: "second", lineage_id: secondLineage, workflow_revision_hash: secondHash, revision_number: 1 }),
      getWorkflowRevision: vi.fn((hash: string) => {
        if (hash === secondHash) return secondDetail.promise;
        firstDetailReads += 1;
        return firstDetailReads === 1
          ? Promise.resolve(listedV3Revision("first", workflowHash))
          : reloadedDetail.promise;
      }),
      getProjectOccupancy: vi.fn((_project: string, lineage: string) => {
        if (lineage === secondLineage) return secondOccupancy.promise;
        firstOccupancyReads += 1;
        return firstOccupancyReads === 1 ? Promise.resolve(occupancy([])) : reloadedOccupancy.promise;
      }),
      putProjectOccupancy: vi.fn().mockRejectedValueOnce(conflict)
    });

    const workflow = await screen.findByRole("combobox", { name: "Workflow occupancy" });
    await fireEvent.change(workflow, { target: { value: workflowHash } });
    const builder = await screen.findByRole("combobox", { name: "Recommendation for builder" });
    await fireEvent.change(builder, { target: { value: knownHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await fireEvent.click(await screen.findByRole("button", { name: "Reload" }));
    await fireEvent.change(workflow, { target: { value: secondHash } });
    secondDetail.resolve(withV3Graph("second", secondHash, {
      agent_roles: ["reviewer"],
      node_previews: [{ id: "review", kind: "agent", role: "reviewer", instruction_start: "Review.", depends_on: [] }]
    }));
    secondOccupancy.resolve({ ...occupancy([]), lineage_id: secondLineage });
    expect((await screen.findByRole("combobox", { name: "Recommendation for reviewer" })).isConnected).toBe(true);

    reloadedDetail.resolve(listedV3Revision("first", workflowHash));
    reloadedOccupancy.resolve(occupancy([{ role: "builder", agent_configuration_revision_hash: foreignHash }]));
    await waitFor(() => expect(screen.queryByRole("combobox", { name: "Recommendation for builder" })).toBeNull());
    expect((workflow as HTMLSelectElement).value).toBe(secondHash);
  });
});

describe("every level names the way back up", () => {
  it("proves(every-level-names-the-way-back-up): walks the named way from the run up to the project and from the project up into the board", async () => {
    const feed = new FakeRunEventFeed();
    openAt(`/atelier/runs/${publicReference}`, {
      getRun: vi.fn(async () => startedRun()),
      getWorkflowRevision: vi.fn(async () => workflowRevision()),
      openRunEvents: feed.open,
      listRuns: vi.fn(async () => ({ items: [startedRun()], next_after: null }))
    });
    await screen.findByRole("heading", { name: "Unnamed workflow" });

    const trail = screen.getByRole("navigation", { name: "Where you are" });
    await fireEvent.click(within(trail).getByRole("link", { name: THE_ONE_PROJECT }));

    expect((await screen.findByRole("heading", { name: THE_ONE_PROJECT })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/project");

    await fireEvent.click(
      within(screen.getByRole("navigation", { name: "Where you are" })).getByRole("link", {
        name: "Board"
      })
    );

    expect((await screen.findByRole("heading", { name: "Board" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
  });
});
