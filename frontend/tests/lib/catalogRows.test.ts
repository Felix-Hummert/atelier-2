import { describe, expect, it } from "vitest";

import type {
  AgentDefinitionRevisionListItem,
  WorkflowRevisionSummary
} from "../../src/api/client";
import type { CatalogNameState } from "../../src/lib/catalogName";
import { catalogPageCopy } from "../../src/lib/catalogPageCopy";
import {
  catalogAgentRows,
  catalogRowFacts,
  catalogWorkflowRows
} from "../../src/lib/catalogRows";

const HASH = "b".repeat(64);
const OTHER_HASH = "c".repeat(64);
const NAME = "iterate-code";

function summary(overrides: Partial<WorkflowRevisionSummary> = {}): WorkflowRevisionSummary {
  return {
    workflow_revision_hash: HASH,
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name: NAME,
    description: "build, then review",
    ...overrides
  };
}

function admitted(revisionHash: string): Record<string, CatalogNameState> {
  return { [NAME]: { kind: "admitted", revisionHash, lineageId: "e".repeat(64) } };
}

describe("what the catalog says about a workflow", () => {
  it("calls the catalog's own head startable", () => {
    const [row] = catalogWorkflowRows([summary()], admitted(HASH));

    expect(row?.state).toEqual({ kind: "startable" });
    expect(row?.title).toBe(NAME);
  });

  it("does not call a revision startable because a sibling holds its name", () => {
    const [row] = catalogWorkflowRows([summary()], admitted(OTHER_HASH));

    expect(row?.state).toEqual({ kind: "not-admitted" });
  });

  it("calls a revision no lineage holds not admitted yet", () => {
    const [row] = catalogWorkflowRows([summary()], {});

    expect(row?.state).toEqual({ kind: "not-admitted" });
    expect(row?.admittable).toBe(true);
  });

  it("names why a revision cannot run, in the words a person can act on", () => {
    const [row] = catalogWorkflowRows(
      [
        summary({
          executable: false,
          not_executable_reason: "agent forms nothing binds yet: outputs"
        })
      ],
      {}
    );

    expect(row?.state?.kind).toBe("not-executable");
    expect(row?.state?.kind === "not-executable" && row.state.reason).toContain(
      "declares no output"
    );
  });

  it("offers no admission for a revision that cannot run", () => {
    const [row] = catalogWorkflowRows(
      [summary({ executable: false, not_executable_reason: "not one line" })],
      {}
    );

    expect(row?.admittable).toBe(false);
  });

  it("names a revision without a title rather than showing its hash as one", () => {
    const [row] = catalogWorkflowRows([summary({ name: null })], {});

    expect(row?.title).toBe(catalogPageCopy.unnamedWorkflow);
    expect(row?.name).toBeNull();
    expect(row?.admittable).toBe(false);
  });

  it("carries the document's own declared name for the Details door to link with", () => {
    const [row] = catalogWorkflowRows([summary()], {});

    expect(row?.name).toBe(NAME);
  });

  it("claims nothing about a nameless revision the catalog can only answer by name", () => {
    const [row] = catalogWorkflowRows([summary({ name: null })], {});

    expect(row?.state).toBeNull();
  });

  it("still names why a nameless revision cannot run, which needs no name", () => {
    const [row] = catalogWorkflowRows(
      [summary({ name: null, executable: false, not_executable_reason: "not one line" })],
      {}
    );

    expect(row?.state?.kind).toBe("not-executable");
  });

  it("offers no admission for a title the catalog grammar refuses", () => {
    const [row] = catalogWorkflowRows([summary({ name: "Iterate Code" })], {});

    expect(row?.admittable).toBe(false);
  });
});

describe("grouping revisions of one lineage into one card", () => {
  it("collapses an unadmitted sibling into the admitted card's own note, not a second card", () => {
    const rows = catalogWorkflowRows(
      [summary({ workflow_revision_hash: HASH }), summary({ workflow_revision_hash: OTHER_HASH })],
      admitted(HASH)
    );

    expect(rows).toHaveLength(1);
    expect(rows[0]?.revisionHash).toBe(HASH);
    expect(rows[0]?.state).toEqual({ kind: "startable" });
    expect(rows[0]?.newerRevisionAvailable).toBe(true);
  });

  it("carries no newer-revision note when the admitted name has no sibling", () => {
    const [row] = catalogWorkflowRows([summary({ workflow_revision_hash: HASH })], admitted(HASH));

    expect(row?.newerRevisionAvailable).toBe(false);
  });

  it("keeps every revision its own card before the name has an admitted head", () => {
    const rows = catalogWorkflowRows(
      [summary({ workflow_revision_hash: HASH }), summary({ workflow_revision_hash: OTHER_HASH })],
      {}
    );

    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.state)).toEqual([
      { kind: "not-admitted" },
      { kind: "not-admitted" }
    ]);
    expect(rows.every((row) => row.newerRevisionAvailable === false)).toBe(true);
  });

  it("does not group two different names into one card", () => {
    const rows = catalogWorkflowRows(
      [summary({ name: "iterate-code" }), summary({ name: "iterate-docs", workflow_revision_hash: OTHER_HASH })],
      {}
    );

    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.title)).toEqual(["iterate-code", "iterate-docs"]);
  });
});

describe("what a retired lineage leaves on the shelf", () => {
  const LIVE_NAME = "iterate-docs";
  const LIVE_HASH = "d".repeat(64);

  it("shows the live lineage and no revision of the retired one", () => {
    const rows = catalogWorkflowRows(
      [
        summary(),
        summary({ workflow_revision_hash: OTHER_HASH }),
        summary({ name: LIVE_NAME, workflow_revision_hash: LIVE_HASH })
      ],
      { [NAME]: { kind: "retired" } }
    );

    expect(rows.map((row) => row.name)).toEqual([LIVE_NAME]);
  });
});

describe("what the catalog says about an agent", () => {
  function item(
    overrides: Partial<AgentDefinitionRevisionListItem> = {}
  ): AgentDefinitionRevisionListItem {
    return {
      agent_definition_revision_hash: HASH,
      name: "scribe",
      description: "Writes what the stage needs.",
      ...overrides
    };
  }

  it("names the provider an imported agent belongs to", () => {
    const [row] = catalogAgentRows([item()]);

    expect(row?.provider).toBe(catalogPageCopy.agentProviderClaude);
  });

  it("says only what the file authored, so no row can read as portable", () => {
    const [row] = catalogAgentRows([item()]);

    expect(row).toEqual({
      revisionHash: HASH,
      title: "scribe",
      description: "Writes what the stage needs.",
      provider: catalogPageCopy.agentProviderClaude
    });
  });
});

describe("the facts under an entry's name", () => {
  it("names provenance without putting a machine fingerprint on the card", () => {
    expect(catalogRowFacts()).toEqual([catalogPageCopy.provenanceManual]);
  });
});
