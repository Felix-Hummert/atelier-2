import { writable, type Writable } from "svelte/store";

import type { CockpitRoute } from "./route";

/**
 * The four destinations the target-UI rail names (mockup v5): Chat, Board,
 * Workflows, History. A reachable one opens a page this cockpit already
 * serves. A deferred one is named and disabled, with the vision sentence
 * that owns it — never a dead click and never a fake page.
 */
export type ReachableWorkshopDestination = {
  id: "board" | "workflows" | "history";
  label: string;
  path: "/atelier" | "/atelier/workflows" | "/atelier/project";
};

export type DeferredWorkshopDestination = {
  id: "chat";
  label: string;
  vision: string;
  visionRef: string;
};

export type WorkshopDestination = ReachableWorkshopDestination | DeferredWorkshopDestination;

export const WORKSHOP_DESTINATIONS: readonly WorkshopDestination[] = [
  {
    id: "chat",
    label: "Chat",
    vision: "The conductor door — not built yet. Vision #7.",
    visionRef: "#7"
  },
  { id: "board", label: "Board", path: "/atelier" },
  { id: "workflows", label: "Workflows", path: "/atelier/workflows" },
  { id: "history", label: "History", path: "/atelier/project" }
];

export function destinationIsReachable(
  destination: WorkshopDestination
): destination is ReachableWorkshopDestination {
  return "path" in destination;
}

/** Which rail item the current page sits under. A missing page sits under none. */
export function activeWorkshopDestination(
  route: CockpitRoute
): ReachableWorkshopDestination["id"] | null {
  if (route.page === "studio") {
    return "board";
  }
  if (route.page === "workflows" || route.page === "workflow") {
    return "workflows";
  }
  if (route.page === "project" || route.page === "new" || route.page === "run") {
    return "history";
  }
  return null;
}

export type BoardBadgeCounts = { needsYou: number; running: number };

/**
 * The rail's Board badges, as the Board page's own reads last confirmed them.
 *
 * The rail is mounted for every page, but only the Board page reads runs. A
 * page that is not Board still shows the badges from the Board's last
 * confirmed read rather than nothing -- but never a number this shell cannot
 * trace to that read. Before the Board has read once, this is `null` and the
 * rail shows no badge, honestly, instead of a fabricated zero.
 */
export const boardBadgeCounts: Writable<BoardBadgeCounts | null> = writable(null);
