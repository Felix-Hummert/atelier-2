import type { RunListRow, RunV3 } from "../../src/api/client";
import { splitRunListRows } from "../../src/lib/runList";

/**
 * The healthy runs on one read of the run-list wire, its defective rows set
 * aside (#1042). `GET /runs` answers with the row union, not a plain
 * `RunV3[]`, wherever the durable list can serve a page at all; the e2e
 * specs only ever assert on the healthy runs of that page, so this is the
 * one place that unwraps the union instead of every call site re-deciding it.
 */
export function healthyRunListItems(items: readonly RunListRow[]): RunV3[] {
  return splitRunListRows(items).runs;
}
