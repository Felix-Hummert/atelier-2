/**
 * Words for the one frame a poisoned mutation journal shows (#914, mockup v8
 * §"Journal poisoned", `#v8-21-journal-poisoned`).
 *
 * Shared by every room that could be reading a pending sending when the
 * browser's own memory of it turns out unreadable -- today the Workbench;
 * the run page follows in its own slice. The door is deliberately never
 * called "Discard": that word already belongs, in `runPageCopy`, to leaving a
 * single uncertain send unresolved. The confirmation reuses the same three
 * facts (`Disappears` / `Stays` / `Permanent`) the catalog's own retire card
 * asks with, per the mockup's own note that no new vocabulary is needed.
 */
export const journalPoisonedCopy = {
  sentence: "Your remembered sendings can't be read.",
  door: "Forget them",
  confirmLabel: "Confirm forgetting the remembered sendings",
  confirmQuestion: "Forget everything remembered here?",
  disappears: "Disappears",
  disappearsFact: "Every pending cancel, wait answer and retry this browser remembers",
  stays: "Stays",
  staysFact: "A wait answer already sent — the run keeps it; only your local copy of it is gone",
  permanent: "Permanent",
  permanentFact: "There is no way back.",
  confirm: "Forget them",
  cancel: "Cancel",
  technical: "Technical",
  rawContentLabel: "The exact stored text, before it is forgotten",
  discardFailure: "This browser's memory could not be forgotten.",
  /** The receipt at display time (#914 line 12): no second ledger survives the
   * same poisoned storage, so this is read once, right after the discard,
   * and is gone once the room is left. */
  forgottenReceipt: (atLocal: string, byteCount: number) =>
    `Forgotten at ${atLocal} — ${byteCount} ${byteCount === 1 ? "byte" : "bytes"} gone.`
} as const;
