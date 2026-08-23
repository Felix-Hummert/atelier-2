import { writable, type Writable } from "svelte/store";

import type { CockpitRoute } from "./route";

/**
 * The four destinations the target-UI rail names (mockup v5): Chat, Board,
 * Workflows, History. Each one opens a page this cockpit serves — the rail
 * holds no disabled item, because a rail entry that cannot be clicked is a
 * promise the house does not keep. What a page cannot do yet, that page says
 * in its own words.
 */
export type WorkshopDestination = {
  id: "chat" | "board" | "workflows" | "history";
  label: string;
  path: "/atelier/chat" | "/atelier" | "/atelier/workflows" | "/atelier/history";
};

export const WORKSHOP_DESTINATIONS: readonly WorkshopDestination[] = [
  { id: "chat", label: "Chat", path: "/atelier/chat" },
  { id: "board", label: "Board", path: "/atelier" },
  { id: "workflows", label: "Workflows", path: "/atelier/workflows" },
  { id: "history", label: "History", path: "/atelier/history" }
];

/**
 * Which rail item the current page sits under. A missing page sits under none.
 *
 * `new` marks Workflows (Operator ruling 22.08.): starting a run is a
 * Workflows-owned action reachable from Board and from a workflow's own detail
 * page, not a History concern. `run` marks Board: a run being watched is the
 * Board's own row opened, and the run page's trail leads back there. `project`
 * marks nothing — the project is the context above the four destinations, not
 * a fifth one.
 */
export function activeWorkshopDestination(route: CockpitRoute): WorkshopDestination["id"] | null {
  if (route.page === "chat") {
    return "chat";
  }
  if (route.page === "studio" || route.page === "run") {
    return "board";
  }
  if (route.page === "workflows" || route.page === "workflow" || route.page === "new") {
    return "workflows";
  }
  if (route.page === "history") {
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
