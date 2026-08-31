import { cleanup, render, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  encodePublicRunReference,
  type CockpitApi,
  type NodeDetail,
  type RunV3,
  type WorkflowRevisionDetail,
  type WorkflowRevisionSummary
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { WORKSHOP_DESTINATION } from "../../src/lib/workshop";
import { cockpitApiStub } from "../support/cockpitApi";
import { notCancellableBlock } from "../support/runV3";
import { revisionHash, startedRun } from "../support/runV3";

/**
 * REQ-UIQ-08: a surface that exceeds its interaction budget is a defect.
 * The tempo half of that budget, pinned here: a surface asks once for
 * what it shows — the number of Cockpit API requests when opening a
 * room does not grow with the number of its rows.
 *
 * A request is one CockpitApi method call. The later open puts more
 * rows on the same single list page, so a second page the list must
 * follow is not the variable. Counts, never milliseconds: a timing
 * assertion would lie under load.
 *
 * Workbench rows are STARTED living-shelf runs. Catalog rows are
 * admitted workflow tiles. History rows are finished V3 runs of one
 * workflow. A click into a row, a stream event after open, a
 * Workbench open-decision pin, the Run page, Settings, a question
 * sheet, and a createElement overlay are outside what this open
 * counts.
 *
 * Today's History asks getNodeDetail once per row; Catalog asks
 * getRevisionByName once per workflow tile. The sentence names them
 * and does not prove they ask once.
 */

const FEWER_ROWS = 2;
const MORE_ROWS = 5;

const NAMED_RESIDUAL_SURFACES = ["Catalog", "History"] as const;

type Room = "Workbench" | "Catalog" | "History";

type OpenCount = {
  surface: Room;
  rows: number;
  requests: number;
};

function isThenable(value: unknown): value is Promise<unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    "then" in value &&
    typeof (value as { then?: unknown }).then === "function"
  );
}

function countingCockpitApi(overrides: Partial<CockpitApi> = {}): {
  api: CockpitApi;
  requestCount: () => number;
  idle: () => boolean;
} {
  const inner = cockpitApiStub(overrides);
  let requests = 0;
  let inFlight = 0;
  const api = new Proxy(inner, {
    get(target, property, receiver) {
      const value = Reflect.get(target, property, receiver) as unknown;
      if (typeof property !== "string" || typeof value !== "function") return value;
      return (...args: unknown[]) => {
        requests += 1;
        const result = (value as (...params: unknown[]) => unknown).apply(target, args);
        if (!isThenable(result)) return result;
        inFlight += 1;
        return Promise.resolve(result).finally(() => {
          inFlight -= 1;
        });
      };
    }
  }) as CockpitApi;
  return { api, requestCount: () => requests, idle: () => inFlight === 0 };
}

function growthLine(fewer: OpenCount, more: OpenCount): string | null {
  if (fewer.surface !== more.surface) {
    throw new Error(`compared ${fewer.surface} with ${more.surface}`);
  }
  if (more.rows <= fewer.rows) {
    throw new Error("the later open must have more rows");
  }
  if (more.requests <= fewer.requests) return null;
  return `${fewer.surface}: ${fewer.rows} rows → ${fewer.requests} requests; ${more.rows} rows → ${more.requests} requests`;
}

function surfaceOf(line: string): string {
  const cut = line.indexOf(":");
  return cut === -1 ? line : line.slice(0, cut);
}

function pathOf(room: Room): string {
  if (room === "Workbench") return WORKSHOP_DESTINATION.workbench.path;
  if (room === "Catalog") return WORKSHOP_DESTINATION.catalog.path;
  return WORKSHOP_DESTINATION.history.path;
}

function visibleRowCount(room: Room): number {
  if (room === "History") return document.querySelectorAll(".history-row").length;
  if (room === "Catalog") return document.querySelectorAll("li.catalog-tile").length;
  return document.querySelectorAll("a.living-row").length;
}

function hexId(index: number, fill: string): string {
  return index.toString(16).padStart(64, fill);
}

function minutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString();
}

function historyRun(index: number): RunV3 {
  const runId = `history-${index}`;
  return {
    workflow_format_version: 3,
    run_id: runId,
    public_run_reference: encodePublicRunReference(runId),
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    state_version: 1,
    state: "COMPLETED",
    current_node_id: "final",
    node_rail: [{ node_id: "final", state: "succeeded", attempt: null }],
    cancellation: notCancellableBlock("already-ended"),
    terminal_hash: revisionHash,
    latest_event_cursor: null,
    started_at: minutesAgo(38 + index),
    ended_at: minutesAgo(index),
    current_node_execution_id: revisionHash
  };
}

function historyRevision(): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: revisionHash,
    document_base64: "",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: ["builder"],
      orders: [],
      wait_answer_schemas: [],
      node_previews: [
        { id: "final", kind: "agent", role: "builder", instruction_start: "Do the one thing.", depends_on: [] }
      ],
      loops: [],
      name: "Two agents in a line",
      description: null
    }
  };
}

function historyNodeDetail(run: RunV3): NodeDetail {
  return {
    run_id: run.run_id,
    public_run_reference: run.public_run_reference,
    node_id: "final",
    state: "succeeded",
    job_base64: btoa("job"),
    job_hash: "e".repeat(64),
    answer: { value_base64: btoa('{"answer":"ok"}'), value_hash: "f".repeat(64) },
    provenance: null,
    refusal: null,
    refusal_output: null
  };
}

function catalogRevision(index: number): WorkflowRevisionSummary {
  return {
    workflow_revision_hash: hexId(index, "0"),
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name: `wf${index}`,
    description: "build"
  };
}

function historyApi(rows: number): Partial<CockpitApi> {
  const runs = Array.from({ length: rows }, (_, index) => historyRun(index));
  const byReference = new Map(runs.map((run) => [run.public_run_reference, run]));
  return {
    listRuns: vi.fn(async (_after?: string, state?: RunV3["state"]) => ({
      items: state === "COMPLETED" ? runs : [],
      next_after: null
    })),
    getWorkflowRevision: vi.fn(async () => historyRevision()),
    getNodeDetail: vi.fn(async (publicReference: string) => {
      const run = byReference.get(publicReference);
      if (run === undefined) throw new Error(`no history run ${publicReference}`);
      return historyNodeDetail(run);
    })
  };
}

function catalogApi(rows: number): Partial<CockpitApi> {
  const items = Array.from({ length: rows }, (_, index) => catalogRevision(index));
  return {
    listWorkflowRevisions: vi.fn(async () => ({
      items,
      next_after_revision_hash: null
    })),
    getRevisionByName: vi.fn(async (name: string) => {
      const item = items.find((revision) => revision.name === name);
      if (item === undefined || item.name === null) throw new Error(`no catalog name ${name}`);
      return {
        display_name: item.name,
        lineage_id: hexId(items.indexOf(item), "e"),
        workflow_revision_hash: item.workflow_revision_hash,
        revision_number: 1
      };
    })
  };
}

function workbenchApi(rows: number): Partial<CockpitApi> {
  const runs = Array.from({ length: rows }, (_, index) => {
    const runId = `shelf-${index}`;
    return startedRun({
      run_id: runId,
      public_run_reference: encodePublicRunReference(runId)
    });
  });
  return {
    listRuns: vi.fn(async (_after?: string, state?: RunV3["state"]) => ({
      items: state === "STARTED" ? runs : [],
      next_after: null
    }))
  };
}

function apiFor(room: Room, rows: number): Partial<CockpitApi> {
  if (room === "History") return historyApi(rows);
  if (room === "Catalog") return catalogApi(rows);
  return workbenchApi(rows);
}

async function measureOpen(room: Room, rows: number): Promise<OpenCount> {
  const counting = countingCockpitApi(apiFor(room, rows));
  window.history.replaceState(null, "", pathOf(room));
  render(App, {
    props: {
      cockpitApi: counting.api,
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
  try {
    await waitFor(() => {
      expect(visibleRowCount(room)).toBe(rows);
      expect(counting.idle()).toBe(true);
    });
    return { surface: room, rows, requests: counting.requestCount() };
  } finally {
    cleanup();
  }
}

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  window.history.replaceState(null, "", "/atelier");
});

describe("a surface asks once for what it shows", () => {
  it("names the surface, row counts, and request counts when a later open asks more", () => {
    expect(
      growthLine(
        { surface: "History", rows: 2, requests: 8 },
        { surface: "History", rows: 5, requests: 11 }
      )
    ).toBe("History: 2 rows → 8 requests; 5 rows → 11 requests");
  });

  it("is silent when the request count stays put as the rows grow", () => {
    expect(
      growthLine(
        { surface: "Workbench", rows: 2, requests: 9 },
        { surface: "Workbench", rows: 5, requests: 9 }
      )
    ).toBeNull();
  });

  it("falls on History, which asks a node detail once per row", async () => {
    const fewer = await measureOpen("History", FEWER_ROWS);
    const more = await measureOpen("History", MORE_ROWS);
    expect(growthLine(fewer, more)).toBe(
      `History: ${FEWER_ROWS} rows → ${fewer.requests} requests; ${MORE_ROWS} rows → ${more.requests} requests`
    );
  });

  it("stays green on Workbench living-shelf rows, which ask the same number twice", async () => {
    const fewer = await measureOpen("Workbench", FEWER_ROWS);
    const more = await measureOpen("Workbench", MORE_ROWS);
    expect(more.requests).toBe(fewer.requests);
    expect(growthLine(fewer, more)).toBeNull();
  });

  it("proves(a-surface-asks-once-for-what-it-shows): opening a room does not grow API requests with its row count", async () => {
    const rooms: Room[] = ["Workbench", "Catalog", "History"];
    const violations: string[] = [];
    for (const room of rooms) {
      const line = growthLine(
        await measureOpen(room, FEWER_ROWS),
        await measureOpen(room, MORE_ROWS)
      );
      if (line !== null) violations.push(line);
    }
    expect(violations.map(surfaceOf).sort(), violations.join("\n")).toEqual([
      ...NAMED_RESIDUAL_SURFACES
    ]);
  });
});
