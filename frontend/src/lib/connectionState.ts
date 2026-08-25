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

/**
 * Runs `onRecovered` once, every time the store crosses from "reconnecting"
 * back to "connected" -- the moment a page's own reads, which failed while
 * the workshop was unreachable, are worth asking again on their own, with no
 * reload (#700). A read that failed for a real, unrelated reason is not this
 * function's question: it only ever fires on the edge back to healthy.
 */
export function onConnectionRecovered(onRecovered: () => void): () => void {
  let previous: ConnectionStatus = "connected";
  return connectionState.subscribe((value) => {
    if (previous === "reconnecting" && value === "connected") onRecovered();
    previous = value;
  });
}

const PROBE_INTERVAL_MS = 3_000;
/** ~5 minutes at the interval above -- well past a host redeploy's ~30s gap,
 * bounded so a tab left open through a real, longer outage does not poll
 * forever. */
const MAXIMUM_PROBE_ATTEMPTS = 100;
/** Once the bounded budget above is spent, a returning tab or a network that
 * just reported `online` is worth one more try each -- but never more than
 * one per this gap, so a burst of tab switches or online/offline flapping
 * cannot turn the sparse fallback into the same hammering the budget exists
 * to prevent. */
const SPARSE_RETRY_MINIMUM_GAP_MS = 15_000;

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
 *
 * The interval above gives up once its budget is spent, but a tab is not:
 * the operator returning to it (`visibilitychange`) or the OS reporting the
 * network itself back (`online`) each earn one more try, sparsely, so a
 * longer real outage still heals without a reload once the tab is looked at
 * or the network returns. Each attempt carries the caller's `AbortSignal` so
 * a still-pending probe is cancelled the instant this loop tears down rather
 * than resolving into a component that is already gone.
 */
export function watchConnectionRecovery(
  probe: (signal: AbortSignal) => Promise<unknown>
): () => void {
  let attempts = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let current: ConnectionStatus = "connected";
  let disposed = false;
  let controller: AbortController | null = null;
  let lastAttemptAt = 0;

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
    lastAttemptAt = Date.now();
    const attemptController = new AbortController();
    controller = attemptController;
    try {
      await probe(attemptController.signal);
    } catch {
      // The caller's own request path already reported the failure through
      // reportConnectionLost; this loop's only job is asking again.
    } finally {
      if (controller === attemptController) controller = null;
    }
    if (!disposed && current === "reconnecting" && attempts < MAXIMUM_PROBE_ATTEMPTS) {
      scheduleProbe();
    }
  }

  function trySparseRetry(): void {
    if (
      disposed ||
      current !== "reconnecting" ||
      attempts < MAXIMUM_PROBE_ATTEMPTS ||
      Date.now() - lastAttemptAt < SPARSE_RETRY_MINIMUM_GAP_MS
    ) {
      return;
    }
    void runProbe();
  }

  function onVisibilityChange(): void {
    if (document.visibilityState === "visible") trySparseRetry();
  }

  const unsubscribe = status.subscribe((value) => {
    current = value;
    if (value === "connected") {
      attempts = 0;
      clearTimer();
      return;
    }
    if (timer === null && attempts < MAXIMUM_PROBE_ATTEMPTS) scheduleProbe();
  });

  document.addEventListener("visibilitychange", onVisibilityChange);
  window.addEventListener("online", trySparseRetry);

  return () => {
    disposed = true;
    unsubscribe();
    clearTimer();
    controller?.abort();
    document.removeEventListener("visibilitychange", onVisibilityChange);
    window.removeEventListener("online", trySparseRetry);
  };
}
