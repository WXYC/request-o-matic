"""WX-1.2.4 detector: catches Pydantic v2 / Slack JSON regressions that would
silently re-encode user-supplied strings before they leave the service."""

from __future__ import annotations

import json

import pytest

from services.parser import MessageType, ParsedRequest
from services.slack import build_slack_blocks
from tests.charset_torture import CharsetTortureEntry, entry_id, iter_entries

CORPUS_ENTRIES = list(iter_entries())


@pytest.mark.parametrize("entry", CORPUS_ENTRIES, ids=entry_id)
def test_parsed_request_json_roundtrip(entry: CharsetTortureEntry) -> None:
    """ParsedRequest must round-trip every corpus entry through Pydantic v2 JSON."""
    original = ParsedRequest(
        song=entry["input"],
        album=entry["input"],
        artist=entry["input"],
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message=entry["input"],
    )
    serialized = original.model_dump_json()
    restored = ParsedRequest.model_validate_json(serialized)

    where = f"{entry['category']}: {entry['notes']}"
    assert restored.song == entry["input"], where
    assert restored.album == entry["input"], where
    assert restored.artist == entry["input"], where
    assert restored.raw_message == entry["input"], where


@pytest.mark.parametrize("entry", CORPUS_ENTRIES, ids=entry_id)
def test_slack_block_json_roundtrip(entry: CharsetTortureEntry) -> None:
    """Slack block builder must preserve the message bytes through JSON encode+decode."""
    blocks = build_slack_blocks(message=entry["input"], items_with_artwork=[])
    payload = json.dumps({"blocks": blocks}, ensure_ascii=False)
    restored = json.loads(payload)

    text = restored["blocks"][0]["text"]["text"]
    where = f"{entry['category']}: {entry['notes']}"
    # Slack block builder wraps the message in *...* (mrkdwn bold). Strip and compare.
    assert text == f"*{entry['input']}*", where
