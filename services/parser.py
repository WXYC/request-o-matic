import json
import logging
import re
from enum import StrEnum
from typing import Literal, get_args

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
# album is returned. A single politeness clause is sufficient -- we don't see
# chained "please thanks" tails in practice.
_TRAILING_POLITENESS_RE = re.compile(
    r"[\s,.!?]+(?:please|thanks|thank\ you|thx)\s*[.!?]*\s*$",
    re.IGNORECASE | re.VERBOSE,
)

# A whole feedback/greeting clause appended after the request -- e.g. the
# listener writes "<song> by <artist> from <album>....  thanks for your set!".
# The trailing-politeness trim above only removes a single trailing token; this
# removes the politeness lead-in word AND everything after it. It fires ONLY when
# a sentence boundary -- terminal punctuation or a comma -- precedes the
# politeness word. That boundary requirement is what distinguishes an appended
# clause from a title that merely *contains* a politeness word ("Pretty Please
# Goodbye"). A run of spaces is deliberately NOT a boundary: a double-space typo
# inside a title ("pretty  please goodbye") would otherwise truncate it, and a
# genuine appended clause is reliably introduced by punctuation or a comma
# anyway. The lead-in set mirrors _TRAILING_POLITENESS_RE so the two stay
# consistent.
_TRAILING_FEEDBACK_RE = re.compile(
    r"""
    (?: [.!?…]+ | ,+ )                   # a clause boundary (punctuation or comma)
    \s*
    (?: please | thanks | thank\ you | thx )   # feedback lead-in
    \b
    [\s\S]*                              # ... and the rest of the appended clause
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A descriptor noun introducing the album title -- "from album <title>", "off
# the record <title>". The noun is a lead-in, not part of the title, so strip it.
# The tricky part is that "album"/"record"/"lp"/"ep" are also legitimate *title*
# words ("Album of the Year", "Record Collection", "EP 3", "LP 2"), so the strip
# is deliberately narrow -- it fires in only two unambiguous shapes:
#
#   1. determiner-led -- "the album <title>" / "the record <title>". A real title
#      virtually never begins "the album ..."/"the record ...", so "the" is a
#      confident descriptor signal.
#   2. bare "album <title>" -- the attested telegraphic form listeners actually
#      type ("from album my love is"). Guarded by a negative lookahead so the
#      "Album of ..." title pattern ("Album of the Year") keeps its first word.
#
# Bare "record"/"lp"/"ep" are intentionally NOT stripped: their bare descriptor
# use is unattested and they collide with real titles ("Record Collection",
# "EP 3", "LP 2"). The trailing `\s+` requires title text to follow, so a bare
# album titled "Album" (PiL) survives "off album".
_LEADING_ALBUM_DESCRIPTOR_RE = re.compile(
    r"""
    ^
    (?:
        the \s+ (?: album | record )   # 1. determiner-led descriptor
      |
        album (?! \s+ of \b )          # 2. bare "album", but not "Album of ..."
    )
    \s+
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Single source of truth for the album prepositions the pre-pass recognises.
# Order matters: multi-word forms must precede their own prefixes ("off of"
# before "off") so the regex alternation is greedy-correct.
#
# Declared as a Literal so the membership half of test parity is enforced at
# type-check time: tests/scenarios.py types AlbumPrepassCase.preposition as
# `Preposition`, so a corpus case carrying a preposition the parser does not
# support (a typo, or a string for a branch that was never added) is a mypy
# error in the "Type Check" CI job -- it can no longer slip past as a runtime
# guard-test failure. The coverage half (every supported preposition has a
# positive corpus case) remains a runtime guard in
# tests/unit/test_album_extraction.py, because asserting a regex fires on a
# given string is irreducibly runtime. See WXYC/request-o-matic#140.
Preposition = Literal["off of", "off", "from", "on"]

# Runtime tuple derived from the Literal so the two cannot drift. get_args
# preserves declaration order, so the greedy-correct ordering above is kept.
# Both regexes below build from this tuple, and so does the shared test corpus
# (tests/scenarios.py::ALBUM_PREPASS_CASES).
SUPPORTED_ALBUM_PREPOSITIONS: tuple[Preposition, ...] = get_args(Preposition)

# Cheap literal pre-screen to bound worst-case backtracking on long inputs
# without a preposition. The verbose `_ALBUM_PREPOSITION_RE` has two
# non-greedy `.` runs; on a long DJ blurb that lacks any of these tokens it
# can backtrack through every prefix split. This regex is linear and lets
# us bail before the heavyweight pattern runs.
_PREP_TOKEN_PRESCREEN_RE = re.compile(
    r"\s(" + "|".join(SUPPORTED_ALBUM_PREPOSITIONS) + r")\s", re.IGNORECASE
)

# Signal-words in the prefix that mark a message as request-shaped. Used to
# gate the `from` preposition, where the false-positive surface ("hello from
# <place>", "greetings from <city>") cannot be covered by a finite first-token
# denylist on the album side. `on`/`off`/`off of` do not need this gate -- the
# prepositions themselves carry enough signal.
_REQUEST_SIGNAL_RE = re.compile(r"\bby\b|,", re.IGNORECASE)

# A standalone "by" authorship token. In the album text it marks a trailing
# "by <artist>" clause (the reverse-order shape below); it is not matched inside
# words ("baby", "goodbye", "rugby") thanks to the word boundaries.
_ARTIST_BY_TOKEN_RE = re.compile(r"\bby\b", re.IGNORECASE)


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
# The preposition alternation is built from `SUPPORTED_ALBUM_PREPOSITIONS` so the
# two stay in lockstep; internal spaces are escaped (`off\ of`) because VERBOSE
# mode ignores unescaped whitespace.
_PREP_ALTERNATION = "|".join(p.replace(" ", r"\ ") for p in SUPPORTED_ALBUM_PREPOSITIONS)
_ALBUM_PREPOSITION_RE = re.compile(
    rf"""
    ^
    (?P<prefix>.+?)              # song (+ optional "by <artist>") -- non-empty, non-greedy
    \s+
    (?P<prep>{_PREP_ALTERNATION})   # preposition (order matters: "off of" before "off")
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

    stripped = raw_message.strip()

    # Cheap pre-screen: bail before the verbose pattern when no preposition
    # token is present. Bounds worst-case backtracking on long messages.
    if _PREP_TOKEN_PRESCREEN_RE.search(stripped) is None:
        return None

    match = _ALBUM_PREPOSITION_RE.match(stripped)
    if match is None:
        return None

    album_raw = match.group("album").strip()
    # Trim a trailing feedback clause ("...doga.  thanks for the set!") first,
    # then any bare trailing politeness token ("...doga please"). The feedback
    # trim requires a sentence boundary before the politeness word, so a title
    # that merely contains one ("Pretty Please Goodbye") is left intact.
    album_raw = _TRAILING_FEEDBACK_RE.sub("", album_raw)
    album_raw = _TRAILING_POLITENESS_RE.sub("", album_raw).strip()
    if not album_raw:
        return None

    # Drop a leading album-descriptor noun ("from album <title>" -> "<title>").
    # The noun introduces the title rather than being part of it. The regex is
    # anchored so it only fires when title text follows (see its definition).
    album_raw = _LEADING_ALBUM_DESCRIPTOR_RE.sub("", album_raw).strip()
    if not album_raw:
        return None

    # Reject the bare "of" tail: the regex eagerly parses "moon pix off of"
    # as preposition="off", album="of" -- there's no real album text there.
    prep = match.group("prep").lower()
    if prep == "off" and album_raw.lower() == "of":
        return None

    # Idiom denylist: check the first significant token of the album text.
    # If it's an idiom-head we decline to fire and leave it to Groq.
    first_token = re.split(r"\s+", album_raw, maxsplit=1)[0].lower().rstrip(",.!?")
    if first_token in _ALBUM_IDIOM_HEADS:
        return None

    prefix = match.group("prefix").strip()

    # Reverse-order shape "<song> {prep} <album> by <artist>": the album group is
    # anchored to end-of-string, so a trailing "by <artist>" authorship clause is
    # swallowed into the album ("neptune by prince jammy") and Groq -- handed only
    # the "<song>" prefix -- cannot recover the artist. An album title can itself
    # contain "by" ("Death By Chocolate", "One By One"), so we cannot safely split
    # the clause off here; decline instead and defer the whole message to Groq's
    # world knowledge. Gate on the prefix NOT already naming an artist (no "by"/
    # comma before the preposition): when it has -- the pre-pass's core case, e.g.
    # "moon pix by cat power off death by chocolate" -- the album's "by" is title
    # text, so keep firing. WXYC/request-o-matic#193 (sibling of #188).
    if _ARTIST_BY_TOKEN_RE.search(album_raw) and _REQUEST_SIGNAL_RE.search(prefix) is None:
        return None

    # `from` has a different false-positive surface than `on`/`off`/`off of`:
    # the album side after `from` is typically a proper noun (boston, new york),
    # so a finite first-token denylist can't gate it. Instead, require the
    # prefix to look request-shaped (contains "by" or a comma).
    if prep == "from" and _REQUEST_SIGNAL_RE.search(prefix) is None:
        return None

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
- Terse messages like "song title. artist name.", "song - artist", "song title, artist name", or "song by artist" should extract both song and artist. The word "by" in "X by Y" is a preposition indicating authorship -- Y is the artist, not an album. This applies even when the song title contains common words like "love", "hate", "like", etc., or when the song title is itself a short common word that resembles a temporal adverb (e.g., "today"): in the "<song>, <artist>" comma shape (and the dash and "by" shapes) the left side is the song, not a preamble to drop. For example, "I love acid, luke vibert" is a request for the song "I Love Acid" by Luke Vibert, not feedback; likewise "Today, Jefferson Airplane" is a request for the song "Today" by Jefferson Airplane (song="Today", artist="Jefferson Airplane"), not a temporal aside.
- Dashes (with or without spaces) are delimiters separating song, artist, and album. "Song-Artist-Album" or "Song - Artist - Album" should be split into three fields. For example, "Spoonful-Cream-Wheels of Fire lp" means song="Spoonful", artist="Cream", album="Wheels of Fire".
- Words like "lp", "cd", "vinyl", "7\"", "12\"", "45" at the end of a message are physical format descriptors, not album names. Ignore them. For example, in "Spoonful-Cream-Wheels of Fire lp", "lp" is a format descriptor and "Wheels of Fire" is the album.
- Words like "some", "something", "anything", "any", "a little", "a bit of", "more" before "by"/"from" + artist name are filler/determiners meaning "play [some quantity of] artist", NOT song titles. For example, "Some phil collins please" means play some Phil Collins -- "some" is not a song title. "Something by Helden" means play anything by Helden -- "something" is not a song title. Set song to null in these cases.
- The mirror of that rule applies to the artist slot: when the listener names a specific song or album but leaves the performer **unspecified** -- describing it generically rather than naming a real act -- set artist to null and keep the song/album. Phrases like "any Hindustani classical musician", "some jazz trio", "any artist", "any band", "whoever", "anybody", or "someone" in the artist position are a request for that title by an unspecified performer, not a literal artist name. For example, "Raga Bharavi by any Hindustani classical musician" means song="Raga Bharavi", artist=null (play that raga by any performer) -- do NOT capture "any Hindustani classical musician" as the artist, and do NOT invent a specific artist to fill the slot. This lets the search run song-only across all artists instead of failing on a nonexistent literal artist.
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
            span.set_data("ai.output.album_prepass_fired", pre_pass_album is not None)
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
