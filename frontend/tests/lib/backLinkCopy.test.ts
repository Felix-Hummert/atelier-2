import { describe, expect, it } from "vitest";

import { backLinkCopy, inAppRoomOrigin, runBackLink } from "../../src/lib/backLinkCopy";
import { cockpitRoute, runPath, workflowPath } from "../../src/lib/route";
import { WORKSHOP_DESTINATION } from "../../src/lib/workshop";
import { publicReference } from "../support/runV3";

describe("the trail back from a run", () => {
  it("leads an ended run with no in-app origin to History", () => {
    expect(runBackLink(true, null)).toEqual({
      label: backLinkCopy.history,
      path: WORKSHOP_DESTINATION.history.path
    });
  });

  it("leads a live run with no in-app origin to Workbench", () => {
    expect(runBackLink(false, null)).toEqual({
      label: backLinkCopy.workbench,
      path: WORKSHOP_DESTINATION.workbench.path
    });
  });

  it("leads an ended run that was opened from Catalog back to Catalog", () => {
    expect(runBackLink(true, WORKSHOP_DESTINATION.catalog)).toEqual({
      label: backLinkCopy.catalog,
      path: WORKSHOP_DESTINATION.catalog.path
    });
  });

  it("leads a live run that was opened from History back to History", () => {
    expect(runBackLink(false, WORKSHOP_DESTINATION.history)).toEqual({
      label: backLinkCopy.history,
      path: WORKSHOP_DESTINATION.history.path
    });
  });
});

describe("an in-app previous path is an origin only when it is a room", () => {
  it("treats a catalog workflow detail as Catalog", () => {
    expect(inAppRoomOrigin(cockpitRoute(WORKSHOP_DESTINATION.catalog.path))).toBe(WORKSHOP_DESTINATION.catalog);
    expect(inAppRoomOrigin(cockpitRoute(workflowPath("Preview door")))).toBe(WORKSHOP_DESTINATION.catalog);
  });

  it("does not treat another run path as an origin", () => {
    expect(inAppRoomOrigin(cockpitRoute(runPath(publicReference)))).toBeNull();
  });

  it("does not treat a leftover from=chat query as an origin", () => {
    expect(inAppRoomOrigin(cockpitRoute(`${runPath(publicReference)}?from=chat`))).toBeNull();
  });
});
