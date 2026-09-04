import { describe, expect, it, vi } from "vitest";

import {
  CockpitRequestError,
  type CatalogLineageKind,
  type Problem,
  type WorkflowRevisionDetail
} from "../../src/api/client";
import { admitPublishedRevision, handCatalogDocumentIn } from "../../src/lib/catalogAdmission";

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

function admission(displayName: string, revisionNumber: number) {
  return {
    status: 201,
    value: {
      display_name: displayName,
      lineage_id: lineageId,
      catalog_revision_hash: hash,
      revision_number: revisionNumber
    }
  };
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
      wait_answer_schemas: [],
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

const KINDS: readonly CatalogLineageKind[] = ["workflow", "agent_definition"];

describe.each(KINDS)("admitting a published %s revision", (kind) => {
  it("founds a legal name through the one lineage door", async () => {
    const foundCatalogLineage = vi.fn(async () => admission("diff-review", 1));
    const api = {
      foundCatalogLineage,
      admitCatalogMember: vi.fn(),
      getRevisionByName: vi.fn()
    };

    await admitPublishedRevision(api, kind, hash, "diff-review", actor, activatedAt);

    expect(foundCatalogLineage).toHaveBeenCalledWith({
      kind,
      catalog_revision_hash: hash,
      actor,
      activated_at: activatedAt
    });
    expect(api.admitCatalogMember).not.toHaveBeenCalled();
  });

  it("does not found an uncatalogable authored name", async () => {
    const api = {
      foundCatalogLineage: vi.fn(),
      admitCatalogMember: vi.fn(),
      getRevisionByName: vi.fn()
    };

    await admitPublishedRevision(
      api,
      kind,
      hash,
      "Der erste Lauf auf V14",
      actor,
      activatedAt
    );

    expect(api.foundCatalogLineage).not.toHaveBeenCalled();
  });

  it("admits a later revision into the lineage of the same kind that holds the name", async () => {
    const api = {
      foundCatalogLineage: vi.fn(async () => {
        throw new CockpitRequestError("held", problem("catalog-name-held", 409), true);
      }),
      getRevisionByName: vi.fn(async () => ({
        display_name: "diff-review",
        lineage_id: lineageId,
        catalog_revision_hash: "c".repeat(64),
        revision_number: 1
      })),
      admitCatalogMember: vi.fn(async () => admission("diff-review", 2))
    };

    await admitPublishedRevision(api, kind, hash, "diff-review", actor, activatedAt);

    expect(api.getRevisionByName).toHaveBeenCalledWith(kind, "diff-review");
    expect(api.admitCatalogMember).toHaveBeenCalledWith(lineageId, {
      kind,
      catalog_revision_hash: hash,
      actor,
      activated_at: activatedAt
    });
  });
});

describe("handing a catalog document in", () => {
  const document = new TextEncoder().encode("format_version: 3\nname: import-proof\n");

  it("stores the declared kind and publishes a workflow into the catalog", async () => {
    const addLibraryDocument = vi.fn(async () => ({
      status: 201,
      value: { intake_id: hash, kind: "workflow" as const }
    }));
    const publish = vi.fn(async () => ({ status: 201, value: v3Revision("import-proof") }));
    const foundCatalogLineage = vi.fn(async () => admission("import-proof", 1));
    const api = {
      addLibraryDocument,
      publish,
      publishAgentDefinition: vi.fn(),
      getAgentDefinitionRevision: vi.fn(),
      foundCatalogLineage,
      admitCatalogMember: vi.fn(),
      getRevisionByName: vi.fn()
    };

    await handCatalogDocumentIn(api, document, "workflow", actor, activatedAt);

    expect(addLibraryDocument).toHaveBeenCalledWith(document, "workflow", actor, activatedAt);
    expect(publish).toHaveBeenCalledOnce();
    expect(foundCatalogLineage).toHaveBeenCalledWith({
      kind: "workflow",
      catalog_revision_hash: hash,
      actor,
      activated_at: activatedAt
    });
    expect(api.publishAgentDefinition).not.toHaveBeenCalled();
  });

  it("gives a stored agent the lineage its frontmatter name authored", async () => {
    const addLibraryDocument = vi.fn(async () => ({
      status: 201,
      value: { intake_id: hash, kind: "agent" as const }
    }));
    const publishAgentDefinition = vi.fn(async () => ({
      status: 201,
      value: { agent_definition_revision_hash: hash }
    }));
    const foundCatalogLineage = vi.fn(async () => admission("diff-reviewer", 1));
    const api = {
      addLibraryDocument,
      publish: vi.fn(),
      publishAgentDefinition,
      getAgentDefinitionRevision: vi.fn(async () => ({
        agent_definition_revision_hash: hash,
        name: "diff-reviewer",
        description: "Reviews one bounded diff.",
        system_prompt: "Review it."
      })),
      foundCatalogLineage,
      admitCatalogMember: vi.fn(),
      getRevisionByName: vi.fn()
    };

    await handCatalogDocumentIn(api, document, "agent", actor, activatedAt);

    expect(addLibraryDocument).toHaveBeenCalledWith(document, "agent", actor, activatedAt);
    expect(publishAgentDefinition).toHaveBeenCalledWith(new TextDecoder().decode(document));
    expect(api.publish).not.toHaveBeenCalled();
    expect(foundCatalogLineage).toHaveBeenCalledWith({
      kind: "agent_definition",
      catalog_revision_hash: hash,
      actor,
      activated_at: activatedAt
    });
  });

  it("stores a skill without publishing", async () => {
    const addLibraryDocument = vi.fn(async () => ({
      status: 201,
      value: { intake_id: hash, kind: "skill" as const }
    }));
    const api = {
      addLibraryDocument,
      publish: vi.fn(),
      publishAgentDefinition: vi.fn(),
      getAgentDefinitionRevision: vi.fn(),
      foundCatalogLineage: vi.fn(),
      admitCatalogMember: vi.fn(),
      getRevisionByName: vi.fn()
    };

    await handCatalogDocumentIn(api, document, "skill", actor, activatedAt);

    expect(addLibraryDocument).toHaveBeenCalledWith(document, "skill", actor, activatedAt);
    expect(api.publish).not.toHaveBeenCalled();
    expect(api.publishAgentDefinition).not.toHaveBeenCalled();
    expect(api.foundCatalogLineage).not.toHaveBeenCalled();
  });
});
