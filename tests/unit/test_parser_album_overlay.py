"""Unit tests for the album-extraction overlay inside parse_request.

WXYC/request-o-matic#140 -- when the deterministic pre-pass extracts an album,
parse_request must overlay it on top of whatever Groq returns (so the final
result is deterministic for the canonical phrasings). The Groq call still
happens because the rest of the fields (song, artist, message_type, etc.) need
LLM extraction with typo correction.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import pytest

from services.parser import MessageType, parse_request


def _make_mock_groq(parsed_payload: dict, completion_tokens: int = 12) -> Mock:
    """Build a Groq client mock whose chat.completions.create returns parsed_payload."""
    client = Mock()
    client.chat = Mock()
    client.chat.completions = Mock()

    response = Mock()
    response.choices = [Mock()]
    response.choices[0].message = Mock()
    response.choices[0].message.content = json.dumps(parsed_payload)
    response.usage = Mock()
    response.usage.prompt_tokens = 100
    response.usage.completion_tokens = completion_tokens

    client.chat.completions.create = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_overlay_fills_album_when_groq_returned_null() -> None:
    """When the pre-pass extracts an album and Groq returned null, overlay wins."""
    client = _make_mock_groq(
        {
            "song": "tower of dub",
            "album": None,  # Groq missed it (the 2-out-of-3 failure mode in #140)
            "artist": "The Orb",
            "is_request": True,
            "message_type": "request",
        }
    )

    result = await parse_request("tower of dub by the orb on live '93", client)

    assert result.album is not None
    assert result.album.lower() == "live '93"
    assert result.song == "tower of dub"
    assert result.artist == "The Orb"
    assert result.is_request is True
    assert result.message_type == MessageType.REQUEST


@pytest.mark.asyncio
async def test_overlay_replaces_album_when_groq_returned_wrong_value() -> None:
    """When the pre-pass extracts an album, it overrides whatever Groq said.

    The pre-pass is exact and deterministic; trust it over the LLM for the album
    slot. Other slots (song, artist) still come from Groq.
    """
    client = _make_mock_groq(
        {
            "song": "tower of dub",
            "album": "Something Else Entirely",
            "artist": "The Orb",
            "is_request": True,
            "message_type": "request",
        }
    )

    result = await parse_request("tower of dub by the orb on live '93", client)

    assert result.album is not None
    assert result.album.lower() == "live '93"


@pytest.mark.asyncio
async def test_no_overlay_when_pre_pass_declines() -> None:
    """Idioms like 'on the radio' must not trigger overlay; Groq's null wins."""
    client = _make_mock_groq(
        {
            "song": "Moon Pix",
            "album": None,
            "artist": "Cat Power",
            "is_request": True,
            "message_type": "request",
        }
    )

    result = await parse_request("moon pix by cat power on the radio", client)

    assert result.album is None
    assert result.artist == "Cat Power"


@pytest.mark.asyncio
async def test_no_overlay_and_full_message_when_album_tail_carries_artist() -> None:
    """Reverse-order "<song> on <album> by <artist>": pre-pass declines.

    The trailing "by <artist>" clause would otherwise be swallowed into the album
    (and the artist nulled). The pre-pass declines, so Groq sees the *whole*
    message -- artist clause intact -- and its artist survives. No album overlay.
    """
    client = _make_mock_groq(
        {
            "song": "Conspiracy on Neptune",
            "album": None,
            "artist": "Prince Jammy",
            "is_request": True,
            "message_type": "request",
        }
    )

    result = await parse_request("conspiracy on neptune by prince jammy", client)

    # No overlay: Groq's null album stands (not the polluted "neptune by prince jammy").
    assert result.album is None
    assert result.artist == "Prince Jammy"

    # Pre-pass declined -> Groq received the whole message, "by <artist>" and album
    # text both present (nothing was stripped).
    call_args = client.chat.completions.create.await_args
    user_msg = next(m for m in call_args.kwargs["messages"] if m["role"] == "user")
    user_content = user_msg["content"].lower()
    assert "by prince jammy" in user_content
    assert "neptune" in user_content


@pytest.mark.asyncio
async def test_groq_receives_stripped_message_when_pre_pass_fires() -> None:
    """Pre-pass strips the trailing 'on <album>' suffix before Groq sees it.

    This isolates the rest of the parse from the album text, so Groq doesn't
    get confused about which token is song vs album.
    """
    client = _make_mock_groq(
        {
            "song": "tower of dub",
            "album": None,
            "artist": "The Orb",
            "is_request": True,
            "message_type": "request",
        }
    )

    await parse_request("tower of dub by the orb on live '93", client)

    # Pull the user message that was sent to Groq.
    call_args = client.chat.completions.create.await_args
    messages = call_args.kwargs["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    user_content = user_msg["content"]

    # The "on live '93" suffix has been consumed; Groq sees only the song+artist
    # portion (case may have been preserved by the regex's pass-through).
    assert "live '93" not in user_content.lower()
    assert "tower of dub" in user_content.lower()
    assert "the orb" in user_content.lower()


@pytest.mark.asyncio
async def test_overlay_works_for_from_phrasing() -> None:
    client = _make_mock_groq(
        {
            "song": "la paradoja",
            "album": None,
            "artist": "Juana Molina",
            "is_request": True,
            "message_type": "request",
        }
    )

    result = await parse_request("la paradoja by juana molina from doga", client)

    assert result.album is not None
    assert result.album.lower() == "doga"


@pytest.mark.asyncio
async def test_overlay_works_for_off_phrasing() -> None:
    """Bare `off` (no `of`) overlays the album through parse_request.

    Covers the bare-`off` preposition deterministically so the live-Groq E2E
    smoke in test_integration.py is not the only coverage of this branch.
    """
    client = _make_mock_groq(
        {
            "song": "Back, Baby",
            "album": None,
            "artist": "Jessica Pratt",
            "is_request": True,
            "message_type": "request",
        }
    )

    result = await parse_request("back, baby by jessica pratt off on your own love again", client)

    assert result.album is not None
    assert result.album.lower() == "on your own love again"


@pytest.mark.asyncio
async def test_overlay_strips_descriptor_and_trailing_feedback() -> None:
    """Production repro: "<song> by <artist> from album <title>....  <feedback>".

    The pre-pass must (a) drop the leading "album" descriptor noun and (b) not
    swallow the appended feedback sentence into the album, while Groq -- which
    only ever sees the stripped song+artist prefix -- returns song/artist cleanly.
    """
    client = _make_mock_groq(
        {
            "song": "Anvil Will Fall",
            "album": None,
            "artist": "Harvey Milk",
            "is_request": True,
            "message_type": "request",
        }
    )

    message = (
        "anvil will fall by harvey milk from album my love is....     "
        "thanks for your set we're enjoying it!!!"
    )
    result = await parse_request(message, client)

    assert result.album is not None
    assert result.album.strip().lower() == "my love is"
    assert result.song == "Anvil Will Fall"
    assert result.artist == "Harvey Milk"

    # Groq saw only the song+artist prefix -- no descriptor, no album, no feedback.
    call_args = client.chat.completions.create.await_args
    user_msg = next(m for m in call_args.kwargs["messages"] if m["role"] == "user")
    user_content = user_msg["content"].lower()
    assert "album" not in user_content
    assert "my love is" not in user_content
    assert "thanks" not in user_content
    assert "anvil will fall by harvey milk" in user_content


@pytest.mark.asyncio
async def test_overlay_works_for_off_of_phrasing() -> None:
    client = _make_mock_groq(
        {
            "song": "Back, Baby",
            "album": None,
            "artist": "Jessica Pratt",
            "is_request": True,
            "message_type": "request",
        }
    )

    result = await parse_request(
        "back, baby by jessica pratt off of on your own love again", client
    )

    assert result.album is not None
    assert result.album.lower() == "on your own love again"
