import type { CockpitRoute } from "./route";
import { WORKSHOP_DESTINATION, type WorkshopDestination } from "./workshop";

/**
 * Copy the one-way-back trail speaks. Shared by every room that hosts
 * `BackLink`.
 *
 * Room names alias `WORKSHOP_DESTINATION` so the trail cannot drift from
 * the rail.
 */
export const backLinkCopy = {
  whereYouAre: "Where you are",
  workbench: WORKSHOP_DESTINATION.workbench.label,
  catalog: WORKSHOP_DESTINATION.catalog.label,
  history: WORKSHOP_DESTINATION.history.label,
  settings: WORKSHOP_DESTINATION.settings.label
} as const;

/** Where the run trail leads: an in-app room origin, else History if ended and Workbench if live. */
export function runBackLink(
  ended: boolean,
  cameFrom: WorkshopDestination | null
): { label: string; path: WorkshopDestination["path"] } {
  const destination = cameFrom ?? (ended ? WORKSHOP_DESTINATION.history : WORKSHOP_DESTINATION.workbench);
  return { label: backLinkCopy[destination.id], path: destination.path };
}

/**
 * A previous path is an origin only when `cockpitRoute` names a real workshop
 * room. Another run, a missing page, or a leftover query is not — those must
 * not survive a paste or a reload as if the operator had walked there.
 */
export function inAppRoomOrigin(route: CockpitRoute): WorkshopDestination | null {
  switch (route.page) {
    case "workbench":
      return WORKSHOP_DESTINATION.workbench;
    case "catalog":
    case "workflow":
      return WORKSHOP_DESTINATION.catalog;
    case "history":
      return WORKSHOP_DESTINATION.history;
    case "settings":
      return WORKSHOP_DESTINATION.settings;
    case "run":
    case "not-found":
      return null;
  }
}
