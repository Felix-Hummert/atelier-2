import { get } from "svelte/store";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  connectionState,
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
});
