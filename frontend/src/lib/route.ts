import { decodePublicRunReference } from "../api/client";
import servedPaths from "./servedPaths.json";

/**
 * Every path a browser may be given cold — reloaded, pasted, bookmarked.
 *
 * This router decides what is a page, so it declares them; the server reads the
 * same file and fails its host test when it does not serve exactly these. That
 * crossing exists because `/atelier/project` shipped reachable only by click:
 * the server's list was a copy, and a copy can only carry the paths someone
 * already thought of.
 */
export const SERVED_PATHS: readonly string[] = servedPaths;

/** Where a run's public reference stands in a served path. */
export const PUBLIC_REFERENCE_PLACEHOLDER = "{public_ref}";

/** Where a workflow's name stands in a served path. */
export const WORKFLOW_NAME_PLACEHOLDER = "{workflow_name}";

/**
 * Where the operator opened a run from, when it was not the Board.
 *
 * The run page's trail leads back to the room the operator actually left
 * (#654: coming from the chat, "← Board" was a lie). The origin travels as a
 * query parameter so it survives a reload, and a link without one keeps the
 * Board as the trail's home.
 */
export type RunOrigin = "chat";

const RUN_ORIGIN_PARAMETER = "from";

export type CockpitRoute =
  | { page: "chat" }
  | { page: "studio" }
  | { page: "project" }
  | { page: "new" }
  | { page: "workflows" }
  | { page: "workflow"; name: string }
  | { page: "history" }
  | { page: "run"; publicReference: string; origin: RunOrigin | null }
  | { page: "not-found" };

export function cockpitRoute(path: string): CockpitRoute {
  const [pathname = "", query = ""] = path.split("?", 2);
  if (pathname === "/atelier" || pathname === "/atelier/") {
    return { page: "studio" };
  }
  // /atelier/runs is where this installation's runs lived before the project
  // level existed; it names the same set, so old history entries keep landing
  // on it rather than on a not-found page. #133 owns its fate once a project
  // has a backend identity and "every run" and "this project" can differ.
  if (pathname === "/atelier/project" || pathname === "/atelier/runs") {
    return { page: "project" };
  }
  if (pathname === "/atelier/chat" || pathname === "/atelier/chat/") {
    return { page: "chat" };
  }
  if (pathname === "/atelier/new") {
    return { page: "new" };
  }
  if (pathname === "/atelier/workflows" || pathname === "/atelier/workflows/") {
    return { page: "workflows" };
  }
  if (pathname === "/atelier/history" || pathname === "/atelier/history/") {
    return { page: "history" };
  }
  const workflowMatch = /^\/atelier\/workflows\/([^/]+)$/.exec(pathname);
  if (workflowMatch?.[1] !== undefined) {
    try {
      return { page: "workflow", name: decodeURIComponent(workflowMatch[1]) };
    } catch {
      return { page: "not-found" };
    }
  }
  const match = /^\/atelier\/runs\/([^/]+)$/.exec(pathname);
  if (match?.[1] !== undefined) {
    let publicReference: string;
    try {
      publicReference = decodeURIComponent(match[1]);
    } catch {
      return { page: "not-found" };
    }
    if (decodePublicRunReference(publicReference) !== null) {
      return { page: "run", publicReference, origin: runOrigin(query) };
    }
  }
  return { page: "not-found" };
}

function runOrigin(query: string): RunOrigin | null {
  const named = new URLSearchParams(query).get(RUN_ORIGIN_PARAMETER);
  return named === "chat" ? named : null;
}

/** The one place the path of a run is built. */
export function runPath(publicReference: string, origin?: RunOrigin): string {
  const path = `/atelier/runs/${publicReference}`;
  return origin === undefined ? path : `${path}?${RUN_ORIGIN_PARAMETER}=${origin}`;
}

/** The one place the path of a workflow's catalog page is built. */
export function workflowPath(name: string): string {
  return `/atelier/workflows/${encodeURIComponent(name)}`;
}
