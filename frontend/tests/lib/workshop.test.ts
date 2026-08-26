import { describe, expect, it } from "vitest";

import {
  WORKSHOP_DESTINATION,
  WORKSHOP_ROOMS,
  activeWorkshopDestination
} from "../../src/lib/workshop";

describe("the workshop rail names its destinations", () => {
  it("names three rooms in the order the picture draws them, each with its own glyph and page", () => {
    expect(WORKSHOP_ROOMS.map((room) => [room.label, room.glyph, room.path])).toEqual([
      ["Workbench", "⌂", "/atelier/chat"],
      ["Catalog", "▤", "/atelier/catalog"],
      ["History", "◷", "/atelier/history"]
    ]);
  });

  it("keeps Settings out of the rooms, as the context standing at the rail's foot", () => {
    expect(WORKSHOP_ROOMS.map((room) => room.id)).not.toContain("settings");
    expect([
      WORKSHOP_DESTINATION.settings.label,
      WORKSHOP_DESTINATION.settings.glyph,
      WORKSHOP_DESTINATION.settings.path
    ]).toEqual(["Settings", "⚙", "/atelier/settings"]);
  });

  it("marks the rail entry the current page sits under", () => {
    expect(activeWorkshopDestination({ page: "workbench" })).toBe("workbench");
    // A run that is watched sits under the Workbench, where living work lives.
    expect(activeWorkshopDestination({ page: "run", publicReference: "run1.cnVu" })).toBe(
      "workbench"
    );
    // The catalog is the one room a workflow is found and started from, so its
    // detail sits under it -- never under History.
    expect(activeWorkshopDestination({ page: "catalog" })).toBe("catalog");
    expect(activeWorkshopDestination({ page: "workflow", name: "Preview door" })).toBe("catalog");
    expect(activeWorkshopDestination({ page: "history" })).toBe("history");
    expect(activeWorkshopDestination({ page: "settings" })).toBe("settings");
    expect(activeWorkshopDestination({ page: "not-found" })).toBeNull();
  });
});
