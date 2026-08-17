import { describe, expect, it } from "vitest";

import type { WorkflowRevisionSummary } from "../../src/api/client";
import {
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
    revision_hash: hashChar.repeat(64),
    format_version: 3,
    executable: true,
    not_executable_reason: null,
    name,
    description: null,
    ...changes
  };
}

function unnamed(hashChar: string): WorkflowRevisionSummary {
  return {
    revision_hash: hashChar.repeat(64),
    format_version: 2,
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
      "drei-saetze-review-sehend": newest.revision_hash
    });

    expect(rows).toHaveLength(1);
    expect(rows[0]?.name).toBe("drei-saetze-review-sehend");
    expect(rows[0]?.revisions.map((item) => item.revision_hash)).toEqual([
      newest.revision_hash,
      older.revision_hash
    ]);
    expect(selectedRevisionOf(rows[0]!).revision_hash).toBe(newest.revision_hash);
  });

  it("does not invent a submenu for a name that has one revision", () => {
    const only = named("a", "one-lineage");

    const rows = groupSavedWorkflows([only], { "one-lineage": only.revision_hash });

    expect(rows).toHaveLength(1);
    expect(rows[0]?.revisions).toEqual([only]);
  });

  it("leaves unnamed documents as their own rows, never one unnamed pile", () => {
    const first = unnamed("a");
    const second = unnamed("b");

    const rows = groupSavedWorkflows([first, second]);

    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.revisions.map((item) => item.revision_hash))).toEqual([
      [first.revision_hash],
      [second.revision_hash]
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
    expect(selectedRevisionOf(rows[0]!).revision_hash).toBe(first.revision_hash);
  });

  it("follows an explicit selection instead of the default head", () => {
    const older = named("a", "shared");
    const newest = named("b", "shared");
    const row = groupSavedWorkflows([older, newest], { shared: newest.revision_hash })[0];

    expect(selectedRevisionOf(row!, older.revision_hash).revision_hash).toBe(
      older.revision_hash
    );
  });

  it("labels the catalog head as Latest and every other member as Earlier", () => {
    const older = named("a", "shared");
    const newest = named("b", "shared");

    expect(revisionChoiceLabel(newest, newest.revision_hash)).toBe("Latest");
    expect(revisionChoiceLabel(older, newest.revision_hash)).toBe("Earlier");
  });
});
