from __future__ import annotations

from atelier2.contracts.agent_transcripts import (
    MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES,
    MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    AssistantTurn,
    AttemptTranscript,
)

_FULL_WIDTH_STEPS = 127
_CLOSING_STEP_CHARACTERS = 1_237


def largest_attempt_transcript() -> AttemptTranscript:
    """The widest document `AttemptTranscript` will keep, filled to the byte.

    Full-width steps first, then one closing step cut to spend exactly what the
    document bound has left. The two widths are a packing, not a contract, so
    they are checked against the bound here rather than trusted: reshaping a
    stored fragment fails this one builder loudly instead of quietly shrinking
    every test that asks for a maximum-sized transcript.
    """

    transcript = AttemptTranscript.of(
        [
            *(
                AssistantTurn("x" * MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
                for _ in range(_FULL_WIDTH_STEPS)
            ),
            AssistantTurn("x" * _CLOSING_STEP_CHARACTERS),
        ]
    )
    if len(transcript.document) != MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES:
        raise AssertionError(
            "the transcript packing no longer fills the document bound: "
            f"{len(transcript.document)} of {MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES} bytes"
        )
    return transcript
