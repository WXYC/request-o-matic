"""Prompt-contract regression guard for services.parser.SYSTEM_PROMPT.

Bug context: WXYC/request-o-matic#162 -- "Today, Jefferson Airplane" parsed as
song="Jefferson Airplane", artist=null because Groq treated the leading short
common word "Today," as a temporal/conversational preamble and dropped it.

The fix is prompt-only (no behavior code changed). A mocked-Groq unit test would
be vacuous here -- the mock returns whatever JSON we feed it and exercises none
of the prompt. So instead of asserting parser *behavior* (that's the manual
external_api integration test), this asserts the prompt *contract*: that
SYSTEM_PROMPT still encodes the #162 rule, so it can't be silently deleted in a
refactor.

It asserts at the *concept* level (the distinguishing phrase the rule
introduces) rather than pinning a literal example sentence, so a benign reword
-- e.g. swapping the example artist -- doesn't fail CI. See docs/testing.md
"Prompt-only parser fixes".
"""

from __future__ import annotations

from services.parser import SYSTEM_PROMPT

# Compare case-insensitively: the rule's meaning, not its casing, is the contract.
_PROMPT = SYSTEM_PROMPT.lower()


def test_prompt_documents_short_temporal_word_as_song() -> None:
    """#162: a short word resembling a temporal adverb can be the song title.

    This is the distinguishing concept the fix adds to the comma/terse rule;
    if it disappears, the parser regresses to dropping "Today," as preamble.
    """
    assert "temporal adverb" in _PROMPT, (
        "SYSTEM_PROMPT no longer documents that a short temporal-adverb-like word "
        "can be the song title (regression guard for #162)"
    )
