<script lang="ts">
  import { shortFingerprint } from "../lib/fingerprint";

  /**
   * A fact that proves something, written where it can be read.
   *
   * Label, value and what the value seals stand together and stay on screen:
   * a label whose value only appears after a click is a riddle, and a round ⓘ
   * button beside it reads as a control that starts something (operator,
   * 23.08.). The exact bytes are one copy away, and the shortened form on
   * screen is enough to compare against a receipt.
   *
   * This lives only where proof belongs — the node panel's Evidence tab and
   * the expert reveals of the start door — never on a main surface.
   */
  export let label: string;
  export let seals: string;
  export let value: string;
  /**
   * Whether this anchor's own sentence opens with the word "Seals".
   *
   * A list of several anchors in a row (the node panel's run evidence,
   * #579's befund 7.5) saying "Seals" six times reads as wallpaper, not six
   * facts; a caller that already says it once for the whole list passes
   * `false` here and this prints only what each anchor seals, its first
   * letter capitalised so the sentence still reads whole on its own.
   */
  export let sealsPrefix = true;

  $: sealsSentence = sealsPrefix
    ? `Seals ${seals}.`
    : `${seals.charAt(0).toUpperCase()}${seals.slice(1)}.`;

  let copied = false;

  async function copy(): Promise<void> {
    copied = false;
    const clipboard = globalThis.navigator.clipboard;
    if (clipboard === undefined) return;
    try {
      await clipboard.writeText(value);
      copied = true;
    } catch {
      // The value is readable on screen either way; a refused clipboard is not
      // a failure of this control, so it says nothing rather than alarming.
      copied = false;
    }
  }
</script>

<span class="proof-anchor" role="group" aria-label={label}>
  <span class="proof-label">{label}</span>
  <span class="proof-line">
    <code class="proof-value">{shortFingerprint(value)}</code>
    <button class="reveal-affordance" type="button" aria-label={`Copy ${label}`} onclick={() => { void copy(); }}>
      {copied ? "Copied" : "Copy"}
    </button>
  </span>
  <span class="proof-seals">{sealsSentence}</span>
</span>
