import { writable, type Writable } from "svelte/store";

import type { CockpitRoute } from "./route";

/**
 * The destinations the target-UI rail names: Workbench, Board, Workflows,
 * Catalog, History. Each one opens a page this cockpit serves — the rail holds
 * no disabled item, because a rail entry that cannot be clicked is a promise
 * the house does not keep. What a page cannot do yet, that page says in its own
 * words.
 *
 * The Workbench keeps the `/atelier/chat` address it grew from (issue #580):
 * the surface became the composer plus its pinned decisions, but its served
 * path is a durable bookmark this rename leaves untouched.
 *
 * Catalog is the newest room (#659): it is where a piece enters the atelier and
 * where everything published — workflows, agents, skills — is seen with its
 * provenance. Workflows still owns browsing a named workflow's graph and
 * starting it, so the two rooms overlap in their workflow list today; #660
 * reshapes the Catalog into the Git window and owns collapsing that overlap.
 */
export type WorkshopDestination = {
  id: "chat" | "board" | "workflows" | "catalog" | "history";
  label: string;
  path:
    | "/atelier/chat"
    | "/atelier"
    | "/atelier/workflows"
    | "/atelier/catalog"
    | "/atelier/history";
};

/** Each destination by name, for the trail that leads back to one of them. */
export const WORKSHOP_DESTINATION: Record<WorkshopDestination["id"], WorkshopDestination> = {
  chat: { id: "chat", label: "Workbench", path: "/atelier/chat" },
  board: { id: "board", label: "Board", path: "/atelier" },
  workflows: { id: "workflows", label: "Workflows", path: "/atelier/workflows" },
  catalog: { id: "catalog", label: "Catalog", path: "/atelier/catalog" },
  history: { id: "history", label: "History", path: "/atelier/history" }
};

export const WORKSHOP_DESTINATIONS: readonly WorkshopDestination[] = [
  WORKSHOP_DESTINATION.chat,
  WORKSHOP_DESTINATION.board,
  WORKSHOP_DESTINATION.workflows,
  WORKSHOP_DESTINATION.catalog,
  WORKSHOP_DESTINATION.history
];

/**
 * Which rail item the current page sits under. A missing page sits under none.
 *
 * `new` marks Workflows (Operator ruling 22.08.): starting a run is a
 * Workflows-owned action reachable from Board and from a workflow's own detail
 * page, not a History concern. `run` marks the room it was opened from — the
 * Workbench for a chat episode, otherwise the Board, whose row a watched run
 * is — and the run page's trail leads back to the same room. `project` marks
 * nothing — the project is the context above the four destinations, not a
 * fifth one.
 */
export function activeWorkshopDestination(route: CockpitRoute): WorkshopDestination["id"] | null {
  if (route.page === "chat") {
    return "chat";
  }
  if (route.page === "run") {
    return route.origin ?? "board";
  }
  if (route.page === "studio") {
    return "board";
  }
  if (route.page === "workflows" || route.page === "workflow" || route.page === "new") {
    return "workflows";
  }
  if (route.page === "catalog") {
    return "catalog";
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
