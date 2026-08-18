import type { CockpitRoute } from "./route";

/**
 * The five destinations the target-UI rail names. A reachable one opens a page
 * this cockpit already serves. A deferred one is named and disabled, with the
 * vision sentence that owns it — never a dead click and never a fake page.
 */
export type ReachableWorkshopDestination = {
  id: "studio" | "projects";
  label: string;
  path: "/atelier" | "/atelier/project";
};

export type DeferredWorkshopDestination = {
  id: "runs" | "library" | "settings";
  label: string;
  vision: string;
  visionRef: string;
};

export type WorkshopDestination = ReachableWorkshopDestination | DeferredWorkshopDestination;

export const WORKSHOP_DESTINATIONS: readonly WorkshopDestination[] = [
  { id: "studio", label: "Studio", path: "/atelier" },
  { id: "projects", label: "Projekte", path: "/atelier/project" },
  {
    id: "runs",
    label: "Runs",
    vision: "Cross-project run list — not built yet. REQ-UI-13.",
    visionRef: "REQ-UI-13"
  },
  {
    id: "library",
    label: "Library",
    vision: "Names, never hashes — not built yet. REQ-UI-05.",
    visionRef: "REQ-UI-05"
  },
  {
    id: "settings",
    label: "Settings",
    vision: "Professional settings surface — not built yet. REQ-UI-15.",
    visionRef: "REQ-UI-15"
  }
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
    return "studio";
  }
  if (route.page === "project" || route.page === "new" || route.page === "run") {
    return "projects";
  }
  return null;
}
