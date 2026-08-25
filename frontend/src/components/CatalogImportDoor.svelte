<script lang="ts">
  import { CockpitRequestError, type Problem } from "../api/client";
  import { catalogPageCopy } from "../lib/catalogPageCopy";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import ProblemNotice from "./ProblemNotice.svelte";

  /**
   * One door for bringing an authored file into the catalog.
   *
   * A file and a paste are the same act with two ways in, so they share one
   * document and one Import: choosing a file fills the text, which lets the
   * operator read and correct what they are about to publish instead of
   * sending bytes they never saw.
   *
   * The door never guesses at a refusal. What the API named, the API's own
   * problem says (`ProblemNotice`); only a failure with no problem body falls
   * back to this door's sentence.
   */
  export let title: string;
  export let hint: string;
  export let label: string;
  export let accept: string;
  export let fieldId: string;
  export let failureTitle: string;
  export let onImport: (document: string) => Promise<void>;

  let draft = "";
  let busy = false;
  let problem: Problem | null = null;
  let failure: string | null = null;
  let succeeded = false;

  async function chooseFile(event: Event): Promise<void> {
    const chosen = (event.currentTarget as HTMLInputElement).files?.[0];
    if (chosen === undefined) return;
    clearVerdict();
    try {
      draft = await chosen.text();
    } catch (error) {
      failure = humanErrorMessage(error, catalogPageCopy.fileUnreadable);
    }
  }

  function clearVerdict(): void {
    problem = null;
    failure = null;
    succeeded = false;
  }

  async function submit(): Promise<void> {
    clearVerdict();
    if (draft.trim() === "") {
      failure = catalogPageCopy.emptyDocument;
      return;
    }
    busy = true;
    try {
      await onImport(draft);
      succeeded = true;
      draft = "";
    } catch (error) {
      if (error instanceof CockpitRequestError && error.problem !== null) {
        problem = error.problem;
      } else {
        failure = humanErrorMessage(error, failureTitle);
      }
    } finally {
      busy = false;
    }
  }
</script>

<section class="import-door card" aria-labelledby={`${fieldId}-title`}>
  <h3 id={`${fieldId}-title`}>{wrapDisplayCopy(title)}</h3>
  <p class="muted">{wrapDisplayCopy(hint)}</p>

  <label class="field-label" for={fieldId}>{wrapDisplayCopy(label)}</label>
  <textarea
    id={fieldId}
    rows="8"
    spellcheck="false"
    disabled={busy}
    bind:value={draft}
    oninput={clearVerdict}
  ></textarea>

  <div class="import-actions">
    <!-- The native control brings its own button and its own "no file chosen",
         both in the browser's language, which would put two labels and a
         sentence nobody needs beside one act. The input keeps every keyboard
         and assistive-technology behaviour it has; only its chrome goes. -->
    <label class="file-choice button">
      {wrapDisplayCopy(catalogPageCopy.chooseFile)}
      <input class="visually-hidden" type="file" {accept} disabled={busy} onchange={chooseFile} />
    </label>
    <button class="primary" type="button" disabled={busy} onclick={() => { void submit(); }}
      >{wrapDisplayCopy(busy ? catalogPageCopy.importing : catalogPageCopy.importAction)}</button
    >
  </div>

  {#if problem !== null}
    <ProblemNotice title={failureTitle} {problem} />
  {/if}
  {#if failure !== null}
    <p class="failure" role="alert">{failure}</p>
  {/if}
  {#if succeeded}
    <p class="succeeded" role="status">{wrapDisplayCopy(catalogPageCopy.imported)}</p>
  {/if}
</section>

<style>
  .import-door {
    display: grid;
    gap: var(--space-2);
  }

  h3 {
    margin: 0;
    font-size: var(--text-md);
  }

  p {
    margin: 0;
    font-size: var(--text-xs);
  }

  .muted {
    color: var(--ink-dim);
  }

  .failure {
    color: var(--signal-failure);
  }

  .import-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    margin-top: var(--space-2);
  }

  .file-choice {
    cursor: pointer;
  }

  .file-choice:has(input:focus-visible) {
    outline: var(--edge) solid var(--accent);
    outline-offset: var(--space-1);
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
