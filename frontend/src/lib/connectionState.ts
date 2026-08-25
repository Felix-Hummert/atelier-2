import { writable, type Readable } from "svelte/store";

/**
 * Whether the workshop can currently reach its own server (#700).
 *
 * One store, fed from the two places that ever touch the wire --
 * `requestJsonResult` and `subscribeEventSource` in `api/client.ts` -- so
 * every surface reads the same fact instead of guessing from its own last
 * request. A redeploy's ~30s restart is the one case this exists for: no
 * page-local error banner, one calm line the whole workshop shows and clears
 * together.
 */
export type ConnectionStatus = "connected" | "reconnecting";

const status = writable<ConnectionStatus>("connected");

export const connectionState: Readable<ConnectionStatus> = { subscribe: status.subscribe };

/** The one honest line every surface shows while the connection is lost. */
export const restartNoticeCopy = "The atelier is restarting — back in a moment";

/** A round trip reached the server, whatever it answered. */
export function reportConnectionRestored(): void {
  status.set("connected");
}

/** The wire itself did not carry a round trip -- not a 4xx/5xx, an outage. */
export function reportConnectionLost(): void {
  status.set("reconnecting");
}

const PROBE_INTERVAL_MS = 3_000;
/** ~5 minutes at the interval above -- well past a host redeploy's ~30s gap,
 * bounded so a tab left open through a real, longer outage does not poll
 * forever. */
const MAXIMUM_PROBE_ATTEMPTS = 100;

/**
 * The bounded recovery loop for a surface that holds no open stream of its
 * own (#700 stage 1).
 *
 * A run cockpit or the board heals the moment its native `EventSource`
 * reopens and reports here on its own `open` event. The Workbench, Catalog,
 * Workflows and History pages never hold one, so nothing would ever ask
 * again once a request failed -- this is the one loop that asks on their
 * behalf, a fixed number of tries against an existing cheap read, starting
 * only while the store is unhealthy and stopping the instant it is not.
 */
export function watchConnectionRecovery(probe: () => Promise<unknown>): () => void {
  let attempts = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let current: ConnectionStatus = "connected";
  let disposed = false;

  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function scheduleProbe(): void {
    clearTimer();
    timer = setTimeout(() => {
      void runProbe();
    }, PROBE_INTERVAL_MS);
  }

  async function runProbe(): Promise<void> {
    timer = null;
    attempts += 1;
    try {
      await probe();
    } catch {
      // The caller's own request path already reported the failure through
      // reportConnectionLost; this loop's only job is asking again.
    }
    if (!disposed && current === "reconnecting" && attempts < MAXIMUM_PROBE_ATTEMPTS) {
      scheduleProbe();
    }
  }

  const unsubscribe = status.subscribe((value) => {
    current = value;
    if (value === "connected") {
      attempts = 0;
      clearTimer();
      return;
    }
    if (timer === null) scheduleProbe();
  });

  return () => {
    disposed = true;
    unsubscribe();
    clearTimer();
  };
}
