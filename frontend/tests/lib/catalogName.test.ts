import { describe, expect, it, vi } from "vitest";

import { CockpitRequestError, type Problem } from "../../src/api/client";
import type { WorkflowRevisionSummary } from "../../src/api/client";
import { catalogHeadsOf, catalogNameStateOf, isCatalogDisplayName } from "../../src/lib/catalogName";

function problem(code: string): Problem {
  return {
    type: `urn:atelier2:problem:v1:${code}`,
    title: code,
    status: code === "catalog-lineage-retired" ? 410 : 404,
    detail: code
  } as Problem;
}

describe("the catalog display-name grammar the picker reads", () => {
  it("accepts a legal authored name and refuses the live #305 title", () => {
    expect(isCatalogDisplayName("diff-review")).toBe(true);
    expect(isCatalogDisplayName("drei-saetze-review-sehend")).toBe(true);
    expect(isCatalogDisplayName("Der erste Lauf auf V14")).toBe(false);
    expect(isCatalogDisplayName("a".repeat(64))).toBe(false);
  });
});

describe("what a published name is to the catalog", () => {
  it("names an illegal title without asking the catalog", async () => {
    const ask = vi.fn();

    await expect(catalogNameStateOf("Der erste Lauf auf V14", ask)).resolves.toEqual({
      kind: "unnamable"
    });
    expect(ask).not.toHaveBeenCalled();
  });

  it("names a legal missing name as unlisted", async () => {
    const ask = vi.fn(async () => {
      throw new CockpitRequestError("missing", problem("catalog-name-not-found"), true);
    });

    await expect(catalogNameStateOf("diff-review", ask)).resolves.toEqual({
      kind: "unlisted"
    });
    expect(ask).toHaveBeenCalledWith("diff-review");
  });

  it("keeps an admitted name as the catalog head", async () => {
    const hash = "a".repeat(64);
    const lineageId = "b".repeat(64);
    const ask = vi.fn(async () => ({
      display_name: "diff-review",
      lineage_id: lineageId,
      workflow_revision_hash: hash,
      revision_number: 1
    }));

    await expect(catalogNameStateOf("diff-review", ask)).resolves.toEqual({
      kind: "admitted",
      revisionHash: hash,
      lineageId
    });
  });
});

describe("the complete catalog head snapshot", () => {
  const revision = (name: string, hash: string): WorkflowRevisionSummary => ({
    workflow_revision_hash: hash,
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name,
    description: null
  });

  it("refuses an admitted head that the full list does not carry under its exact name", () => {
    const hash = "a".repeat(64);
    expect(catalogHeadsOf([revision("other", hash)], {
      named: { kind: "admitted", revisionHash: hash, lineageId: "b".repeat(64) }
    })).toBeNull();
    expect(catalogHeadsOf([revision("named", "c".repeat(64))], {
      named: { kind: "admitted", revisionHash: hash, lineageId: "b".repeat(64) }
    })).toBeNull();
  });

  it("returns only exact admitted heads", () => {
    const hash = "a".repeat(64);
    expect(catalogHeadsOf([revision("named", hash)], {
      named: { kind: "admitted", revisionHash: hash, lineageId: "b".repeat(64) },
      retired: { kind: "retired" }
    })).toEqual({ named: hash });
  });
});
