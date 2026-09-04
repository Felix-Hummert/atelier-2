/**
 * The one place the workshop reloads its own page (#1100).
 *
 * Never called on its own -- the operator's wish behind #1100 keeps a
 * confirmed page from disappearing out from under them, so this is reached
 * only from the footer's own reload control, on a click. Named apart from
 * `window.location.reload` directly so that one seam, not the browser global,
 * is what a test doubles.
 */
export function reloadPage(): void {
  window.location.reload();
}
