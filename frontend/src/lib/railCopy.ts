import { PRODUCT_NAME } from "./productName";

/**
 * The workshop shell's own copy: the rail's brand wordmark and count suffix,
 * and the quiet provenance footer's two lines (#1100).
 *
 * Each destination's word is the destination's own (`workshop.ts`), so the rail
 * holds no second copy of a room's name. Owned here rather than inline in
 * WorkshopShell so the pseudo-locale check can see that every shell string has
 * one owner instead of a hardcoded copy.
 */
export const railCopy = {
  brand: PRODUCT_NAME,
  needsYouCountSuffix: "needs you",
  newVersionAvailable: "New version available",
  reload: "Reload"
} as const;
