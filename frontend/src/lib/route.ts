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

export type CockpitRoute =
  | { page: "studio" }
  | { page: "project" }
  | { page: "new" }
  | { page: "workflows" }
  | { page: "workflow"; name: string }
  | { page: "run"; publicReference: string }
  | { page: "not-found" };

export function cockpitRoute(pathname: string): CockpitRoute {
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
  if (pathname === "/atelier/new") {
    return { page: "new" };
  }
  if (pathname === "/atelier/workflows" || pathname === "/atelier/workflows/") {
    return { page: "workflows" };
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
  return `/atelier/workflows/${encodeURIComponent(name)}`;
}
