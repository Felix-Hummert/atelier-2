import { get } from "svelte/store";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  connectionState,
  onConnectionRecovered,
  reportConnectionLost,
  reportConnectionRestored,
  restartNoticeCopy,
  watchConnectionRecovery
} from "../../src/lib/connectionState";

describe("the workshop's one connection store (#700)", () => {
  afterEach(() => {
    reportConnectionRestored();
  });

  it("starts connected and answers every report honestly", () => {
    expect(get(connectionState)).toBe("connected");

    reportConnectionLost();
    expect(get(connectionState)).toBe("reconnecting");

    reportConnectionRestored();
    expect(get(connectionState)).toBe("connected");
  });

  it("names one non-empty line every surface shows while the connection is lost", () => {
    expect(restartNoticeCopy.length).toBeGreaterThan(0);
  });
});

describe("a page's own reads, worth asking again once the connection returns (#700)", () => {
  afterEach(() => {
    reportConnectionRestored();
  });

  it("fires only on the reconnecting-to-connected edge, never on a first healthy mount or a loss", () => {
    const onRecovered = vi.fn();
    const stop = onConnectionRecovered(onRecovered);

    reportConnectionLost();
    expect(onRecovered).not.toHaveBeenCalled();

    reportConnectionRestored();
    expect(onRecovered).toHaveBeenCalledTimes(1);

    // A second, unrelated "already connected" report is not a new edge.
    reportConnectionRestored();
    expect(onRecovered).toHaveBeenCalledTimes(1);

    reportConnectionLost();
    reportConnectionRestored();
    expect(onRecovered).toHaveBeenCalledTimes(2);

    stop();
    reportConnectionLost();
    reportConnectionRestored();
    expect(onRecovered).toHaveBeenCalledTimes(2);
  });
});

describe("the bounded recovery probe for a page with no open stream (#700)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    reportConnectionRestored();
  });

  afterEach(() => {
    reportConnectionRestored();
    vi.useRealTimers();
  });

  it("asks nothing while the connection already answers", async () => {
    const probe = vi.fn(async () => undefined);
    const stop = watchConnectionRecovery(probe);

    await vi.advanceTimersByTimeAsync(30_000);

    expect(probe).not.toHaveBeenCalled();
    stop();
  });

  it("asks again on a fixed interval once lost, and stops the instant a call heals it", async () => {
    const attemptsToHeal = 3;
    let calls = 0;
    const probe = vi.fn(async () => {
      calls += 1;
      if (calls >= attemptsToHeal) {
        reportConnectionRestored();
        return;
      }
      throw new Error("still down");
    });
    const stop = watchConnectionRecovery(probe);

    reportConnectionLost();
    for (let attempt = 0; attempt < attemptsToHeal; attempt += 1) {
      await vi.advanceTimersByTimeAsync(3_000);
    }

    expect(probe).toHaveBeenCalledTimes(attemptsToHeal);
    expect(get(connectionState)).toBe("connected");

    await vi.advanceTimersByTimeAsync(60_000);
    expect(probe).toHaveBeenCalledTimes(attemptsToHeal);
    stop();
  });

  it("gives up after its bounded number of tries rather than polling forever", async () => {
    const probe = vi.fn(async () => {
      throw new Error("still down");
    });
    const stop = watchConnectionRecovery(probe);

    reportConnectionLost();
    await vi.advanceTimersByTimeAsync(3_000 * 150);
    const attemptsMade = probe.mock.calls.length;

    expect(attemptsMade).toBeLessThanOrEqual(100);
    expect(attemptsMade).toBeGreaterThan(50);

    await vi.advanceTimersByTimeAsync(3_000 * 10);
    expect(probe.mock.calls.length).toBe(attemptsMade);
    stop();
  });

  it("asks no more once stopped", async () => {
    const probe = vi.fn(async () => {
      throw new Error("still down");
    });
    const stop = watchConnectionRecovery(probe);

    reportConnectionLost();
    await vi.advanceTimersByTimeAsync(3_000);
    const attemptsMade = probe.mock.calls.length;
    stop();

    await vi.advanceTimersByTimeAsync(30_000);
    expect(probe.mock.calls.length).toBe(attemptsMade);
  });

  it("recovers after the initial budget is exhausted -- the network itself reporting online earns one more try", async () => {
    const attemptsToExhaustBudget = 100;
    let calls = 0;
    const probe = vi.fn(async () => {
      calls += 1;
      if (calls > attemptsToExhaustBudget) {
        reportConnectionRestored();
        return;
      }
      throw new Error("still down");
    });
    const stop = watchConnectionRecovery(probe);

    reportConnectionLost();
    for (let attempt = 0; attempt < attemptsToExhaustBudget; attempt += 1) {
      await vi.advanceTimersByTimeAsync(3_000);
    }
    expect(probe).toHaveBeenCalledTimes(attemptsToExhaustBudget);
    expect(get(connectionState)).toBe("reconnecting");

    // Time alone earns nothing more once the interval budget is spent.
    await vi.advanceTimersByTimeAsync(3_000 * 5);
    expect(probe).toHaveBeenCalledTimes(attemptsToExhaustBudget);

    window.dispatchEvent(new Event("online"));
    await vi.advanceTimersByTimeAsync(0);
    expect(probe).toHaveBeenCalledTimes(attemptsToExhaustBudget + 1);
    expect(get(connectionState)).toBe("connected");

    // A second signal right after the first earns nothing more -- the sparse
    // fallback is rate-limited, not a second unbounded loop.
    window.dispatchEvent(new Event("online"));
    await vi.advanceTimersByTimeAsync(0);
    expect(probe).toHaveBeenCalledTimes(attemptsToExhaustBudget + 1);
    stop();
  });

  it("aborts its still-pending probe's signal the instant it tears down", async () => {
    const captured: { signal: AbortSignal | null } = { signal: null };
    const probe = vi.fn(
      (signal: AbortSignal) =>
        new Promise<void>((_resolve, reject) => {
          captured.signal = signal;
          signal.addEventListener("abort", () => reject(new Error("aborted")));
        })
    );
    const stop = watchConnectionRecovery(probe);

    reportConnectionLost();
    await vi.advanceTimersByTimeAsync(3_000);
    expect(probe).toHaveBeenCalledTimes(1);
    expect(captured.signal?.aborted).toBe(false);

    stop();

    expect(captured.signal?.aborted).toBe(true);
  });
});
