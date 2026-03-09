"""
Integration tests against production services.

Run with: pytest tests/test_integration.py -v
Skip with: pytest tests/ -m "not integration"
"""

import os

import pytest
from dotenv import load_dotenv

from tests.scenarios import (
    AMPS_FOR_CHRIST_AMBIGUOUS,
    BIOSPHERE_ALBUM_FILTER,
    ECHO_BUNNYMEN_ARTIST_ONLY,
    ETERNAL_HALLUCINATION,
    FLOW_COMA_808_STATE,
    HOLLAND_1945,
    LAID_BACK_ARTIST_VS_TITLE,
    LIVING_COLOR_SPELLING,
    LUSH_TRACK_FILTER,
    MEET_ME_IN_CITY,
    MI_AMI_COMMA_FORMAT,
    PLUG_ALIAS,
    QUIXOTIC_SPECIAL_CHARS,
    SNEAKER_PIMPS_TRACK_VALIDATION,
    SOME_PHIL_COLLINS_FILLER,
    SPOONFUL_DASH_FORMAT,
    SUGAR_PLANT_FALSE_POSITIVE,
    TOY_WORD_BOUNDARY,
    YOUNG_GOV_PREFIX,
)

load_dotenv()

# Mark all tests in this file as integration tests
pytestmark = pytest.mark.integration

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

skip_if_no_groq = pytest.mark.skipif(
    not GROQ_API_KEY, reason="GROQ_API_KEY not set - skipping parser integration tests"
)


class TestParserIntegration:
    """Test the parser against the real Groq API."""

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_preserves_asterisks_in_artist_name(self):
        """Test that special characters like asterisks are preserved in artist names.

        The artist "Quix*o*tic" should NOT be normalized to "Quixotic".
        """
        from groq import Groq

        from services.parser import parse_request

        client = Groq(api_key=GROQ_API_KEY)
        s = QUIXOTIC_SPECIAL_CHARS

        result = parse_request(s.raw_message, client)

        print("\n📝 Parsed result:")
        print(f"  Artist: {result.artist}")
        print(f"  Is Request: {result.is_request}")

        assert result.is_request is True
        assert result.artist is not None

        # The key assertion: asterisks should be preserved
        assert "*" in result.artist, (
            f"Expected asterisks to be preserved in artist name, got: {result.artist}"
        )
        assert result.artist.lower().replace("*", "") == "quixotic", (
            f"Expected artist to be 'Quix*o*tic' (or similar), got: {result.artist}"
        )

        print(f"  ✅ Asterisks preserved: {result.artist}")

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_preserves_special_chars_in_various_artists(self):
        """Test preservation of special characters in well-known artist names."""
        from groq import Groq

        from services.parser import parse_request

        client = Groq(api_key=GROQ_API_KEY)

        test_cases = [
            ("play something by P!nk", "P!nk", "!"),
            ("deadmau5 please", "deadmau5", "5"),
        ]

        for message, _expected_contains, special_char in test_cases:
            result = parse_request(message, client)

            print(f"\n📝 '{message}' -> Artist: {result.artist}")

            assert result.artist is not None, f"Expected artist for '{message}'"
            # Check special char is preserved (case-insensitive check on base name)
            assert special_char in result.artist or special_char in result.artist.lower(), (
                f"Expected '{special_char}' in artist name for '{message}', got: {result.artist}"
            )

            print(f"  ✅ Special char '{special_char}' preserved")

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_parses_comma_separated_song_artist_format(self):
        """Test that 'song title, artist name' format is recognized as a request.

        Bug: The parser wasn't recognizing comma-separated format like
        "the man in your house, mi ami" as a song request.

        Expected: Should extract song and artist from comma-separated format.
        """
        from groq import Groq

        from services.parser import parse_request

        client = Groq(api_key=GROQ_API_KEY)
        s = MI_AMI_COMMA_FORMAT

        result = parse_request(s.raw_message, client)

        print("\n📝 Parsed result:")
        print(f"  Song: {result.song}")
        print(f"  Artist: {result.artist}")
        print(f"  Is Request: {result.is_request}")

        assert result.is_request is True, "Should recognize as a request"
        assert result.song is not None, "Should extract song title"
        assert result.artist is not None, "Should extract artist name"
        assert "man" in result.song.lower() and "house" in result.song.lower(), (
            f"Expected song '{s.song}', got: {result.song}"
        )
        assert "mi ami" in result.artist.lower(), (
            f"Expected artist '{s.artist}', got: {result.artist}"
        )

        print("  ✅ Correctly parsed comma-separated format!")

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_parses_song_with_common_words_in_comma_format(self):
        """Test that 'I love acid, luke vibert' is a request, not feedback.

        Bug: The parser classified this as feedback because "I love" looks like
        an emotional expression, but "I Love Acid" is a song by Luke Vibert.
        The comma-separated format should take priority.
        """
        from groq import Groq

        from services.parser import parse_request

        client = Groq(api_key=GROQ_API_KEY)

        result = parse_request("I love acid, luke vibert", client)

        print("\n📝 Parsed result:")
        print(f"  Song: {result.song}")
        print(f"  Artist: {result.artist}")
        print(f"  Is Request: {result.is_request}")

        assert result.is_request is True, (
            f"Should recognize as a request, got message_type={result.message_type}"
        )
        assert result.song is not None, "Should extract 'I Love Acid' as song title"
        assert result.artist is not None, "Should extract 'Luke Vibert' as artist"
        assert "love" in result.song.lower() and "acid" in result.song.lower(), (
            f"Expected song containing 'Love' and 'Acid', got: {result.song}"
        )
        assert "vibert" in result.artist.lower(), (
            f"Expected artist containing 'Vibert', got: {result.artist}"
        )

        print("  ✅ Correctly parsed song with common words in comma format!")

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_does_not_hallucinate_artist_names(self):
        """Test that the parser does not invent artist names not in the message.

        Bug: "mind odyssey by eternal" was parsed as artist "Eternalux" -- a name
        hallucinated by the model, not present in the original message. The album
        was also incorrectly set to "By Eternal" as a downstream consequence.

        Expected: artist should be "Eternal" (what the listener wrote), song should
        be "Mind Odyssey", album should be null.
        """
        from groq import Groq

        from services.parser import parse_request

        client = Groq(api_key=GROQ_API_KEY)
        s = ETERNAL_HALLUCINATION
        assert s.artist is not None and s.song is not None

        result = parse_request(s.raw_message, client)

        print("\n📝 Parsed result:")
        print(f"  Artist: {result.artist}")
        print(f"  Song: {result.song}")
        print(f"  Album: {result.album}")
        print(f"  Is Request: {result.is_request}")

        assert result.is_request is True, "Should recognize as a request"

        # Artist should be exactly what the listener wrote, not a hallucination
        assert result.artist is not None, "Should extract artist"
        assert result.artist.lower() == s.artist.lower(), (
            f"Expected artist '{s.artist}' (from message), got: {result.artist}"
        )
        assert "eternalux" not in (result.artist or "").lower(), (
            "Artist 'Eternalux' is hallucinated -- not in the original message"
        )

        # Song should contain "mind" and "odyssey"
        assert result.song is not None, "Should extract song title"
        assert "mind" in result.song.lower() and "odyssey" in result.song.lower(), (
            f"Expected song 'Mind Odyssey', got: {result.song}"
        )

        # Album should be null (not "By Eternal")
        assert result.album is None, f"Expected album to be null, got: {result.album}"

        print("  ✅ Parser extracted only names from the original message!")

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_dash_separated_song_artist_album_format(self):
        """Test that 'Spoonful-Cream-Wheels of Fire lp' parses correctly.

        Bug: The parser treated the entire 'Spoonful-Cream-Wheels of Fire' as the
        song title and 'lp' as the album, instead of splitting on dashes to get
        song=Spoonful, artist=Cream, album=Wheels of Fire.
        """
        from groq import Groq

        from services.parser import parse_request

        client = Groq(api_key=GROQ_API_KEY)
        s = SPOONFUL_DASH_FORMAT

        result = parse_request(s.raw_message, client)

        print("\n📝 Parsed result:")
        print(f"  Song: {result.song}")
        print(f"  Artist: {result.artist}")
        print(f"  Album: {result.album}")
        print(f"  Is Request: {result.is_request}")

        assert result.is_request is True, "Should recognize as a request"

        artist = result.artist
        assert artist is not None, "Should extract artist"
        assert artist.lower() == (s.artist or "").lower(), (
            f"Expected artist '{s.artist}', got: {artist}"
        )

        song = result.song
        assert song is not None, "Should extract song title"
        assert song.lower() == (s.song or "").lower(), f"Expected song '{s.song}', got: {song}"

        album = result.album
        assert album is not None, "Should extract album"
        assert "wheels of fire" in album.lower(), f"Expected album '{s.album}', got: {album}"

        print("  ✅ Correctly parsed dash-separated format!")

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_filler_words_not_parsed_as_song_title(self):
        """Test that filler words like 'some' are not interpreted as song titles.

        Bug: "Some phil collins please" was parsed as song="Some", artist="Phil Collins"
        when "some" is just a determiner meaning "play some Phil Collins".

        Expected: artist="Phil Collins", song=null.
        """
        from groq import Groq

        from services.parser import parse_request

        client = Groq(api_key=GROQ_API_KEY)
        s = SOME_PHIL_COLLINS_FILLER

        result = parse_request(s.raw_message, client)

        print("\n📝 Parsed result:")
        print(f"  Song: {result.song}")
        print(f"  Artist: {result.artist}")
        print(f"  Is Request: {result.is_request}")

        assert result.is_request is True, "Should recognize as a request"
        assert result.artist is not None, "Should extract artist"
        assert "collins" in result.artist.lower(), (
            f"Expected artist 'Phil Collins', got: {result.artist}"
        )
        assert result.song is None, (
            f"Expected song to be null ('some' is a filler word), got: {result.song}"
        )

        print("  ✅ Filler word 'some' correctly ignored!")


class TestFullRequestIntegration:
    """Test the full /request endpoint.

    Set TEST_ENV to control which server to test against:
        TEST_ENV=local pytest ...      # localhost:8000 (default, requires local server)
        TEST_ENV=staging pytest ...    # staging server on Railway
        TEST_ENV=production pytest ... # production server on Railway
    """

    @pytest.mark.asyncio
    async def test_artist_only_search_returns_results(self, base_url):
        """
        Test that artist-only searches (no song or album) return results.

        Bug: After refactoring to support multiple albums from Discogs, artist-only
        searches like "Can i request something from echo and the bunnymen" returned
        no results because the search condition required albums_for_search OR song.

        Expected: Should return Echo and the Bunnymen albums.
        """
        import httpx

        s = ECHO_BUNNYMEN_ARTIST_ONLY
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        # Check parsing
        parsed = data.get("parsed", {})
        assert parsed.get("artist") == s.artist, (
            f"Should parse artist as '{s.artist}', got {parsed.get('artist')}"
        )

        # Check results
        results = data.get("library_results", [])
        assert len(results) > 0, "Should find Echo and the Bunnymen albums"

        # Verify all results are by Echo and the Bunnymen
        for result in results:
            assert "echo" in result.get("artist", "").lower(), (
                f"Result should be by Echo and the Bunnymen, got {result.get('artist')}"
            )

        print(f"\n✅ Artist-only search returned {len(results)} results:")
        for r in results:
            print(f"    - {r.get('artist')} - {r.get('title')}")

    @pytest.mark.asyncio
    async def test_meet_me_in_the_city_returns_correct_album(self, base_url):
        """
        Test that 'Meet Me in the City Junior Kimbrough' returns the correct album.

        Bug: The search was returning 'Do the Rump' instead of 'Meet Me in the City'
        even though the library has an album called 'Meet Me in the City' by Junior Kimbrough.

        Expected: Should return 'Meet Me in the City' album (Blues cd KI 6/4)
        """
        import httpx

        s = MEET_ME_IN_CITY
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")
        print(f"  Album: {parsed.get('album')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')} ({r.get('call_number')})")

        # Should have results
        assert len(results) > 0, f"Should find results for {s.artist}"

        # The first result should be "Meet Me in the City", NOT "Do the Rump"
        first_result = results[0]
        assert "meet me in the city" in first_result.get("title", "").lower(), (
            f"Expected 'Meet Me in the City' album, but got '{first_result.get('title')}'. "
            f"The search returned an album that doesn't contain the requested song."
        )

        print("\n✅ Correctly returned 'Meet Me in the City' album!")

    @pytest.mark.asyncio
    async def test_thoughtforms_by_lush_excludes_albums_without_song(self, base_url):
        """
        Test that 'Thoughtforms by Lush' only returns albums that have the song.

        Bug: The search was returning 'Lovelife' which doesn't have 'Thoughtforms' on it,
        because the fallback search returned all Lush albums without filtering.

        Expected: Should return albums that actually have Thoughtforms (Scar, Mad Love, Gala)
        and NOT return Lovelife.
        """
        import httpx

        s = LUSH_TRACK_FILTER
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should have results
        assert len(results) > 0, "Should find results for Lush"

        # All results should be by Lush
        for r in results:
            assert r.get("artist") == "Lush", f"Expected Lush, got {r.get('artist')}"

        # Should NOT include Lovelife (which doesn't have Thoughtforms)
        titles = [r.get("title", "").lower() for r in results]
        assert "lovelife" not in titles, (
            "Lovelife should NOT be in results because it doesn't have Thoughtforms"
        )

        # Should include albums that actually have Thoughtforms
        # (According to Discogs: Mad Love, Scar, Gala, etc.)
        has_valid_album = any(title in ["mad love", "scar", "gala"] for title in titles)
        assert has_valid_album, (
            f"Expected at least one album that has Thoughtforms (Mad Love, Scar, or Gala), "
            f"but got: {titles}"
        )

        print("\n✅ Correctly excluded albums without the requested song!")

    @pytest.mark.asyncio
    async def test_biosphere_excludes_albums_without_track(self, base_url):
        """
        Test that 'The Things I Tell You by Biosphere' excludes albums without the track.

        Bug: Searching for a track would return albums by the artist that don't have
        the track, because the fuzzy library search matched the artist but not the album.

        Expected: Should return Substrata and Wireless (which have the track)
        and NOT return Stator (which doesn't have it).
        """
        import httpx

        s = BIOSPHERE_ALBUM_FILTER
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should have results
        assert len(results) > 0, "Should find results for Biosphere"

        # All results should be by Biosphere
        for r in results:
            assert r.get("artist") == "Biosphere", f"Expected Biosphere, got {r.get('artist')}"

        # Should NOT include Stator (which doesn't have The Things I Tell You)
        titles = [r.get("title", "").lower() for r in results]
        assert "stator" not in titles, (
            "Stator should NOT be in results because it doesn't have 'The Things I Tell You'"
        )

        # Should include albums that actually have the track
        # (According to Discogs: Substrata, Wireless)
        has_valid_album = any(title in ["substrata", "wireless"] for title in titles)
        assert has_valid_album, (
            f"Expected at least one album that has 'The Things I Tell You' "
            f"(Substrata or Wireless), but got: {titles}"
        )

        print("\n✅ Correctly excluded albums without the requested track!")

    @pytest.mark.asyncio
    async def test_young_gov_excludes_young_black_teenagers(self, base_url):
        """
        Test that searching for 'Young Gov' does not return 'Young Black Teenagers'.

        Bug: Artist prefix matching was too loose, matching 'Young Black Teenagers'
        when searching for 'Young Gov' because both start with 'Young'.

        Expected: Should return only results where artist starts with 'Young Gov',
        or no results if no exact match exists.
        """
        import httpx

        s = YOUNG_GOV_PREFIX
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("library_results", [])

        print(f"\n📚 Library Results for '{s.artist}':")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should NOT include Young Black Teenagers
        for r in results:
            artist = r.get("artist", "").lower()
            assert "young black teenagers" not in artist, (
                "'Young Black Teenagers' should not match 'Young Gov' search"
            )

        print("\n✅ Correctly excluded 'Young Black Teenagers' from 'Young Gov' search!")

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason=LAID_BACK_ARTIST_VS_TITLE.xfail_reason or "")
    async def test_laid_back_matches_band_not_album_titles(self, base_url):
        """
        Test that searching for 'Laid Back' doesn't return false positive title matches.

        Bug: Search returns albums like "Night Shift - Laid Back Trip Hop" (Various Artists)
        because the title contains "Laid Back", even though the artist is different.

        Expected: Should prefer albums by artists with "Laid Back" in their name
        (e.g., Gregg Allman - "Laid Back", Beatnik Filmstars - "Laid Back and English")
        over Various Artists compilations with "laid back" only in title.
        """
        import httpx

        s = LAID_BACK_ARTIST_VS_TITLE
        assert s.artist is not None
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("library_results", [])

        print(f"\n📚 Library Results for '{s.artist}':")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Check that we don't return Various Artists compilations (title-only matches)
        various_artists_results = [
            r for r in results if "various artists" in r.get("artist", "").lower()
        ]

        assert len(various_artists_results) == 0, (
            f"Should not return Various Artists compilations: {various_artists_results}"
        )

        # Check that results contain "laid back" in either artist or title
        for r in results:
            artist = r.get("artist", "").lower()
            title = r.get("title", "").lower()

            assert s.artist.lower() in artist or s.artist.lower() in title, (
                f"Unrelated result: '{r.get('artist')}' - '{r.get('title')}'"
            )

        print("\n✅ No Various Artists false positives!")

    @pytest.mark.asyncio
    async def test_toy_excludes_chew_toy(self, base_url):
        """
        Test that searching for 'Toy' does not return 'Chew Toy'.

        Bug: Artist filtering was matching partial words, so 'Toy' matched 'Chew Toy'.

        Expected: Should only return albums by artist 'Toy', not 'Chew Toy'.
        """
        import httpx

        s = TOY_WORD_BOUNDARY
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("library_results", [])

        print(f"\n📚 Library Results for '{s.artist}':")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should NOT include "Chew Toy"
        for r in results:
            artist = r.get("artist", "").lower()
            assert "chew toy" not in artist, (
                "'Chew Toy' should not match 'Toy' search - "
                "artist filtering should use word boundaries"
            )

        print("\n✅ Correctly excluded 'Chew Toy' from 'Toy' search!")

    @pytest.mark.asyncio
    async def test_amps_for_christ_excludes_edward_bear(self, base_url):
        """
        Test that searching for 'Amps for Christ' does not return 'Edward Bear'.

        Bug: Ambiguous format detection ("Amps for Christ - Edward") was incorrectly
        matching "Edward Bear" albums.

        Expected: Should return 'Amps for Christ' albums, not 'Edward Bear'.
        """
        import httpx

        s = AMPS_FOR_CHRIST_AMBIGUOUS
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.artist, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("library_results", [])

        print(f"\n📚 Library Results for '{s.artist}':")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should NOT include Edward Bear
        for r in results:
            artist = r.get("artist", "").lower()
            assert "edward bear" not in artist, (
                "'Edward Bear' should not match 'Amps for Christ' search"
            )

        # If we have results, they should be by Amps for Christ
        if results:
            has_amps = any(
                r.get("artist", "").lower().startswith("amps for christ") for r in results
            )
            assert has_amps, (
                f"Expected 'Amps for Christ' albums, got: {[r.get('artist') for r in results]}"
            )

        print("\n✅ Correctly excluded 'Edward Bear' from 'Amps for Christ' search!")

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason=HOLLAND_1945.xfail_reason or "")
    async def test_holland_1945_returns_aeroplane(self, base_url):
        """
        Test that 'Holland, 1945 Neutral Milk Hotel' returns the correct album.

        Bug: Search was returning 'On Avery Island' instead of 'In the Aeroplane Over the Sea'
        because the keyword search wasn't including song title words.

        Expected: Should return 'In the Aeroplane Over the Sea' (which has Holland, 1945).
        """
        import httpx

        s = HOLLAND_1945
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should have results
        assert len(results) > 0, "Should find results for Neutral Milk Hotel"

        # First result should be "In the Aeroplane Over the Sea"
        first_result = results[0]
        assert "aeroplane" in first_result.get("title", "").lower(), (
            f"Expected 'In the Aeroplane Over the Sea', got '{first_result.get('title')}'. "
            f"The search returned an album that doesn't have 'Holland, 1945'."
        )

        print("\n✅ Correctly returned 'In the Aeroplane Over the Sea'!")

    @pytest.mark.asyncio
    async def test_mi_ami_comma_format_returns_watersports(self, base_url):
        """
        Test that 'the man in your house, mi ami' returns the album Watersports.

        Bug: The parser wasn't recognizing "song, artist" (comma-separated) format,
        classifying it as "other" instead of a request.

        Expected: Should parse as request and return 'Watersports' by Mi Ami.
        """
        import httpx

        s = MI_AMI_COMMA_FORMAT
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Is Request: {parsed.get('is_request')}")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should be recognized as a request
        assert parsed.get("is_request") is True, (
            "Should recognize 'song, artist' format as a request"
        )

        # Should have results
        assert len(results) > 0, "Should find results for Mi Ami"

        # Should return Watersports
        first_result = results[0]
        assert "watersports" in first_result.get("title", "").lower(), (
            f"Expected 'Watersports' album, got '{first_result.get('title')}'"
        )
        assert "mi ami" in first_result.get("artist", "").lower(), (
            f"Expected artist 'Mi Ami', got '{first_result.get('artist')}'"
        )

        print("\n✅ Correctly returned 'Watersports' by Mi Ami!")

    @pytest.mark.asyncio
    async def test_living_color_corrects_to_living_colour(self, base_url):
        """
        Test that 'Cult of Personality by Living Color' finds Living Colour.

        Bug: American spelling "Living Color" wasn't matching British spelling
        "Living Colour" in the library, returning no results.

        Expected: Should correct spelling and return Living Colour albums.
        """
        import httpx

        s = LIVING_COLOR_SPELLING
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should have results despite spelling difference
        assert len(results) > 0, "Should find Living Colour albums even when spelled 'Living Color'"

        # All results should be by Living Colour
        for r in results:
            assert "living colour" in r.get("artist", "").lower(), (
                f"Expected 'Living Colour', got '{r.get('artist')}'"
            )

        print("\n✅ Correctly corrected 'Living Color' to 'Living Colour'!")

    @pytest.mark.asyncio
    async def test_sugar_plant_excludes_unrelated_compilations(self, base_url):
        """
        Test that 'Simple by Sugar Plant' does not return unrelated compilations.

        Bug: Discogs search for "Simple" was returning "22 Explosive Hits, Vol 2"
        because it contains "A Simple Man" by "Sugar Bears (2)" - partial matches
        on both track and artist. Then fuzzy album matching was incorrectly matching
        this to "K-Tel: 22 Explosive Hits!" in the library.

        Expected: Should either return actual Sugar Plant albums, or no results
        if Sugar Plant isn't in the library. Should NOT return K-Tel compilations.
        """
        import httpx

        s = SUGAR_PLANT_FALSE_POSITIVE
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should NOT include unrelated K-Tel or "Explosive Hits" compilations
        for r in results:
            title = r.get("title", "").lower()
            assert "k-tel" not in title and "explosive" not in title, (
                f"Should not return unrelated compilation '{r.get('title')}' "
                f"for 'Simple by Sugar Plant' request"
            )

        # If we have results, they should be by Sugar Plant or Various Artists
        # compilations that actually contain "Simple" by Sugar Plant
        for r in results:
            artist = r.get("artist", "").lower()
            # Either by Sugar Plant directly, or a verified compilation
            is_sugar_plant = "sugar plant" in artist
            is_valid_compilation = "various" in artist
            assert is_sugar_plant or is_valid_compilation, (
                f"Expected Sugar Plant or verified compilation, got '{r.get('artist')}'"
            )

        print("\n✅ Correctly excluded unrelated compilations!")

    @pytest.mark.asyncio
    async def test_results_have_unique_library_urls(self, base_url):
        """
        Test that all results have unique library URLs.

        Each library result should point to a different library record.
        No duplicates should be returned.
        """
        import httpx

        # Use queries known to return multiple results from other tests
        test_queries = [
            "Junior Kimbrough",  # Known from test_meet_me_in_the_city_returns_correct_album
            "Various Artists",
            "Laid Back",  # Known from test_laid_back_matches_band_not_album_titles
        ]

        found_multi_result = False
        for query in test_queries:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{base_url}/request",
                    json={"message": query, "skip_slack": True},
                )
                response.raise_for_status()
                data = response.json()

            results = data.get("library_results", [])

            # Check all IDs are unique
            ids = [r.get("id") for r in results]
            assert len(ids) == len(set(ids)), f"Duplicate IDs found in results for '{query}': {ids}"

            # Check all library URLs are unique
            library_urls = [r.get("library_url") for r in results]
            assert len(library_urls) == len(set(library_urls)), (
                f"Duplicate library URLs found in results for '{query}': {library_urls}"
            )

            if len(results) > 1:
                found_multi_result = True

            print(f"\n✅ '{query}': {len(results)} results, all unique library URLs")

        # At least one query should return multiple results
        assert found_multi_result, "Expected at least one query to return multiple results"

    @pytest.mark.asyncio
    async def test_results_have_no_duplicate_albums(self, base_url):
        """
        Test that search results don't return the same album twice.

        When multiple results are returned, each should be a different album
        (no duplicate titles from the same artist).
        """
        import httpx

        # Use queries known to work from other tests
        test_cases = [
            ("Cult of Personality by Living Color", "spelling-corrected artist"),
            ("thoughtforms by lush", "song search"),
            ("Meet Me in the City Junior Kimbrough", "song/album search"),
        ]

        found_multi_result = False
        for query, description in test_cases:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{base_url}/request",
                    json={"message": query, "skip_slack": True},
                )
                response.raise_for_status()
                data = response.json()

            results = data.get("library_results", [])

            # Check all IDs are unique
            ids = [r.get("id") for r in results]
            assert len(ids) == len(set(ids)), (
                f"Duplicate IDs found for '{query}' ({description}): {ids}"
            )

            # Check no duplicate (artist, title) pairs
            artist_title_pairs = [(r.get("artist"), r.get("title")) for r in results]
            assert len(artist_title_pairs) == len(set(artist_title_pairs)), (
                f"Duplicate artist/title pairs for '{query}' ({description}): {artist_title_pairs}"
            )

            if len(results) > 1:
                found_multi_result = True

            print(f"\n✅ '{query}' ({description}): {len(results)} unique results")
            for r in results:
                print(f"    - {r.get('artist')} - {r.get('title')}")

        # At least one query should return multiple results
        assert found_multi_result, "Expected at least one query to return multiple results"

    @pytest.mark.asyncio
    async def test_6_underground_sneaker_pimps_returns_only_becoming_x(self, base_url):
        """
        Test that '6 underground - sneaker pimps' only returns albums with the track.

        Bug: The search was returning both 'Becoming X' and 'Kiss & Swallow' with
        the message "6 Underground is not on any album in the library", even though
        6 Underground IS on Becoming X.

        Expected: Should return only 'Becoming X' (which has 6 Underground)
        and NOT return 'Kiss & Swallow' (which doesn't have it).
        """
        import httpx

        s = SNEAKER_PIMPS_TRACK_VALIDATION
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])
        song_not_found = data.get("song_not_found", True)
        context_message = data.get("context_message", "")

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        print(f"\n📋 Context: {context_message}")
        print(f"  song_not_found: {song_not_found}")

        # Should have results
        assert len(results) > 0, "Should find results for Sneaker Pimps"

        # All results should be by Sneaker Pimps
        for r in results:
            assert "sneaker pimps" in r.get("artist", "").lower(), (
                f"Expected Sneaker Pimps, got {r.get('artist')}"
            )

        # Should NOT include Kiss & Swallow (which doesn't have 6 Underground)
        titles = [r.get("title", "").lower() for r in results]
        assert "kiss & swallow" not in titles, (
            "Kiss & Swallow should NOT be in results because it doesn't have '6 Underground'"
        )

        # Should include Becoming X (which has 6 Underground)
        has_becoming_x = any("becoming x" in title for title in titles)
        assert has_becoming_x, f"Expected 'Becoming X' (which has 6 Underground), but got: {titles}"

        # Should NOT say "not on any album" since it IS on Becoming X
        assert song_not_found is False, (
            "song_not_found should be False since 6 Underground is on Becoming X"
        )

        print("\n✅ Correctly returned only albums with the requested track!")

    @pytest.mark.asyncio
    async def test_flow_coma_808_state_excludes_unrelated_album(self, base_url):
        """
        Test that 'flow coma by 808 state' does not return unrelated albums.

        Bug: Discogs finds "Flow Coma" on "The Best Of 808 State: Blueprint",
        then search_album_fuzzy matched library album "808 State" (a different
        album: "Four States Of 808state") via token_set_ratio subset bias.

        Expected: Should not return "808 State" / "Four States Of 808state"
        as a false positive match.
        """
        import httpx

        s = FLOW_COMA_808_STATE
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should NOT return "808 State" album (which is actually "Four States Of 808state"
        # on Discogs release 13488484 and does NOT contain "Flow Coma")
        for r in results:
            title = r.get("title", "").lower()
            assert title != "808 state", (
                "Should not return '808 State' album — it does not contain 'Flow Coma'. "
                "This is a false positive from token_set_ratio subset bias."
            )

        print("\n✅ Correctly excluded unrelated '808 State' album!")

    @pytest.mark.asyncio
    async def test_skip_cache_bypasses_all_caches(self, base_url):
        """
        Test that skip_cache=True bypasses both in-memory and PG caches.

        Sends the same query twice: once normally (which populates caches),
        then with skip_cache=True. The second request should show 0 memory
        hits and 0 PG hits.
        """
        import httpx

        query = "Autechre Confield"

        async with httpx.AsyncClient(timeout=60.0) as client:
            # First request: populates caches
            response1 = await client.post(
                f"{base_url}/request",
                json={"message": query, "skip_slack": True},
            )
            response1.raise_for_status()

            # Second request: skip_cache should bypass all caches
            response2 = await client.post(
                f"{base_url}/request",
                json={"message": query, "skip_slack": True, "skip_cache": True},
            )
            response2.raise_for_status()
            data = response2.json()

        cache_stats = data.get("cache_stats", {})
        memory_hits = cache_stats.get("memory_hits", 0)
        pg_hits = cache_stats.get("pg_hits", 0)

        print(f"\n📊 Cache stats with skip_cache=True: {cache_stats}")

        assert memory_hits == 0, (
            f"Expected 0 memory cache hits with skip_cache=True, got {memory_hits}"
        )
        assert pg_hits == 0, f"Expected 0 PG cache hits with skip_cache=True, got {pg_hits}"

        print("✅ skip_cache=True correctly bypassed all caches")

    @pytest.mark.asyncio
    async def test_plug_not_corrected_to_plugz(self, base_url):
        """
        Test that 'me and mr. jones by plug from drum n bass for papa' does NOT
        falsely correct artist "Plug" to "Plugz".

        Bug: find_similar_artist("Plug") fuzzy-matched to "Plugz" at 89% similarity,
        exceeding the flat 85% threshold. This overwrote the parsed artist before
        library search, so FTS5 never saw "Plug" and missed Luke Vibert's album.

        Note: The library catalogs Plug's albums under "Luke Vibert", so we don't
        expect library results here -- just that the artist isn't falsely corrected.
        """
        import httpx

        s = PLUG_ALIAS
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")
        print(f"  Album: {parsed.get('album')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Artist should NOT be corrected to Plugz
        for r in results:
            assert "plugz" not in r.get("artist", "").lower(), (
                f"Should not return Plugz albums, got '{r.get('artist')}'"
            )

        print("\n✅ Correctly avoided false correction of 'Plug' to 'Plugz'!")

    @pytest.mark.asyncio
    async def test_plug_me_and_mr_jones_finds_album(self, base_url):
        """
        Test that 'me and mr jones by plug' returns results including the correct album.

        Bug: "Plug" is an alias for "Luke Vibert" on Discogs. The system failed at three
        levels: Discogs track validation, album resolution filtering, and library artist
        filtering all rejected the alias mismatch.

        Expected: Should return "Drum 'n' Bass for Papa" (or similar Plug/Luke Vibert album).
        """
        import httpx

        s = PLUG_ALIAS
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": s.raw_message, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")
        print(f"  Album: {parsed.get('album')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        assert len(results) > 0, (
            "Should find results for 'me and mr jones by plug'. "
            "Plug is an alias for Luke Vibert on Discogs."
        )
