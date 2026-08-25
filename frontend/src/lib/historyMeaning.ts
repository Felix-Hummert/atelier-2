import { isRunV3, type CockpitApi, type NodeDetail } from "../api/client";
import { decodeUtf8Base64 } from "./exactBytes";
import type { HistoryRow } from "./historyRows";
import { historyPageCopy } from "./historyPageCopy";
import type { WorkflowGraphV3 } from "./runList";

/**
 * A row's two Klartext facts (REQ-UI-13): why it ran, and what came of it.
 *
 * Both are read straight from the run's own node detail -- the same durable
 * material `NodeDetailPanel.svelte` already opens on a click -- never a second
 * summary the server did not attest. `purpose` is null only where the run's
 * workflow declares no order at all (an honest absence, shown as a dash);
 * `result` always carries a sentence, because "Done" alone is exactly what
 * REQ-UI-13 refuses.
 */
export type RowMeaning = {
  purpose: string | null;
  result: string;
};

/**
 * The heading `atelier2.application.compose_node_job.node_job` writes before
 * an order's value, mirrored here because the wire answers job text, not a
 * structured order list -- the same "read the job the composition already
 * wrote" move `bind_node_execution.py` documents on the server side.
 */
const ORDER_SECTION = /^--- order: (.+) ---$/;
const RESULT_SECTION = /^--- result of .+: .+ ---$/;

/**
 * The orders a node's job text carries, keyed by the order's own declared
 * name. A job with no order section (a node that reads none) answers empty,
 * never a guess.
 */
export function ordersInJob(jobText: string): ReadonlyMap<string, string> {
  const orders = new Map<string, string>();
  let name: string | null = null;
  let lines: string[] = [];
  const flush = (): void => {
    if (name !== null) orders.set(name, lines.join("\n").trim());
  };
  for (const line of jobText.split("\n")) {
    const orderMatch = ORDER_SECTION.exec(line);
    if (orderMatch?.[1] !== undefined) {
      flush();
      name = orderMatch[1];
      lines = [];
      continue;
    }
    if (RESULT_SECTION.test(line)) {
      flush();
      name = null;
      continue;
    }
    if (name !== null) lines.push(line);
  }
  flush();
  return orders;
}

/** The first line of a multi-line value, trimmed -- a heading never keeps the rest. */
function firstLine(text: string): string {
  return (text.split("\n")[0] ?? "").trim();
}

/**
 * The one text a JSON value reads as in plain words, or null where more than
 * one candidate exists and picking one would be a guess.
 *
 * A bare JSON string is its own text. An object is read the same way this
 * codebase already reads a single-field *order* (`orderSchema.ts`'s
 * `singleRequiredStringField`), turned around for an *output*: the sink's
 * declared report shape is never named here (the conductor's is `answer`,
 * `atelier2.host.conductor_workflow`'s own `_REPORT_ANSWER_FIELD`; another
 * workflow's may be `greeting`, or anything else its author chose), so this
 * reads structurally instead -- the one string-valued property among however
 * many the object declares, exactly where that is unambiguous.
 */
function soleTextIn(parsed: unknown): string | null {
  if (typeof parsed === "string") return parsed;
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
  const strings = Object.values(parsed as Record<string, unknown>).filter(
    (value): value is string => typeof value === "string"
  );
  return strings.length === 1 ? (strings[0] ?? null) : null;
}

/**
 * A value read down to one honest line: its sole text where the JSON shape
 * names one unambiguously, its own first line of compact JSON otherwise --
 * still the real value, never a placeholder.
 */
function plainTextOf(decoded: string): string {
  try {
    const parsed: unknown = JSON.parse(decoded);
    const sole = soleTextIn(parsed);
    if (sole !== null) return firstLine(sole);
  } catch {
    // Not JSON at all -- read the raw bytes as the plain text they are.
  }
  return firstLine(decoded);
}

/**
 * The node ids a V3 graph starts from: every node nothing else in the graph
 * depends on. A node names an order through a `graph_input` source
 * (`bind_node_execution.py`), and that can be any root, not only a single
 * "first" one, so every root is asked.
 */
function rootNodeIds(graph: WorkflowGraphV3): readonly string[] {
  return graph.node_previews
    .filter((node) => node.depends_on.length === 0)
    .map((node) => node.id);
}

/**
 * The one node nothing else in the graph depends on. `MultipleSinkCompletionUnsupported`
 * (server-side) makes this exist for exactly one node on any graph that can
 * actually complete a run -- a graph this reads before that guarantee holds
 * (a run still short of its sink) answers null rather than guessing one.
 */
function soleSinkNodeId(graph: WorkflowGraphV3): string | null {
  const dependedOn = new Set(graph.node_previews.flatMap((node) => node.depends_on));
  const sinks = graph.node_previews.filter((node) => !dependedOn.has(node.id));
  return sinks.length === 1 ? (sinks[0]?.id ?? null) : null;
}

/**
 * Every node this row needs read, fetched once each even where a root and the
 * sink happen to be the same node (the common single-agent shape). A node
 * this row could not read (network failure, or a node the run never actually
 * reached) is simply absent from the map -- every caller below already treats
 * absence as its own honest fallback.
 */
async function readNodeDetails(
  publicRunReference: string,
  nodeIds: ReadonlySet<string>,
  cockpitApi: CockpitApi
): Promise<ReadonlyMap<string, NodeDetail>> {
  const entries = await Promise.all(
    [...nodeIds].map(async (nodeId) => {
      try {
        return [nodeId, await cockpitApi.getNodeDetail(publicRunReference, nodeId)] as const;
      } catch {
        return null;
      }
    })
  );
  return new Map(entries.filter((entry): entry is readonly [string, NodeDetail] => entry !== null));
}

function purposeFrom(
  roots: readonly string[],
  details: ReadonlyMap<string, NodeDetail>
): string | null {
  const orders = new Map<string, string>();
  for (const nodeId of roots) {
    const detail = details.get(nodeId);
    if (detail === undefined || detail.job_base64 === null) continue;
    const jobText = decodeUtf8Base64(detail.job_base64);
    if (jobText === null) continue;
    for (const [name, value] of ordersInJob(jobText)) orders.set(name, value);
  }
  if (orders.size === 0) return null;
  return [...orders.keys()]
    .sort()
    .map((name) => plainTextOf(orders.get(name) ?? ""))
    .join(" · ");
}

function completedResultFrom(
  sinkId: string | null,
  details: ReadonlyMap<string, NodeDetail>
): string | null {
  if (sinkId === null) return null;
  const detail = details.get(sinkId);
  if (detail === undefined || detail.answer === null) return null;
  const decoded = decodeUtf8Base64(detail.answer.value_base64);
  return decoded === null ? null : plainTextOf(decoded);
}

/**
 * One row's purpose and result, read live from its own node material.
 *
 * `graph` is null for a V1/V2 run (no node detail door this reads carries a
 * job/answer split there): purpose stays unset, and a completed row falls
 * back to the same honest "unavailable" text a genuine read failure would
 * also produce -- both mean the same thing to the person reading the row.
 */
export async function meaningOf(
  row: HistoryRow,
  graph: WorkflowGraphV3 | null,
  cockpitApi: CockpitApi
): Promise<RowMeaning> {
  const reference = row.run.public_run_reference;
  const roots = graph !== null && isRunV3(row.run) ? rootNodeIds(graph) : [];
  const outcome = row.result;
  const sinkId = outcome.kind === "completed" && graph !== null ? soleSinkNodeId(graph) : null;

  const neededNodeIds = new Set(roots);
  if (sinkId !== null) neededNodeIds.add(sinkId);
  if (outcome.kind === "failed") neededNodeIds.add(outcome.nodeId);

  const details = await readNodeDetails(reference, neededNodeIds, cockpitApi);
  const purpose = purposeFrom(roots, details);

  if (outcome.kind === "completed") {
    return { purpose, result: completedResultFrom(sinkId, details) ?? historyPageCopy.resultUnavailable };
  }
  // The node's own refusal is unreadable (or the store genuinely recorded
  // none) -- name the node it stopped at, the same fact the row named before
  // this read existed, rather than repeating "failed" a second way.
  return { purpose, result: details.get(outcome.nodeId)?.refusal ?? outcome.nodeId };
}
