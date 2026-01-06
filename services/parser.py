import json
import logging
from enum import Enum
from typing import Optional

from groq import Groq
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class MessageType(str, Enum):
    REQUEST = "request"
    DJ_MESSAGE = "dj_message"
    FEEDBACK = "feedback"
    OTHER = "other"


class ParsedRequest(BaseModel):
    song: Optional[str] = None
    album: Optional[str] = None
    artist: Optional[str] = None
    is_request: bool
    message_type: MessageType
    raw_message: str


SYSTEM_PROMPT = """You are a parser for a radio station's song request system. Extract structured metadata from listener messages.

For each message, determine:
1. **song**: The specific song title requested, or null if not specified (e.g., "any song by X")
2. **album**: The album name, or null if not specified
3. **artist**: The artist/band name, or null if not specified
4. **is_request**: true if the listener wants the DJ to play something, false otherwise
5. **message_type**: One of:
   - "request": A song/artist/album request
   - "dj_message": Conversational message to the DJ (may also contain a request)
   - "feedback": Thanks, complaints, technical issues
   - "other": Unclassifiable

Guidelines:
- Normalize artist/song/album names to proper title case
- Preserve intentional stylization like asterisks, numbers, or special characters in artist/song/album names (e.g., "Quix*o*tic" stays "Quix*o*tic", "P!nk" stays "P!nk", "deadmau5" stays "deadmau5")
- Ignore parenthetical asides like "(rip Mani)" or "(2021 remaster)"
- Correct obvious typos when you can confidently identify the intended artist/song, but don't remove intentional special characters
- If someone says "anything by X" or "any song off Y album", that's still a request
- A message can be both a dj_message AND contain a request (is_request: true)
- Terse messages like "song title. artist name.", "song - artist", or "song title, artist name" should extract both song and artist
- When in doubt about whether something is a song title or album, prefer treating it as a song title

Respond with valid JSON only, no markdown formatting."""


USER_PROMPT_TEMPLATE = """Parse this message:

{message}"""


def parse_request(message: str, client: Groq) -> ParsedRequest:
    """Parse a listener message and extract song request metadata."""
    logger.info(f"Parsing message: {message[:100]}...")

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(message=message)},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )

        content = response.choices[0].message.content
        if content is None:
            raise ValueError("Empty response from Groq")

        parsed = json.loads(content)
        logger.debug(f"Raw parsed response: {parsed}")

        return ParsedRequest(
            song=parsed.get("song"),
            album=parsed.get("album"),
            artist=parsed.get("artist"),
            is_request=parsed.get("is_request", False),
            message_type=parsed.get("message_type", MessageType.OTHER),
            raw_message=message,
        )

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        raise ValueError(f"Invalid JSON response from Groq: {e}")
    except Exception as e:
        logger.error(f"Error parsing request: {e}")
        raise
