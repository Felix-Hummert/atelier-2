import { describe, expect, it, vi } from "vitest";

import type { RunV3, WorkflowRevisionDetail } from "../../src/api/client";
import {
  newestActivityFirst,
  newestReadOfEachRun,
  runActivityAt,
  workflowNamesOf
} from "../../src/lib/runList";
import {
  notCancellableBlock,
  publicReference,
  revisionHash,
  startedRun
} from "../support/runV3";

function v3Run(changes: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/two-agents",
    workflow_name: "Two agents in a line",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "review",
    node_rail: [{ node_id: "review", state: "working", attempt: null }],
    cancellation: notCancellableBlock("between-nodes"),
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: null,
    ...changes,
    current_node_execution_id: changes.current_node_execution_id ?? revisionHash
  };
}

function v3Revision(
  name: string,
  hash: string = revisionHash
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
      wait_answer_schemas: [],
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

describe("the project run list ranks by last known activity", () => {
  it("puts a later start before an earlier one, even when the durable list answered oldest first", () => {
    const older = v3Run({
      run_id: "older",
      public_run_reference: "run1.b2xkZXI",
      started_at: "2026-08-18T14:00:00Z"
    });
    const newer = v3Run({
      run_id: "newer",
      public_run_reference: "run1.bmV3ZXI",
      started_at: "2026-08-18T16:00:00Z"
    });

    expect(newestActivityFirst([older, newer]).map((run) => run.run_id)).toEqual(["newer", "older"]);
  });

  it("ranks a finished run by when it ended, not when it started", () => {
    const long = v3Run({
      run_id: "long",
      public_run_reference: "run1.bG9uZw",
      state: "COMPLETED",
      terminal_hash: "d".repeat(64),
      started_at: "2026-08-18T10:00:00Z",
      ended_at: "2026-08-18T12:00:00Z"
    });
    const short = v3Run({
      run_id: "short",
      public_run_reference: "run1.c2hvcnQ",
      state: "COMPLETED",
      terminal_hash: "e".repeat(64),
      started_at: "2026-08-18T11:00:00Z",
      ended_at: "2026-08-18T13:00:00Z"
    });

    expect(runActivityAt(long)).toBe("2026-08-18T12:00:00Z");
    expect(newestActivityFirst([long, short]).map((run) => run.run_id)).toEqual(["short", "long"]);
  });

  it("ranks a run the operator cancelled by when it ended, same as any other terminal run", () => {
    const cancelled = v3Run({
      run_id: "cancelled",
      public_run_reference: "run1.Y2FuY2VsbGVk",
      state: "CANCELLED",
      terminal_hash: "f".repeat(64),
      started_at: "2026-08-18T10:00:00Z",
      ended_at: "2026-08-18T11:00:00Z"
    });
    const later = v3Run({
      run_id: "later",
      public_run_reference: "run1.bGF0ZXI",
      started_at: "2026-08-18T12:00:00Z"
    });

    expect(runActivityAt(cancelled)).toBe("2026-08-18T11:00:00Z");
    expect(newestActivityFirst([cancelled, later]).map((run) => run.run_id)).toEqual([
      "later",
      "cancelled"
    ]);
  });

  it("leaves rows without a stamp in the durable order, behind every dated row", () => {
    const dated = v3Run({ run_id: "dated", public_run_reference: "run1.ZGF0ZWQ" });
    const firstUntimed = startedRun({ run_id: "first", public_run_reference: "run1.Zmlyc3Q" });
    const secondUntimed = startedRun({ run_id: "second", public_run_reference: "run1.c2Vjb25k" });

    expect(
      newestActivityFirst([firstUntimed, dated, secondUntimed]).map((run) => run.run_id)
    ).toEqual(["dated", "first", "second"]);
  });
});

describe("the project run list reads published workflow names", () => {
  it("maps a V3 revision hash to the name the published graph already answers", async () => {
    const readRevision = vi.fn(async () => v3Revision("Two agents in a line"));
    const names = await workflowNamesOf(
      [
        v3Run(),
        v3Run({ run_id: "same revision", public_run_reference: "run1.c2FtZQ" })
      ],
      readRevision
    );

    expect(names.get(revisionHash)).toBe("Two agents in a line");
    expect(readRevision).toHaveBeenCalledTimes(1);
  });

  it("refuses the whole name set when one of several revisions cannot be read", async () => {
    const missingHash = "d".repeat(64);
    const readRevision = vi.fn(async (hash: string) => {
      if (hash === missingHash) throw new Error("revision missing");
      return v3Revision("Two agents in a line", hash);
    });

    await expect(
      workflowNamesOf(
        [
          v3Run(),
          v3Run({
            run_id: "missing revision",
            public_run_reference: "run1.bWlzc2luZw",
            workflow_revision_hash: missingHash
          })
        ],
        readRevision
      )
    ).rejects.toThrow("revision missing");

    expect(readRevision).toHaveBeenCalledTimes(2);
  });

  it("refuses a non-V3 revision as the name of a V3 run", async () => {
    // No document may declare a retired format anymore (#901 slice 5), but the
    // durable layer could still theoretically answer a corrupted row; the
    // cast stands in for that -- the type system itself now refuses this shape.
    const nonV3Revision = {
      workflow_revision_hash: revisionHash,
      document_base64: "",
      graph: {
        workflow_format_version: 1,
        start_node_id: "final",
        nodes: [
          {
            type: "subworkflow",
            node_id: "final",
            operation: "add",
            operands: [2, 3],
            next_node_id: null
          }
        ]
      }
    } as unknown as WorkflowRevisionDetail;
    await expect(
      workflowNamesOf([v3Run()], async () => nonV3Revision)
    ).rejects.toThrow("a V3 run referenced a workflow revision of another format");
  });

  it("refuses to file a different revision under the hash a run named", async () => {
    await expect(
      workflowNamesOf(
        [v3Run()],
        async () => v3Revision("Different revision", "e".repeat(64))
      )
    ).rejects.toThrow("a V3 run received a different workflow revision");
  });
});

/**
 * A room that asks several state lists at one moment gets them answered
 * separately, so a run that moves between two answers comes back in both.
 */
describe("one entry per run, across separately answered reads", () => {
  it("keeps the higher state version when two reads answer with the same run", () => {
    const opened = v3Run({ state: "WAITING_INPUT", state_version: 2 });
    const earlier = v3Run({ state: "STARTED", state_version: 1 });

    expect(newestReadOfEachRun([earlier, opened])).toEqual([opened]);
    // Whichever read is drained first, the fresher truth is the one kept.
    expect(newestReadOfEachRun([opened, earlier])).toEqual([opened]);
  });

  it("keeps every distinct run, in the order it was first read", () => {
    const first = v3Run({ public_run_reference: "run1.YQ" });
    const second = v3Run({ public_run_reference: "run1.Yg" });

    expect(newestReadOfEachRun([first, second])).toEqual([first, second]);
  });

  it("answers an empty reading with an empty list rather than inventing a row", () => {
    expect(newestReadOfEachRun([])).toEqual([]);
  });
});
