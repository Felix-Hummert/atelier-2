import { describe, expect, it } from "vitest";

import { WORKSHOP_DESTINATIONS, activeWorkshopDestination } from "../../src/lib/workshop";

describe("the workshop rail names four destinations", () => {
  it("opens a page for every destination it names, so no rail item is a dead click", () => {
    expect(
      WORKSHOP_DESTINATIONS.map((destination) => [destination.label, destination.path])
    ).toEqual([
      ["Workbench", "/atelier/chat"],
      ["Board", "/atelier"],
      ["Workflows", "/atelier/workflows"],
      ["History", "/atelier/history"]
    ]);
  });

  it("marks the rail item the current page sits under", () => {
    expect(activeWorkshopDestination({ page: "chat" })).toBe("chat");
    expect(activeWorkshopDestination({ page: "studio" })).toBe("board");
    expect(activeWorkshopDestination({ page: "workflows" })).toBe("workflows");
    expect(activeWorkshopDestination({ page: "workflow", name: "Preview door" })).toBe(
      "workflows"
    );
    // Starting a run is a Workflows-owned action (reachable from Board and
    // from a workflow's own detail page), not a History concern.
    expect(activeWorkshopDestination({ page: "new" })).toBe("workflows");
    expect(activeWorkshopDestination({ page: "history" })).toBe("history");
    // A run being watched sits under the room it was opened from (#654): the
    // Board for a Board row, the Workbench for a chat episode -- never under
    // History, which holds only what has finished.
    expect(
      activeWorkshopDestination({ page: "run", publicReference: "run1.cnVu", origin: null })
    ).toBe("board");
    expect(
      activeWorkshopDestination({ page: "run", publicReference: "run1.cnVu", origin: "chat" })
    ).toBe("chat");
    // The project is the context above the four destinations, not a fifth one.
    expect(activeWorkshopDestination({ page: "project" })).toBeNull();
    expect(activeWorkshopDestination({ page: "not-found" })).toBeNull();
  });
});
