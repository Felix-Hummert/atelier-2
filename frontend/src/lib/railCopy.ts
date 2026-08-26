import { PRODUCT_NAME } from "./productName";

/**
 * The rail's own copy: the brand wordmark, and what the ochre count says to a
 * reader who cannot see it.
 *
 * Each destination's word is the destination's own (`workshop.ts`), so the rail
 * holds no second copy of a room's name. Owned here rather than inline in
 * WorkshopShell so the pseudo-locale check can see that every rail string has
 * one owner instead of a hardcoded copy.
 */
export const railCopy = {
  brand: PRODUCT_NAME,
  needsYouCountSuffix: "needs you"
} as const;
