<script lang="ts">
  import { tick } from "svelte";

  import { chatPageCopy } from "../lib/chatPageCopy";
  import { currentChatTranscript, sendChatTurn, type ChatMessage } from "../lib/chatTranscript";
  import { wrapDisplayCopy } from "../lib/displayCopy";

  export let navigate: (path: string) => void;

  let transcript: readonly ChatMessage[] = currentChatTranscript();
  let typed = "";
  let composer: { focus(): void };

  const speakerLabels: Record<ChatMessage["speaker"], string> = {
    you: chatPageCopy.youLabel,
    house: chatPageCopy.houseLabel
  };

  async function send(event: Event): Promise<void> {
    event.preventDefault();
    const sent = sendChatTurn(typed);
    if (sent === transcript) return;
    transcript = sent;
    typed = "";
    await tick();
    composer.focus();
  }

  /**
   * Enter sends, Shift+Enter keeps writing — the shape every chat composer has,
   * and the reason the field is a textarea rather than a single-line input: a
   * message to the house is often more than one line.
   */
  function keydown(event: KeyboardEvent): void {
    if (event.key !== "Enter" || event.shiftKey) return;
    void send(event);
  }
</script>

<section class="chat-page surface" aria-labelledby="chat-title">
  <header class="surface-head">
    <p class="eyebrow">{wrapDisplayCopy(chatPageCopy.eyebrow)}</p>
    <h1 id="chat-title">{wrapDisplayCopy(chatPageCopy.title)}</h1>
  </header>

  {#if transcript.length === 0}
    <div class="chat-empty card empty-state">
      <h2>{wrapDisplayCopy(chatPageCopy.emptyTitle)}</h2>
      <p>{wrapDisplayCopy(chatPageCopy.emptyDescription)}</p>
      <a
        class="button primary"
        href="/atelier/workflows"
        onclick={(event) => { event.preventDefault(); navigate("/atelier/workflows"); }}
      >{wrapDisplayCopy(chatPageCopy.emptyNext)}</a>
    </div>
  {:else}
    <ol class="chat-transcript" aria-label={wrapDisplayCopy(chatPageCopy.transcriptLabel)}>
      {#each transcript as message (message.id)}
        <li class="chat-line chat-line-{message.speaker}">
          <p class="chat-message">
            <span class="chat-speaker">{wrapDisplayCopy(speakerLabels[message.speaker])}</span>
            {message.text}
            {#if message.source !== null}
              <span class="chat-source">{message.source}</span>
            {/if}
          </p>
        </li>
      {/each}
    </ol>
  {/if}

  <form class="chat-composer" onsubmit={send}>
    <label class="chat-composer-label" for="chat-message">
      {wrapDisplayCopy(chatPageCopy.composerLabel)}
    </label>
    <div class="chat-composer-row">
      <textarea
        id="chat-message"
        rows="2"
        bind:value={typed}
        bind:this={composer}
        onkeydown={keydown}
      ></textarea>
      <button class="primary" type="submit">{wrapDisplayCopy(chatPageCopy.send)}</button>
    </div>
  </form>
</section>

<style>
  .chat-empty h2 {
    margin: 0;
  }

  .chat-transcript {
    display: grid;
    gap: var(--space-3);
    max-width: var(--reading-width);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .chat-line {
    display: flex;
    min-width: 0;
  }

  .chat-line-you {
    justify-content: flex-end;
  }

  .chat-message {
    display: grid;
    gap: var(--space-1);
    max-width: 92%;
    margin: 0;
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-3) var(--space-4);
    background: var(--panel2);
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
  }

  .chat-line-you .chat-message {
    border-color: color-mix(in srgb, var(--accent) 30%, var(--line));
    background: color-mix(in srgb, var(--accent) 10%, var(--panel2));
  }

  .chat-speaker {
    color: var(--muted);
    font-size: var(--text-2xs);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .chat-source {
    justify-self: start;
    border: 1px solid var(--line);
    border-radius: var(--r-pill);
    padding: 0 var(--space-2);
    color: var(--muted);
    background: var(--chip);
    font-size: var(--text-2xs);
  }

  .chat-composer {
    display: grid;
    gap: var(--space-2);
    max-width: var(--reading-width);
  }

  .chat-composer-label {
    color: var(--muted);
    font-size: var(--text-2xs);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .chat-composer-row {
    display: flex;
    align-items: flex-end;
    gap: var(--space-3);
  }

  .chat-composer-row textarea {
    flex: 1;
    font-family: var(--sans);
    font-size: var(--text-sm);
  }
</style>
