import { get, writable, type Readable } from "svelte/store";

/**
 * The commit and boot instant the running serve reports (#1100).
 *
 * One fact, fed only from `GET /health`: the page's own load answer names
 * `loadedVersion`, and a later answer -- read once after a reconnect
 * (`onConnectionRecovered`) -- is compared against it by `noteObservedVersion`.
 * A mismatch means the serve behind the page changed since it loaded, and the
 * footer says so instead of silently reloading (REQ-UIQ-10): a confirmed page
 * never disappears out from under the operator's hands.
 */
export interface ServeVersion {
  commit: string;
  deployedAt: string;
}

const loadedVersionStore = writable<ServeVersion | null>(null);
const newVersionAvailableStore = writable(false);

export const loadedVersion: Readable<ServeVersion | null> = {
  subscribe: loadedVersionStore.subscribe
};
export const newVersionAvailable: Readable<boolean> = {
  subscribe: newVersionAvailableStore.subscribe
};

/**
 * Names the version the page loaded with.
 *
 * Called once, at mount: it always overwrites, because a fresh mount is by
 * definition a fresh load -- any earlier mismatch no longer describes the
 * page the operator is now looking at.
 */
export function recordLoadedVersion(version: ServeVersion): void {
  loadedVersionStore.set(version);
  newVersionAvailableStore.set(false);
}

/** Compares a freshly observed health answer against the loaded version. */
export function noteObservedVersion(version: ServeVersion): void {
  const loaded = get(loadedVersionStore);
  if (loaded !== null && loaded.commit !== version.commit) {
    newVersionAvailableStore.set(true);
  }
}
