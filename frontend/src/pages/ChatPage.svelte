<script lang="ts">
  import { tick } from "svelte";

  import { chatPageCopy } from "../lib/chatPageCopy";
  import { currentChatTranscript, sendChatTurn, type ChatMessage } from "../lib/chatTranscript";
  import { wrapDisplayCopy } from "../lib/displayCopy";

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
    <h1 id="chat-title">{wrapDisplayCopy(chatPageCopy.title)}</h1>
  </header>

  {#if transcript.length === 0}
    <div class="chat-empty card empty-state">
      <h2>{wrapDisplayCopy(chatPageCopy.emptyTitle)}</h2>
      <p>{wrapDisplayCopy(chatPageCopy.emptyDescription)}</p>
    </div>
  {:else}
    <ol class="chat-transcript" aria-label={wrapDisplayCopy(chatPageCopy.transcriptLabel)}>
      {#each transcript as message (message.id)}
        <li class="chat-line chat-line-{message.speaker}">
          <p class="chat-message">
            <span class="chat-speaker">{wrapDisplayCopy(speakerLabels[message.speaker])}</span>
            {message.text}
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
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-3) var(--space-4);
    background: var(--panel2);
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
  }

  /* Your own line is the paper one shade deeper, not a grey pasted onto a
     warm house: the tint is mixed from the ground the workshop already uses. */
  .chat-line-you .chat-message {
    border-color: color-mix(in srgb, var(--ink) 20%, var(--line));
    background: var(--chip);
  }

  .chat-speaker {
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-strong);
  }

  .chat-composer {
    display: grid;
    gap: var(--space-2);
    max-width: var(--reading-width);
  }

  .chat-composer-label {
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
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
