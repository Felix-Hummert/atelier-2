/**
 * Copy the recoverable-read control speaks: looking, refreshing, and the
 * retry that repeats a failed read. Shared by every room that hosts
 * `ReadState`, so those words have one owner rather than one per page.
 */
export const readStateCopy = {
  looking: "Looking…",
  refreshing: "Refreshing…",
  retry: "Retry"
} as const;

export function retryLabel(label: string): string {
  return `${readStateCopy.retry} ${label}`;
}
