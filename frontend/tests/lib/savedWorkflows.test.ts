import { describe, expect, it } from "vitest";

import type { WorkflowRevisionDetail, WorkflowRevisionSummary } from "../../src/api/client";
import {
  agentRolesOf,
  groupSavedWorkflows,
  revisionChoiceLabel,
  selectedRevisionOf
} from "../../src/lib/savedWorkflows";

function named(
  hashChar: string,
  name: string,
  changes: Partial<WorkflowRevisionSummary> = {}
): WorkflowRevisionSummary {
  return {
    workflow_revision_hash: hashChar.repeat(64),
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name,
    description: null,
    ...changes
  };
}

function unnamed(hashChar: string): WorkflowRevisionSummary {
  return {
    workflow_revision_hash: hashChar.repeat(64),
    workflow_format_version: 2,
    executable: true,
    not_executable_reason: null,
    name: null,
    description: null
  };
}

describe("grouping saved workflows by the name the listing already publishes", () => {
  it("keeps two revisions of one name as one row and puts the catalog head first", () => {
    const older = named("a", "drei-saetze-review-sehend");
    const newest = named("b", "drei-saetze-review-sehend", {
      executable: false,
      not_executable_reason: "agent forms nothing binds yet: outputs"
    });

    const rows = groupSavedWorkflows([older, newest], {
      "drei-saetze-review-sehend": newest.workflow_revision_hash
    });

    expect(rows).toHaveLength(1);
    expect(rows[0]?.name).toBe("drei-saetze-review-sehend");
    expect(rows[0]?.revisions.map((item) => item.workflow_revision_hash)).toEqual([
      newest.workflow_revision_hash,
      older.workflow_revision_hash
    ]);
    expect(selectedRevisionOf(rows[0]!).workflow_revision_hash).toBe(newest.workflow_revision_hash);
  });

  it("does not invent a submenu for a name that has one revision", () => {
    const only = named("a", "one-lineage");

    const rows = groupSavedWorkflows([only], { "one-lineage": only.workflow_revision_hash });

    expect(rows).toHaveLength(1);
    expect(rows[0]?.revisions).toEqual([only]);
  });

  it("leaves unnamed documents as their own rows, never one unnamed pile", () => {
    const first = unnamed("a");
    const second = unnamed("b");

    const rows = groupSavedWorkflows([first, second]);

    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.revisions.map((item) => item.workflow_revision_hash))).toEqual([
      [first.workflow_revision_hash],
      [second.workflow_revision_hash]
    ]);
    expect(rows.every((row) => row.name === null)).toBe(true);
  });

  it("keeps two different names as two rows", () => {
    const first = named("a", "alpha");
    const second = named("b", "beta");

    expect(groupSavedWorkflows([first, second]).map((row) => row.name)).toEqual([
      "alpha",
      "beta"
    ]);
  });

  it("still groups a shared name when the catalog did not name a head", () => {
    const first = named("a", "shared");
    const second = named("b", "shared");

    const rows = groupSavedWorkflows([first, second]);

    expect(rows).toHaveLength(1);
    expect(rows[0]?.revisions).toEqual([first, second]);
    expect(selectedRevisionOf(rows[0]!).workflow_revision_hash).toBe(first.workflow_revision_hash);
  });

  it("follows an explicit selection instead of the default head", () => {
    const older = named("a", "shared");
    const newest = named("b", "shared");
    const row = groupSavedWorkflows([older, newest], { shared: newest.workflow_revision_hash })[0];

    expect(selectedRevisionOf(row!, older.workflow_revision_hash).workflow_revision_hash).toBe(
      older.workflow_revision_hash
    );
  });

  it("labels the catalog head as Latest and every other member as Earlier", () => {
    const older = named("a", "shared");
    const newest = named("b", "shared");

    expect(revisionChoiceLabel(newest, newest.workflow_revision_hash)).toBe("Latest");
    expect(revisionChoiceLabel(older, newest.workflow_revision_hash)).toBe("Earlier");
  });
});

describe("the authored agent roles a second consumer may edit", () => {
  it("keeps only real V2/V3 roles once and never invents one for V1", () => {
    const graph = (workflow_format_version: 1 | 2 | 3, roles: string[]) => ({
      workflow_format_version,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: roles,
      orders: [],
      node_previews: [],
      loops: [],
      name: "roles",
      description: null
    }) as WorkflowRevisionDetail["graph"];

    expect(agentRolesOf(graph(3, ["builder", "reviewer", "builder"]))).toEqual(["builder", "reviewer"]);
    expect(agentRolesOf({
      workflow_format_version: 2,
      start_node_id: "one",
      nodes: [
        { type: "agent", node_id: "one", role: "builder", job: "one", next_node_id: "two" },
        { type: "agent", node_id: "two", role: "builder", job: "two", next_node_id: "done" },
        { type: "subworkflow", node_id: "done", operation: "add", operands: [1, 2], next_node_id: null }
      ]
    })).toEqual(["builder"]);
    expect(agentRolesOf({
      workflow_format_version: 1,
      start_node_id: "done",
      nodes: [{ type: "subworkflow", node_id: "done", operation: "add", operands: [1, 2], next_node_id: null }]
    })).toEqual([]);
  });
});
