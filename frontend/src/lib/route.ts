import { decodePublicRunReference } from "../api/client";

export type CockpitRoute =
  | { page: "studio" }
  | { page: "project" }
  | { page: "new" }
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
