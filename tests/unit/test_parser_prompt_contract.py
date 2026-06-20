"""Prompt-contract regression guards for services.parser.SYSTEM_PROMPT.

Bug context: WXYC/request-o-matic#162 -- "Today, Jefferson Airplane" parsed as
song="Jefferson Airplane", artist=null because Groq treated the leading short
common word "Today," as a temporal/conversational preamble and dropped it.

The fix is prompt-only (no behavior code changed). A mocked-Groq unit test would
be vacuous here -- the mock returns whatever JSON we feed it and exercises none
of the prompt. So instead of asserting parser *behavior* (that's the manual
external_api integration test), these tests assert the prompt *contract*: the
SYSTEM_PROMPT continues to encode the comma-shape-short-word rule and its
greeting-asymmetry counterpart, so the rule can't be silently deleted or reworded
away. See docs/testing.md "Bug Fix Protocol" for when this prompt-contract
pattern stands in for the mocked unit half on prompt-only parser fixes.
"""

from __future__ import annotations

from services.parser import SYSTEM_PROMPT

# Normalize to lowercase once: the rule's meaning, not its casing, is the contract.
_PROMPT = SYSTEM_PROMPT.lower()


def test_prompt_documents_comma_shape() -> None:
    """The terse '<song>, <artist>' comma shape must remain documented."""
    assert "song title, artist name" in _PROMPT, (
        "SYSTEM_PROMPT no longer documents the 'song title, artist name' comma shape"
    )


def test_prompt_covers_short_temporal_word_on_left_of_comma() -> None:
    """The comma rule must cover a short common word that resembles a temporal adverb.

    Guards WXYC/request-o-matic#162: the canonical example pins the behavior so a
    future reword can't quietly drop the short-word coverage.
    """
    assert "today, jefferson airplane" in _PROMPT, (
        "SYSTEM_PROMPT no longer carries the 'Today, Jefferson Airplane' short-word "
        "comma example (regression guard for #162)"
    )
    # The example must map the short word to the SONG slot, not the artist slot.
    assert 'song="today"' in _PROMPT, (
        "SYSTEM_PROMPT no longer maps the short leading word 'Today' to song= "
        "in the comma-shape example"
    )


def test_prompt_preserves_greeting_asymmetry() -> None:
    """A genuine greeting is still dropped, so the fix must not over-generalize.

    The greeting rule ("good morning" is preamble) must coexist with the
    short-word rule ("Today" is a song), preserving the asymmetry #162 calls out.
    """
    assert "good morning" in _PROMPT, (
        "SYSTEM_PROMPT no longer documents that greetings like 'good morning' are preamble"
    )
