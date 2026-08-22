import { describe, expect, it, vi } from "vitest";

import { CockpitRequestError, type Problem, type WorkflowRevisionDetail } from "../../src/api/client";
import { admitPublishedRevision } from "../../src/lib/catalogAdmission";

const hash = "a".repeat(64);
const lineageId = "b".repeat(64);
const activatedAt = "2026-08-18T07:00:00Z";
const actor = "atelier2-cockpit";

function problem(code: string, status: number): Problem {
  return {
    type: `urn:atelier2:problem:v1:${code}`,
    title: code,
    status,
    detail: code
  } as Problem;
}

function v3Revision(name: string): WorkflowRevisionDetail {
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
          id: "implement",
          kind: "agent",
          role: "builder",
          instruction_start: "Do the work.",
          depends_on: []
        }
      ],
      loops: [],
      name,
      description: null
    }
  };
}

describe("admitting a published V3 revision", () => {
  it("founds a legal name through the existing door", async () => {
    const foundCatalogLineage = vi.fn(async () => ({
      status: 201,
      value: {
        display_name: "diff-review",
        lineage_id: lineageId,
        workflow_revision_hash: hash,
        revision_number: 1
      }
    }));
    const api = {
      foundCatalogLineage,
      admitCatalogMember: vi.fn(),
      getRevisionByName: vi.fn()
    };

    await admitPublishedRevision(api, v3Revision("diff-review"), actor, activatedAt);

    expect(foundCatalogLineage).toHaveBeenCalledWith({
      workflow_revision_hash: hash,
      actor,
      activated_at: activatedAt
    });
    expect(api.admitCatalogMember).not.toHaveBeenCalled();
  });

  it("does not found an uncatalogable published title", async () => {
    const api = {
      foundCatalogLineage: vi.fn(),
      admitCatalogMember: vi.fn(),
      getRevisionByName: vi.fn()
    };

    await admitPublishedRevision(
      api,
      v3Revision("Der erste Lauf auf V14"),
      actor,
      activatedAt
    );

    expect(api.foundCatalogLineage).not.toHaveBeenCalled();
  });

  it("admits a later revision into the lineage that already holds the name", async () => {
    const api = {
      foundCatalogLineage: vi.fn(async () => {
        throw new CockpitRequestError("held", problem("catalog-name-held", 409), true);
      }),
      getRevisionByName: vi.fn(async () => ({
        display_name: "diff-review",
        lineage_id: lineageId,
        workflow_revision_hash: "c".repeat(64),
        revision_number: 1
      })),
      admitCatalogMember: vi.fn(async () => ({
        status: 201,
        value: {
          display_name: "diff-review",
          lineage_id: lineageId,
          workflow_revision_hash: hash,
          revision_number: 2
        }
      }))
    };

    await admitPublishedRevision(api, v3Revision("diff-review"), actor, activatedAt);

    expect(api.getRevisionByName).toHaveBeenCalledWith("diff-review");
    expect(api.admitCatalogMember).toHaveBeenCalledWith(lineageId, {
      workflow_revision_hash: hash,
      actor,
      activated_at: activatedAt
    });
  });
});
