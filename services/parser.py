import json
import logging
from enum import StrEnum

from groq import Groq
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class MessageType(StrEnum):
    REQUEST = "request"
    DJ_MESSAGE = "dj_message"
    FEEDBACK = "feedback"
    OTHER = "other"


class ParsedRequest(BaseModel):
    song: str | None = None
    album: str | None = None
    artist: str | None = None
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
- Correct obvious typos when you can confidently identify the intended artist/song, but don't remove intentional special characters. Never invent or substitute artist/song/album names that aren't present in the original message -- extract only what the listener actually wrote. If the message provides an explicit artist name (via comma, dash, or "by" format), always use that name even if you recognize the song as being performed by a different, more famous artist. For example, "me and mr jones, plug" means song="Me And Mr Jones", artist="Plug" -- do NOT replace "Plug" with a known performer of that song like Loudon Wainwright III or Amy Winehouse.
- Common greetings and pleasantries like "good morning", "hi", "hey", "hello", "good evening", "howdy" at the start of a message are NOT song titles -- they are conversational preamble. Ignore them when extracting metadata. For example, "Good morning I would love to hear Sara by Fleetwood Mac" means song="Sara", artist="Fleetwood Mac" -- "Good morning" is a greeting, not a song.
- In phrases like "I would like to hear X from Y", "from" can indicate either an album or an artist. Use context to decide: if Y is recognizably an artist or band name, treat it as the artist. For example, "Sarah from Fleetwood Mac" means song="Sara", artist="Fleetwood Mac" because Fleetwood Mac is a band.
- If someone says "anything by X" or "any song off Y album", that's still a request
- A message can be both a dj_message AND contain a request (is_request: true)
- Terse messages like "song title. artist name.", "song - artist", "song title, artist name", or "song by artist" should extract both song and artist. The word "by" in "X by Y" is a preposition indicating authorship -- Y is the artist, not an album. This applies even when the song title contains common words like "love", "hate", "like", etc. For example, "I love acid, luke vibert" is a request for the song "I Love Acid" by Luke Vibert, not feedback.
- Dashes (with or without spaces) are delimiters separating song, artist, and album. "Song-Artist-Album" or "Song - Artist - Album" should be split into three fields. For example, "Spoonful-Cream-Wheels of Fire lp" means song="Spoonful", artist="Cream", album="Wheels of Fire".
- Words like "lp", "cd", "vinyl", "7\"", "12\"", "45" at the end of a message are physical format descriptors, not album names. Ignore them. For example, in "Spoonful-Cream-Wheels of Fire lp", "lp" is a format descriptor and "Wheels of Fire" is the album.
- Words like "some", "something", "anything", "any", "a little", "a bit of", "more" before "by"/"from" + artist name are filler/determiners meaning "play [some quantity of] artist", NOT song titles. For example, "Some phil collins please" means play some Phil Collins -- "some" is not a song title. "Something by Helden" means play anything by Helden -- "something" is not a song title. Set song to null in these cases.
- When the message is just a name like "MJ Lenderman" or "Radiohead" with no song or album context, it's an artist request. Set artist to the name and song to null.
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
        raise ValueError(f"Invalid JSON response from Groq: {e}") from e
    except Exception as e:
        logger.error(f"Error parsing request: {e}")
        raise
