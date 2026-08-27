/**
 * Copy the named proof affordance speaks: copy, copied, and the sentence
 * that names what a value seals. Shared by every room that hosts
 * `ProofAnchor`.
 */
export const proofAnchorCopy = {
  copy: "Copy",
  copied: "Copied"
} as const;

export function copyLabel(label: string): string {
  return `${proofAnchorCopy.copy} ${label}`;
}

export function sealsSentence(seals: string, prefix: boolean): string {
  return prefix
    ? `Seals ${seals}.`
    : `${seals.charAt(0).toUpperCase()}${seals.slice(1)}.`;
}
