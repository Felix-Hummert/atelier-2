import { describe, expect, it } from "vitest";
import {
  cockpitRoute,
  PUBLIC_REFERENCE_PLACEHOLDER,
  SERVED_PATHS,
  type CockpitRoute
} from "../../src/lib/route";

const SAMPLE_PUBLIC_REFERENCE = "run1.cnVu";

function coldLoad(servedPath: string): CockpitRoute {
  return cockpitRoute(servedPath.replace(PUBLIC_REFERENCE_PLACEHOLDER, SAMPLE_PUBLIC_REFERENCE));
}

describe("the paths the server is asked to serve", () => {
  /**
   * The declaration is only worth reading if it cannot promise a path this
   * router would answer with "not found". Together with the host test that
   * serves exactly this file, a level is either reachable cold from both sides
   * or red on one of them.
   */
  it.each([...SERVED_PATHS])("%s opens a page rather than nothing", (servedPath) => {
    expect(coldLoad(servedPath).page).not.toBe("not-found");
  });

  it("declares a path for every page this router can open", () => {
    const opened = new Set(SERVED_PATHS.map((servedPath) => coldLoad(servedPath).page));

    expect([...opened].sort()).toEqual(["new", "project", "run", "studio"]);
  });

  /**
   * The defect this file was written for: the project level was a page here and
   * a 404 on the server, so the level the workshop makes its primary answer
   * survived a click and died on a reload.
   */
  it("opens the project level on the canonical project path", () => {
    expect(SERVED_PATHS).toContain("/atelier/project");
    expect(cockpitRoute("/atelier/project")).toEqual({ page: "project" });
  });

  it("still answers an unknown path with not-found", () => {
    expect(cockpitRoute("/atelier/nowhere").page).toBe("not-found");
  });
});
