<script lang="ts">
  import type { DefectiveRunRow } from "../api/client";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { shortPublicRunReference } from "../lib/fingerprint";
  import { standingMarks } from "../lib/runState";
  import { workbenchPageCopy } from "../lib/workbenchPageCopy";

  /**
   * One listed run whose own projection failed (#1042), the shape every
   * surface that lists runs renders the same way: the Workbench's living
   * shelf and History's ruled rows both stand this row beside the runs they
   * could read, never fold it into an empty state, and never open it -- there
   * is no graph to show for a run this room could not read. `workbenchPageCopy`
   * is this row's one copy owner regardless of which page renders it, so
   * "Could not be read" reads the same everywhere it appears (operator ruling,
   * #1042 review).
   *
   * The mark is `standingMarks.failed`, not a shape of its own: a defective
   * row carries no colour or glyph beyond the failed-state brick already owned
   * by `runState.ts` (operator ruling, #1042 review).
   */
  export let row: DefectiveRunRow;
</script>

<li class="defective-run-row">
  <span class="defective-run-mark" aria-hidden="true">{standingMarks.failed}</span>
  <span class="defective-run-title">{wrapDisplayCopy(workbenchPageCopy.defectiveRunTitle)}</span>
  <span class="defective-run-reference">{shortPublicRunReference(row.public_run_reference)}</span>
  <details class="defective-run-detail">
    <summary>{wrapDisplayCopy(workbenchPageCopy.defectiveRunDetail)}</summary>
    <code>{row.detail}</code>
  </details>
</li>

<style>
  .defective-run-row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-2) var(--space-3);
    min-height: var(--tap);
    border: var(--edge) solid var(--signal-failure);
    border-radius: var(--r-lg);
    padding: var(--space-3) var(--space-4);
    background: var(--panel2);
    font-size: var(--text-sm);
  }

  .defective-run-mark {
    color: var(--signal-failure);
  }

  .defective-run-title {
    font-weight: var(--weight-strong);
  }

  .defective-run-reference {
    margin-left: auto;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  .defective-run-detail {
    flex-basis: 100%;
    font-size: var(--text-xs);
  }

  .defective-run-detail summary {
    display: flex;
    align-items: center;
    min-height: var(--tap);
    cursor: pointer;
    color: var(--ink-dim);
  }

  .defective-run-detail code {
    display: block;
    overflow-wrap: anywhere;
  }
</style>
