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
 *
 * The catalog detail has its own canonical address. Retired rooms do not keep
 * a second address: a bookmark must either name a current surface or fail
 * honestly.
 */
export const SERVED_PATHS: readonly string[] = servedPaths;

/** Where a run's public reference stands in a served path. */
export const PUBLIC_REFERENCE_PLACEHOLDER = "{public_ref}";

/** Where a workflow's name stands in a served path. */
export const WORKFLOW_NAME_PLACEHOLDER = "{workflow_name:path}";

export type CockpitRoute =
  | { page: "workbench" }
  | { page: "settings" }
  | { page: "catalog" }
  | { page: "workflow"; name: string }
  | { page: "history" }
  | { page: "run"; publicReference: string }
  | { page: "not-found" };

export function cockpitRoute(path: string): CockpitRoute {
  const [pathname = ""] = path.split("?", 2);
  // The workshop opens on the Workbench: what needs you now, what is moving,
  // what was said. `/atelier/chat` is the address that surface grew from.
  if (
    pathname === "/atelier" ||
    pathname === "/atelier/" ||
    pathname === "/atelier/chat" ||
    pathname === "/atelier/chat/"
  ) {
    return { page: "workbench" };
  }
  // /atelier/runs is where this installation's runs lived before the project
  // level existed; it names the same set, so old history entries keep landing
  // on it rather than on a not-found page. #133 owns its fate once a project
  // has a backend identity and "every run" and "this project" can differ.
  if (pathname === "/atelier/settings") {
    return { page: "settings" };
  }
  if (pathname === "/atelier/catalog" || pathname === "/atelier/catalog/") {
    return { page: "catalog" };
  }
  if (pathname === "/atelier/history" || pathname === "/atelier/history/") {
    return { page: "history" };
  }
  const workflowMatch = /^\/atelier\/catalog\/(.+)$/.exec(pathname);
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
      return { page: "run", publicReference };
    }
  }
  return { page: "not-found" };
}

/** The one place the path of a run is built. */
export function runPath(publicReference: string): string {
  return `/atelier/runs/${publicReference}`;
}

/** The one place the path of a workflow's catalog page is built. */
export function workflowPath(name: string): string {
  return `/atelier/catalog/${encodeURIComponent(name)}`;
}
