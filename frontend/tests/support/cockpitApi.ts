import { vi } from "vitest";

import type { CockpitApi, RunEventHandlers } from "../../src/api/client";

/**
 * One owner for the CockpitApi test double: when the port grows a method, the
 * suite learns about it here instead of in every app test.
 *
 * Only the two list reads carry a default — the neutral "nothing there" page.
 * Everything a test depends on must be handed in, so no test passes because a
 * shared default happened to answer for it.
 */
export function cockpitApiStub(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return {
    listRuns: vi.fn(async () => ({ items: [], next_after: null })),
    listWorkflowRevisions: vi.fn(async () => ({ items: [], next_after_revision_hash: null })),
    publish: vi.fn(),
    publishAuthProfile: vi.fn(),
    publishAgentConfiguration: vi.fn(),
    start: vi.fn(),
    answer: vi.fn(),
    reconcile: vi.fn(),
    getRun: vi.fn(),
    getWorkflowRevision: vi.fn(),
    openRunEvents: vi.fn(() => ({ close: vi.fn() })),
    ...overrides
  };
}

/** A run event transport the test drives by hand, holding the handlers it was opened with. */
export class FakeRunEventFeed {
  handlers: RunEventHandlers | null = null;
  close = vi.fn();
  open: CockpitApi["openRunEvents"] = vi.fn((_publicReference: string, handlers: RunEventHandlers) => {
    this.handlers = handlers;
    return { close: this.close };
  });
}
