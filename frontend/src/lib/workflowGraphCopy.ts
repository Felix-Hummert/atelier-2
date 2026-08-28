/**
 * Copy the still workflow drawing speaks. Shared by the catalog detail and
 * the run view, so those rooms do not each own the graph's name.
 */
export const workflowGraphCopy = {
  label: "Workflow",
  /** A rail node reused from an origin fork — the picture's dashed double ring. */
  carriedOver: "carried over"
} as const;

export function loopRoundBound(maximumRounds: number): string {
  return `max ${maximumRounds}`;
}

export function loopBoundLabel(bound: string): string {
  return `↻ ${bound}`;
}

export function loopUntilLabel(verdict: string, bound: string): string {
  return `↻ until ${verdict} · ${bound}`;
}

export function loopLabel(loop: {
  readonly maximum_rounds: number;
  readonly repeat_while: { readonly verdict: string } | null;
}): string {
  const bound = loopRoundBound(loop.maximum_rounds);
  return loop.repeat_while === null
    ? loopBoundLabel(bound)
    : loopUntilLabel(loop.repeat_while.verdict, bound);
}
