import type { CockpitRoute } from "./route";

/**
 * The four destinations the target-UI rail names (mockup v5): Chat, Board,
 * Workflows, History. A reachable one opens a page this cockpit already
 * serves. A deferred one is named and disabled, with the vision sentence
 * that owns it — never a dead click and never a fake page.
 */
export type ReachableWorkshopDestination = {
  id: "board" | "history";
  label: string;
  path: "/atelier" | "/atelier/project";
};

export type DeferredWorkshopDestination = {
  id: "chat" | "workflows";
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
  {
    id: "workflows",
    label: "Workflows",
    vision: "Workflow catalog, names never hashes — not built yet. REQ-UI-05.",
    visionRef: "REQ-UI-05"
  },
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
  if (route.page === "project" || route.page === "new" || route.page === "run") {
    return "history";
  }
  return null;
}
