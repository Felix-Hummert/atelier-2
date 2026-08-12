import { decodePublicRunReference } from "../api/client";

export type CockpitRoute =
  | { page: "runs" }
  | { page: "new" }
  | { page: "run"; publicReference: string }
  | { page: "not-found" };

export function cockpitRoute(pathname: string): CockpitRoute {
  if (pathname === "/atelier" || pathname === "/atelier/" || pathname === "/atelier/runs") {
    return { page: "runs" };
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
