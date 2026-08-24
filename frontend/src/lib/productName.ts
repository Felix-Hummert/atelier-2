/**
 * The one owner of the product's visible name (#515, operator ruling 22.08.).
 * The name layering is deliberate: the product says "atelier", the GitHub
 * repository stays `atelier-2`, and technical identifiers (Python package,
 * CLI, URNs, storage keys) stay `atelier2`. Every user-visible surface reads
 * this constant so a future rename is one edit, never a sweep.
 */
export const PRODUCT_NAME = "atelier";
