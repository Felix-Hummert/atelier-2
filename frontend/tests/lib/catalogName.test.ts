import { describe, expect, it, vi } from "vitest";

import { CockpitRequestError, type Problem } from "../../src/api/client";
import { catalogNameStateOf, isCatalogDisplayName } from "../../src/lib/catalogName";

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
    const ask = vi.fn(async () => ({ workflow_revision_hash: hash }));

    await expect(catalogNameStateOf("diff-review", ask)).resolves.toEqual({
      kind: "admitted",
      revisionHash: hash
    });
  });
});
