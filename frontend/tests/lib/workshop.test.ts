import { describe, expect, it } from "vitest";

import {
  WORKSHOP_DESTINATIONS,
  activeWorkshopDestination,
  destinationIsReachable
} from "../../src/lib/workshop";

describe("the workshop rail names four destinations", () => {
  it("opens today's pages and refuses a click on a destination that has none", () => {
    const labels = WORKSHOP_DESTINATIONS.map((destination) => destination.label);
    expect(labels).toEqual(["Chat", "Board", "Workflows", "History"]);

    const reachable = WORKSHOP_DESTINATIONS.filter(destinationIsReachable);
    expect(reachable.map((destination) => [destination.label, destination.path])).toEqual([
      ["Board", "/atelier"],
      ["Workflows", "/atelier/workflows"],
      ["History", "/atelier/history"]
    ]);

    const deferred = WORKSHOP_DESTINATIONS.filter((destination) => !destinationIsReachable(destination));
    expect(deferred.map((destination) => destination.label)).toEqual(["Chat"]);
    for (const destination of deferred) {
      expect(destination.vision).toContain(destination.visionRef);
      expect("path" in destination).toBe(false);
    }
  });

  it("marks the rail item the current page sits under", () => {
    expect(activeWorkshopDestination({ page: "studio" })).toBe("board");
    expect(activeWorkshopDestination({ page: "workflows" })).toBe("workflows");
    expect(activeWorkshopDestination({ page: "workflow", name: "Preview door" })).toBe(
      "workflows"
    );
    // Starting a run is a Workflows-owned action (reachable from Board and
    // from a workflow's own detail page), not a History concern.
    expect(activeWorkshopDestination({ page: "new" })).toBe("workflows");
    expect(activeWorkshopDestination({ page: "history" })).toBe("history");
    expect(activeWorkshopDestination({ page: "run", publicReference: "run1.cnVu" })).toBe(
      "history"
    );
    // The old project level stays reachable at its own URL as a seed for a
    // future project area, but no longer marks the rail's History tab.
    expect(activeWorkshopDestination({ page: "project" })).toBeNull();
    expect(activeWorkshopDestination({ page: "not-found" })).toBeNull();
  });
});
