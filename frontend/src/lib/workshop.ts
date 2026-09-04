import { writable, type Writable } from "svelte/store";

import type { CockpitRoute } from "./route";

/**
 * The rail of the target picture (ADR 0019 §1): three rooms — Workbench,
 * Catalog, History — and at its foot, set apart by a line, Settings, the
 * context above the three, with the project name small beneath it.
 *
 * Each room answers one question. The Workbench holds what wants you now and
 * what is moving; the Catalog holds what the house can do, where it came from
 * and the door to start one by hand; History holds what is over. There is no
 * Board and no Workflows room: a Board would repeat the workbench and the
 * history, and starting lives in the catalog.
 *
 * The Workbench keeps the `/atelier/chat` address it grew from (issue #580).
 * Settings and catalog detail use their canonical paths, declared alongside
 * the server's cold-load paths in `servedPaths.json`.
 */
type WorkshopRoomId = "workbench" | "catalog" | "history";

export type WorkshopDestination = {
  id: WorkshopRoomId | "settings";
  label: string;
  /** The one glyph this destination wears; §01 of the picture explains the vocabulary. */
  glyph: string;
  path: "/atelier/chat" | "/atelier/catalog" | "/atelier/history" | "/atelier/settings";
};

/** Each destination by name, for the trail that leads back to one of them. */
export const WORKSHOP_DESTINATION: Record<WorkshopDestination["id"], WorkshopDestination> = {
  workbench: { id: "workbench", label: "Workbench", glyph: "⌂", path: "/atelier/chat" },
  catalog: { id: "catalog", label: "Catalog", glyph: "▤", path: "/atelier/catalog" },
  history: { id: "history", label: "History", glyph: "◷", path: "/atelier/history" },
  settings: { id: "settings", label: "Settings", glyph: "⚙", path: "/atelier/settings" }
};

/** The three rooms, in the order the rail shows them; Settings stands apart at its foot. */
export const WORKSHOP_ROOMS: readonly WorkshopDestination[] = [
  WORKSHOP_DESTINATION.workbench,
  WORKSHOP_DESTINATION.catalog,
  WORKSHOP_DESTINATION.history
];

/**
 * Which rail entry the current page sits under. A missing page sits under none.
 *
 * The workflow detail sits under the Catalog: it is the one
 * room a workflow is found and started from. A run sits under the Workbench,
 * where living work lives now that the Board is gone. The run page's trail
 * is derived from the run — alive to the Workbench, ended to History
 * (ADR 0019 §1), with an in-app room origin overriding — so the remaining
 * gap is this rail mark, not the trail.
 */
export function activeWorkshopDestination(route: CockpitRoute): WorkshopDestination["id"] | null {
  if (route.page === "workbench" || route.page === "run") {
    return "workbench";
  }
  if (route.page === "catalog" || route.page === "workflow") {
    return "catalog";
  }
  if (route.page === "history") {
    return "history";
  }
  if (route.page === "settings") {
    return "settings";
  }
  return null;
}

/**
 * How many runs wait for a person, as the Workbench's own read last confirmed
 * them — the ochre count in the rail, and the only number the rail carries
 * (ADR 0019 §1: the count in the rail is the notification).
 *
 * The rail is mounted for every page, but only the Workbench reads runs. A
 * page that is not the Workbench still shows the count from its last confirmed
 * read rather than nothing — but never a number this shell cannot trace to
 * that read. Before the Workbench has read once this is `null` and the rail
 * shows no count, honestly, instead of a fabricated zero.
 */
export const runsWaitingForYou: Writable<number | null> = writable(null);
