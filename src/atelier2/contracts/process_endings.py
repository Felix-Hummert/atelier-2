"""What a receipt keeps in words about one provider process and its answer.

An attempt's own record is `agent_attempts`; this is the smaller question of
how a process's ending, and the bytes it answered with, are said in the one
bounded sentence a durable receipt holds. Bounded because a receipt is read at
a glance, and redacted where the words are the provider's own.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.agent_transcripts import (
    AttemptTranscript,
    ProviderTerminalRefusal,
)
from atelier2.contracts.secret_redaction import redact_credentials

MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES = 2_048
"""How much of a dead process's standard error one durable receipt keeps.

Supervision already bounds how much a child may say at all; this is the far
smaller bound of how much of it becomes a *receipt*, because a receipt is a
sentence an operator reads at a glance, not a log. One owner for the number, so
no writer of a receipt picks its own.
"""


MAXIMUM_RECEIPTED_AGENT_ANSWER_BYTES = 2_048
"""How much of what a provider answered one durable receipt keeps as words.

Sibling to `MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES`, and the same width for the
same reason: a receipt is a sentence an operator reads at a glance. Kept from
the *start* of the answer rather than its end, because an answer is a document
whose opening fields are the ones a reader came for -- a tail would cut off the
beginning of the very summary it exists to show.
"""


def _readable(said: bytes) -> str:
    """Provider bytes as text a receipt may hold and a terminal may print.

    A tail is cut at a byte boundary, so a character the cut split is replaced
    rather than raised over. Control characters are replaced for a second
    reason: a provider that writes terminal escape sequences to standard error
    would otherwise move the cursor of every operator who prints this reason.
    """
    return "".join(
        character if character.isprintable() or character in "\n\t" else "\ufffd"
        for character in said.decode("utf-8", "replace")
    )


@dataclass(frozen=True)
class ProcessExitSignature:
    """How one provider process ended, in the words its receipt keeps.

    `return_code` follows the convention the process runner reports it under: a
    negative value is the signal that killed the child. Zero is not a
    contradiction beside `PROCESS_EXITED_UNSUCCESSFULLY` -- an executor reaches
    that code just as well for a child that exited cleanly and left an answer
    the provider's own wire format cannot carry -- so the named verdict says
    which of the three happened instead of implying an exit that never did.

    Standard error travels here and nowhere else: it is the only place the
    reason a process died is written down, and it is deliberately not on the
    event stream, which stays the bounded, secret-free surface it is.
    """

    return_code: int
    standard_error: bytes

    def __post_init__(self) -> None:
        if type(self.return_code) is not int:
            raise TypeError("a process exit signature names an integer return code")
        if type(self.standard_error) is not bytes:
            raise TypeError("a process exit signature carries standard error bytes")

    def named(self) -> str:
        """The one sentence a receipt keeps about this exit."""
        return f"{self._ended}; {self._said}"

    @property
    def _ended(self) -> str:
        if self.return_code < 0:
            return f"killed by signal {-self.return_code}"
        if self.return_code > 0:
            return f"exited with code {self.return_code}"
        return "exited with code 0 leaving an answer its executor could not read"

    @property
    def _said(self) -> str:
        if not self.standard_error:
            return "it wrote nothing to standard error"
        tail = _readable(self.standard_error[-MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES:])
        if len(self.standard_error) <= MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES:
            return f"standard error: {tail}"
        return (
            f"last {MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES} of "
            f"{len(self.standard_error)} standard error bytes: {tail}"
        )


def receipted_agent_answer(answer: bytes) -> str:
    """What the provider answered, in the bounded words a receipt may keep.

    Redacted before it is kept, unlike the transcript this answer travels
    beside: a receipt reason is durable material an operator reads and the run
    page shows, so a credential a provider echoed out of its own tooling must
    not survive into it.
    """

    if not answer:
        return "nothing"
    head = redact_credentials(
        _readable(answer[:MAXIMUM_RECEIPTED_AGENT_ANSWER_BYTES])
    ).text
    if len(answer) <= MAXIMUM_RECEIPTED_AGENT_ANSWER_BYTES:
        return head
    return (
        f"first {MAXIMUM_RECEIPTED_AGENT_ANSWER_BYTES} of {len(answer)} "
        f"answer bytes: {head}"
    )


def process_exit_verdict(
    exit_signature: ProcessExitSignature, transcript: AttemptTranscript | None
) -> str:
    """The one sentence a receipt keeps about a failed process, honestly sourced.

    An exit code and an empty standard error explain nothing about a call the
    provider itself read and refused before it did anything (`#1029`, `#942`):
    the process behaved exactly as designed, and the shell around it is not
    where that refusal was said. Where the transcript carries the provider's
    own named refusal, the receipt keeps that instead of the exit signature's
    silence; every other ending -- a crash, a timeout, a supervision failure --
    still gets the exit signature's own words, unchanged. Any provider's
    transcript may carry the refusal step, not only Claude's: the vocabulary is
    neutral even though one adapter is the only writer of it today.
    """

    refusal = _terminal_refusal(transcript)
    if refusal is None:
        return exit_signature.named()
    return f"provider-reported: {refusal.terminal_reason}: {refusal.text}"


def _terminal_refusal(
    transcript: AttemptTranscript | None,
) -> ProviderTerminalRefusal | None:
    if transcript is None:
        return None
    for event in transcript.events:
        if isinstance(event, ProviderTerminalRefusal):
            return event
    return None
