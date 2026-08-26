import { describe, expect, it } from "vitest";
import {
  cockpitRoute,
  PUBLIC_REFERENCE_PLACEHOLDER,
  runPath,
  SERVED_PATHS,
  WORKFLOW_NAME_PLACEHOLDER,
  workflowPath,
  type CockpitRoute
} from "../../src/lib/route";

const SAMPLE_PUBLIC_REFERENCE = "run1.cnVu";
const SAMPLE_WORKFLOW_NAME = "iterate-code";

/**
 * Every page this router can open, and whether a served path must reach it on a
 * cold load. Total over the router's own union, so a page added there without a
 * decision here is a type error rather than a level that survives a click and
 * dies on a reload -- the defect this file exists for, one edge further out.
 */
const REACHED_COLD: Record<CockpitRoute["page"], boolean> = {
  workbench: true,
  settings: true,
  new: true,
  workflow: true,
  catalog: true,
  history: true,
  run: true,
  "not-found": false
};

function coldLoad(servedPath: string): CockpitRoute {
  return cockpitRoute(
    servedPath
      .replace(PUBLIC_REFERENCE_PLACEHOLDER, SAMPLE_PUBLIC_REFERENCE)
      .replace(WORKFLOW_NAME_PLACEHOLDER, SAMPLE_WORKFLOW_NAME)
  );
}

describe("the paths the server is asked to serve", () => {
  /**
   * The declaration is only worth reading if it cannot promise a path this
   * router would answer with "not found". Together with the host test that
   * serves exactly this file, a level is either reachable cold from both sides
   * or red on one of them.
   */
  it.each([...SERVED_PATHS])(
    "proves(a-level-opens-from-a-pasted-link-and-survives-a-reload): %s opens a page rather than nothing",
    (servedPath) => {
      expect(coldLoad(servedPath).page).not.toBe("not-found");
    }
  );

  it("declares a path for every page this router can open", () => {
    const opened = new Set(SERVED_PATHS.map((servedPath) => coldLoad(servedPath).page));

    expect([...opened].sort()).toEqual(
      Object.entries(REACHED_COLD)
        .filter(([, reached]) => reached)
        .map(([page]) => page)
        .sort()
    );
  });

  /**
   * The defect this file was written for: the project level was a page here and
   * a 404 on the server, so the level the workshop makes its primary answer
   * survived a click and died on a reload.
   */
  it("opens the Settings room on the project path it grew from", () => {
    expect(SERVED_PATHS).toContain("/atelier/project");
    expect(cockpitRoute("/atelier/project")).toEqual({ page: "settings" });
  });

  it("opens the Workbench on the workshop root, where the Board used to stand", () => {
    expect(SERVED_PATHS).toContain("/atelier");
    expect(cockpitRoute("/atelier")).toEqual({ page: "workbench" });
    expect(cockpitRoute("/atelier/chat")).toEqual({ page: "workbench" });
  });

  it("still answers an unknown path with not-found", () => {
    expect(cockpitRoute("/atelier/nowhere").page).toBe("not-found");
  });

  // The Workflows room folded into the Catalog (ADR 0019 §1). Its address
  // keeps working rather than turning a living bookmark into a not-found page.
  it("opens the Catalog on its own path and on the address the Workflows room left behind", () => {
    expect(SERVED_PATHS).toContain("/atelier/workflows");
    expect(cockpitRoute("/atelier/catalog")).toEqual({ page: "catalog" });
    expect(cockpitRoute("/atelier/workflows")).toEqual({ page: "catalog" });
  });

  it("opens History on its own path, separate from the old project level", () => {
    expect(SERVED_PATHS).toContain("/atelier/history");
    expect(cockpitRoute("/atelier/history")).toEqual({ page: "history" });
    expect(cockpitRoute("/atelier/project")).toEqual({ page: "settings" });
  });

  it("round-trips a workflow name a person actually publishes, spaces included", () => {
    const name = "Probefahrt am frischen Haus";

    expect(cockpitRoute(workflowPath(name))).toEqual({ page: "workflow", name });
  });

  it("answers a malformed percent-encoding in a workflow path with not-found", () => {
    expect(cockpitRoute("/atelier/workflows/%").page).toBe("not-found");
  });
});

describe("a run's own address", () => {
  it("opens the run page from the link every room builds", () => {
    expect(cockpitRoute(runPath(SAMPLE_PUBLIC_REFERENCE))).toEqual({
      page: "run",
      publicReference: SAMPLE_PUBLIC_REFERENCE
    });
  });

  // Links carrying the old "?from=chat" marker are still in browser histories;
  // they open the same run rather than a broken page.
  it("ignores a query a bookmarked link still carries", () => {
    expect(cockpitRoute(`/atelier/runs/${SAMPLE_PUBLIC_REFERENCE}?from=chat`)).toEqual({
      page: "run",
      publicReference: SAMPLE_PUBLIC_REFERENCE
    });
  });
});
