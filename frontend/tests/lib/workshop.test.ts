import { describe, expect, it } from "vitest";

import {
  WORKSHOP_DESTINATIONS,
  activeWorkshopDestination,
  destinationIsReachable
} from "../../src/lib/workshop";

describe("the workshop rail names five destinations", () => {
  it("opens today's pages and refuses a click on a destination that has none", () => {
    const labels = WORKSHOP_DESTINATIONS.map((destination) => destination.label);
    expect(labels).toEqual(["Studio", "Projekte", "Runs", "Library", "Settings"]);

    const reachable = WORKSHOP_DESTINATIONS.filter(destinationIsReachable);
    expect(reachable.map((destination) => [destination.label, destination.path])).toEqual([
      ["Studio", "/atelier"],
      ["Projekte", "/atelier/project"]
    ]);

    const deferred = WORKSHOP_DESTINATIONS.filter((destination) => !destinationIsReachable(destination));
    expect(deferred.map((destination) => destination.label)).toEqual(["Runs", "Library", "Settings"]);
    for (const destination of deferred) {
      expect(destination.vision).toContain(destination.visionRef);
      expect("path" in destination).toBe(false);
    }
  });

  it("marks the rail item the current page sits under", () => {
    expect(activeWorkshopDestination({ page: "studio" })).toBe("studio");
    expect(activeWorkshopDestination({ page: "project" })).toBe("projects");
    expect(activeWorkshopDestination({ page: "new" })).toBe("projects");
    expect(activeWorkshopDestination({ page: "run", publicReference: "run1.cnVu" })).toBe(
      "projects"
    );
    expect(activeWorkshopDestination({ page: "not-found" })).toBeNull();
  });
});
