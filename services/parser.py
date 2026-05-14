import json
import logging
import re
from enum import StrEnum

from groq import AsyncGroq
from pydantic import BaseModel

from core.groq_tracing import groq_parse_span

logger = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.1-8b-instant"


# ---------------------------------------------------------------------------
# Deterministic album-extraction pre-pass (WXYC/request-o-matic#140)
# ---------------------------------------------------------------------------
# The Groq parser intermittently fails to extract the album from canonical
# "<song> by <artist> {on|from|off|off of} <album>" phrasing (1-in-3 failure
# rate observed in production logs). The same input parses inconsistently
# across runs because we run llama-3.1-8b-instant at temperature 0.1 and that
# is the floor of what the model exposes -- there's still nondeterminism.
#
# The fix is a deterministic regex pre-pass. When it matches, parse_request
# strips the trailing "<preposition> <album>" suffix from the message before
# sending to Groq, then overlays the extracted album onto the parsed result.
# This guarantees the album slot is populated for the canonical shape.
#
# False-positive surface: idioms like "on the radio", "on Friday", "on repeat".
# We gate the match on a small denylist of trailing tokens that are commonly
# idiomatic rather than album titles.

# Trailing politeness tokens that the listener appended to the message and
# that the regex would otherwise pull into the album text. Stripped before the
# album is returned.
_TRAILING_POLITENESS_RE = re.compile(
    r"(?:[\s,.!?]+(?:please|thanks|thank\ you|thx))+\s*[.!?]*\s*$",
    re.IGNORECASE | re.VERBOSE,
)


# Trailing words that look like an album in surface form but are idioms.
# Compare against the *first* significant token after the preposition.
_ALBUM_IDIOM_HEADS: frozenset[str] = frozenset(
    {
        "the",  # "on the radio", "off the top of my head"
        "air",  # "on air"
        "vinyl",  # "on vinyl"
        "cd",  # "on cd"
        "wax",  # "on wax"
        "rotation",  # "on rotation"
        "tape",  # "on tape"
        "blast",  # "on blast"
        "repeat",  # "on repeat"
        "loop",  # "on loop"
        "shuffle",  # "on shuffle"
        "fire",  # "on fire"
        "point",  # "on point"
        "hold",  # "on hold"
        "today",  # "on today"
        "tonight",  # "on tonight"
        "tomorrow",  # "on tomorrow"
        "monday",  # "on monday"
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    }
)

# Match the canonical templated shapes. Anchored at end of string.
#   - <prefix> <on|from|off|off of> <album-text>
# `prefix` must be non-empty and is what we pass through to Groq after stripping.
_ALBUM_PREPOSITION_RE = re.compile(
    r"""
    ^
    (?P<prefix>.+?)              # song (+ optional "by <artist>") -- non-empty, non-greedy
    \s+
    (?P<prep>off\ of|off|from|on)   # preposition (order matters: "off of" before "off")
    \s+
    (?P<album>\S.*?)             # album text -- must start with non-space and be non-empty
    \s*
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)


def extract_album_prefix(raw_message: str) -> tuple[str, str] | None:
    """Deterministic album extraction for canonical request phrasings.

    Returns a tuple of ``(album, stripped_message)`` when the message matches
    the canonical "<song> [by <artist>] {on|from|off|off of} <album>" shape and
    the trailing token is not a known idiom. Returns ``None`` otherwise.

    The caller is expected to send ``stripped_message`` to Groq (so the LLM
    extracts song/artist/etc. from the shortened text without album confusion)
    and overlay ``album`` onto the parsed result.
    """
    if not raw_message or not raw_message.strip():
        return None

    match = _ALBUM_PREPOSITION_RE.match(raw_message.strip())
    if match is None:
        return None

    album_raw = match.group("album").strip()
    # Trim trailing politeness ("please", "thanks") that listeners append.
    album_raw = _TRAILING_POLITENESS_RE.sub("", album_raw).strip()
    if not album_raw:
        return None

    # Reject pathological tail tokens like a bare "of" -- the regex eagerly
    # treats "moon pix off of" as preposition="off", album="of". Recognize the
    # "off of" preposition properly: when prep is "off" and album starts with
    # "of " (or is just "of"), it's actually "off of" with no album text.
    prep = match.group("prep").lower()
    if prep == "off" and album_raw.lower() in {"of"}:
        return None
    if prep == "off" and album_raw.lower().startswith("of ") and len(album_raw) <= 3:
        return None

    # Idiom denylist: check the first significant token of the album text.
    # If it's an idiom-head we decline to fire and leave it to Groq.
    first_token = re.split(r"\s+", album_raw, maxsplit=1)[0].lower().rstrip(",.!?")
    if first_token in _ALBUM_IDIOM_HEADS:
        return None

    prefix = match.group("prefix").strip()
    return album_raw, prefix


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
- The word "well" at the start of a message is NOT always a greeting or filler. If the text before "by" forms a recognizable phrase (e.g., "well, you needn't"), treat it as the song title. Only discard "well" when it's clearly conversational (e.g., "well, can you play X?").
- In phrases like "I would like to hear X from Y", "from" can indicate either an album or an artist. Use context to decide: if Y is recognizably an artist or band name, treat it as the artist. For example, "Sarah from Fleetwood Mac" means song="Sara", artist="Fleetwood Mac" because Fleetwood Mac is a band.
- If someone says "anything by X" or "any song off Y album", that's still a request
- A message can be both a dj_message AND contain a request (is_request: true)
- Terse messages like "song title. artist name.", "song - artist", "song title, artist name", or "song by artist" should extract both song and artist. The word "by" in "X by Y" is a preposition indicating authorship -- Y is the artist, not an album. This applies even when the song title contains common words like "love", "hate", "like", etc. For example, "I love acid, luke vibert" is a request for the song "I Love Acid" by Luke Vibert, not feedback.
- Dashes (with or without spaces) are delimiters separating song, artist, and album. "Song-Artist-Album" or "Song - Artist - Album" should be split into three fields. For example, "Spoonful-Cream-Wheels of Fire lp" means song="Spoonful", artist="Cream", album="Wheels of Fire".
- Words like "lp", "cd", "vinyl", "7\"", "12\"", "45" at the end of a message are physical format descriptors, not album names. Ignore them. For example, in "Spoonful-Cream-Wheels of Fire lp", "lp" is a format descriptor and "Wheels of Fire" is the album.
- Words like "some", "something", "anything", "any", "a little", "a bit of", "more" before "by"/"from" + artist name are filler/determiners meaning "play [some quantity of] artist", NOT song titles. For example, "Some phil collins please" means play some Phil Collins -- "some" is not a song title. "Something by Helden" means play anything by Helden -- "something" is not a song title. Set song to null in these cases.
- When the message is just a name like "MJ Lenderman" or "Radiohead" with no song or album context, it's an artist request. Set artist to the name and song to null.
- Editorial phrases like "[Artist]'s best/favorite/greatest album/song" are opinions, NOT actual titles. When followed by a colon or dash and a name, the name after the punctuation is the real title. For example, "Beck's best album: Stereopathic Soulmanure" means artist="Beck", album="Stereopathic Soulmanure", is_request=true -- "Beck's best album" is commentary. Similarly, "my favorite Bjork song: Hyperballad" means artist="Bjork", song="Hyperballad". Ignore surrounding enthusiasm like "It's AMAZING" or "so good". These messages are implicit requests -- the listener is naming what they want played.
- When in doubt about whether something is a song title or album, prefer treating it as a song title

Respond with valid JSON only, no markdown formatting."""


USER_PROMPT_TEMPLATE = """Parse this message:

{message}"""


async def parse_request(message: str, client: AsyncGroq) -> ParsedRequest:
    """Parse a listener message and extract song request metadata."""
    logger.info(f"Parsing message: {message[:100]}...")

    # Deterministic pre-pass for canonical "{on|from|off|off of} <album>"
    # phrasings (WXYC/request-o-matic#140). When it fires we send the stripped
    # message to Groq (so the LLM still extracts song/artist) and overlay the
    # regex-extracted album onto the result.
    pre_pass = extract_album_prefix(message)
    if pre_pass is not None:
        pre_pass_album, groq_message = pre_pass
    else:
        pre_pass_album = None
        groq_message = message

    with groq_parse_span(model=GROQ_MODEL, message=message) as span:
        try:
            response = await client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": USER_PROMPT_TEMPLATE.format(message=groq_message)},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )

            content = response.choices[0].message.content
            if content is None:
                raise ValueError("Empty response from Groq")

            parsed = json.loads(content)
            logger.debug(f"Raw parsed response: {parsed}")

            # If the deterministic pre-pass extracted an album, trust it over
            # whatever Groq returned in that slot (Groq sees the message
            # without the album suffix, so it shouldn't claim one anyway).
            album = pre_pass_album if pre_pass_album is not None else parsed.get("album")

            parsed_request = ParsedRequest(
                song=parsed.get("song"),
                album=album,
                artist=parsed.get("artist"),
                is_request=parsed.get("is_request", False),
                message_type=parsed.get("message_type", MessageType.OTHER),
                raw_message=message,
            )

            span.set_data("ai.output.is_request", parsed_request.is_request)
            span.set_data("ai.output.message_type", parsed_request.message_type.value)
            span.set_data("ai.album_prepass.fired", pre_pass_album is not None)
            usage = getattr(response, "usage", None)
            if usage is not None:
                if getattr(usage, "prompt_tokens", None) is not None:
                    span.set_data("ai.tokens.prompt", usage.prompt_tokens)
                if getattr(usage, "completion_tokens", None) is not None:
                    span.set_data("ai.tokens.completion", usage.completion_tokens)

            return parsed_request

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}")
            raise ValueError(f"Invalid JSON response from Groq: {e}") from e
        except Exception as e:
            logger.error(f"Error parsing request: {e}")
            raise
