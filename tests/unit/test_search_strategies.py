"""Tests for core.search module - strategy pattern implementation."""
import pytest

from core.search import (
    SearchState,
    SearchStrategyType,
    build_strategies,
    execute_search_pipeline,
    get_search_type_from_state,
    has_artist_or_album_or_song,
    no_results_and_ambiguous_format,
    no_results_and_song_but_no_artist,
    song_not_found_with_artist_and_song,
)
from library.models import LibraryItem
from services.parser import MessageType, ParsedRequest


@pytest.fixture
def sample_library_item():
    """Create a sample LibraryItem for testing."""
    return LibraryItem(
        id=1,
        title="Abbey Road",
        artist="The Beatles",
        call_letters="AB",
        artist_call_number=1,
        release_call_number=2,
    )


@pytest.fixture
def empty_state():
    """Create an empty SearchState."""
    return SearchState()


@pytest.fixture
def state_with_results(sample_library_item):
    """Create a SearchState with results."""
    return SearchState(results=[sample_library_item])


class TestSearchState:
    """Test SearchState dataclass."""

    def test_default_values(self):
        state = SearchState()
        assert state.results == []
        assert state.song_not_found is False
        assert state.found_on_compilation is False
        assert state.strategies_tried == []
        assert state.discogs_titles == {}
        assert state.albums_for_search == []

    def test_with_values(self):
        state = SearchState(
            song_not_found=True,
            albums_for_search=["Test Album"],
        )
        assert state.song_not_found is True
        assert state.albums_for_search == ["Test Album"]


class TestConditionFunctions:
    """Test strategy condition functions."""

    def test_has_artist_or_album_or_song_with_artist_and_album(self, empty_state):
        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
            artist="The Beatles",
        )
        empty_state.albums_for_search = ["Abbey Road"]
        assert has_artist_or_album_or_song(parsed, empty_state, "test") is True

    def test_has_artist_or_album_or_song_with_artist_and_song(self, empty_state):
        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
            artist="The Beatles",
            song="Come Together",
        )
        assert has_artist_or_album_or_song(parsed, empty_state, "test") is True

    def test_has_artist_or_album_or_song_with_artist_only(self, empty_state):
        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
            artist="The Beatles",
        )
        # Artist only - should still trigger search (artist-only fallback)
        assert has_artist_or_album_or_song(parsed, empty_state, "test") is True

    def test_has_artist_or_album_or_song_no_artist_but_has_song(self, empty_state):
        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
            song="Come Together",
        )
        empty_state.albums_for_search = ["Abbey Road"]
        # Has song and albums, should trigger search
        assert has_artist_or_album_or_song(parsed, empty_state, "test") is True

    def test_has_artist_or_album_or_song_nothing(self, empty_state):
        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        # No artist, no song, no albums - should NOT trigger search
        assert has_artist_or_album_or_song(parsed, empty_state, "test") is False

    def test_no_results_and_ambiguous_format_with_dash(self, empty_state):
        parsed = ParsedRequest(
            raw_message="Artist - Song",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        assert no_results_and_ambiguous_format(parsed, empty_state, "Artist - Song") is True

    def test_no_results_and_ambiguous_format_with_results(self, state_with_results):
        parsed = ParsedRequest(
            raw_message="Artist - Song",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        assert no_results_and_ambiguous_format(parsed, state_with_results, "Artist - Song") is False

    def test_no_results_and_ambiguous_format_no_dash(self, empty_state):
        parsed = ParsedRequest(
            raw_message="Play song by artist",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        assert no_results_and_ambiguous_format(parsed, empty_state, "Play song by artist") is False

    def test_song_not_found_with_artist_and_song_true(self, empty_state):
        empty_state.song_not_found = True
        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
            artist="The Beatles",
            song="Come Together",
        )
        assert song_not_found_with_artist_and_song(parsed, empty_state, "test") is True

    def test_song_not_found_with_artist_and_song_false_when_found(self, empty_state):
        empty_state.song_not_found = False
        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
            artist="The Beatles",
            song="Come Together",
        )
        assert song_not_found_with_artist_and_song(parsed, empty_state, "test") is False

    def test_song_not_found_with_artist_and_song_false_no_artist(self, empty_state):
        empty_state.song_not_found = True
        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
            song="Come Together",
        )
        assert song_not_found_with_artist_and_song(parsed, empty_state, "test") is False

    def test_no_results_and_song_but_no_artist_true(self, empty_state):
        parsed = ParsedRequest(
            raw_message="Laid Back",
            is_request=True,
            message_type=MessageType.REQUEST,
            song="Laid Back",
        )
        assert no_results_and_song_but_no_artist(parsed, empty_state, "Laid Back") is True

    def test_no_results_and_song_but_no_artist_false_has_results(self, state_with_results):
        parsed = ParsedRequest(
            raw_message="Laid Back",
            is_request=True,
            message_type=MessageType.REQUEST,
            song="Laid Back",
        )
        assert no_results_and_song_but_no_artist(parsed, state_with_results, "Laid Back") is False

    def test_no_results_and_song_but_no_artist_false_has_artist(self, empty_state):
        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
            artist="The Beatles",
            song="Come Together",
        )
        assert no_results_and_song_but_no_artist(parsed, empty_state, "test") is False

    def test_no_results_and_song_but_no_artist_false_no_song(self, empty_state):
        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        assert no_results_and_song_but_no_artist(parsed, empty_state, "test") is False


class TestBuildStrategies:
    """Test build_strategies function."""

    def test_builds_three_strategies_without_song_as_artist(self):
        async def mock_search(*args, **kwargs):
            return [], False

        strategies = build_strategies(
            search_library_func=mock_search,
            search_alternative_func=mock_search,
            search_compilations_func=mock_search,
        )
        assert len(strategies) == 3

    def test_builds_four_strategies_with_song_as_artist(self):
        async def mock_search(*args, **kwargs):
            return [], False

        strategies = build_strategies(
            search_library_func=mock_search,
            search_alternative_func=mock_search,
            search_compilations_func=mock_search,
            search_song_as_artist_func=mock_search,
        )
        assert len(strategies) == 4

    def test_strategies_in_correct_order(self):
        async def mock_search(*args, **kwargs):
            return [], False

        strategies = build_strategies(
            search_library_func=mock_search,
            search_alternative_func=mock_search,
            search_compilations_func=mock_search,
            search_song_as_artist_func=mock_search,
        )
        assert strategies[0].name == SearchStrategyType.ARTIST_PLUS_ALBUM
        assert strategies[1].name == SearchStrategyType.SWAPPED_INTERPRETATION
        assert strategies[2].name == SearchStrategyType.TRACK_ON_COMPILATION
        assert strategies[3].name == SearchStrategyType.SONG_AS_ARTIST


class TestGetSearchTypeFromState:
    """Test get_search_type_from_state function."""

    def test_returns_compilation_when_found_on_compilation(self):
        state = SearchState(found_on_compilation=True)
        assert get_search_type_from_state(state) == "compilation"

    def test_returns_none_when_no_strategies_tried(self):
        state = SearchState()
        assert get_search_type_from_state(state) == "none"

    def test_returns_direct_for_artist_plus_album_without_fallback(self):
        state = SearchState(
            strategies_tried=[SearchStrategyType.ARTIST_PLUS_ALBUM],
            song_not_found=False,
        )
        assert get_search_type_from_state(state) == "direct"

    def test_returns_fallback_for_artist_plus_album_with_fallback(self):
        state = SearchState(
            strategies_tried=[SearchStrategyType.ARTIST_PLUS_ALBUM],
            song_not_found=True,
        )
        assert get_search_type_from_state(state) == "fallback"

    def test_returns_alternative_for_swapped_interpretation(self):
        state = SearchState(
            strategies_tried=[
                SearchStrategyType.ARTIST_PLUS_ALBUM,
                SearchStrategyType.SWAPPED_INTERPRETATION,
            ],
        )
        assert get_search_type_from_state(state) == "alternative"

    def test_returns_compilation_for_track_on_compilation(self):
        state = SearchState(
            strategies_tried=[
                SearchStrategyType.ARTIST_PLUS_ALBUM,
                SearchStrategyType.TRACK_ON_COMPILATION,
            ],
        )
        assert get_search_type_from_state(state) == "compilation"

    def test_returns_song_as_artist_for_song_as_artist(self):
        state = SearchState(
            strategies_tried=[
                SearchStrategyType.ARTIST_PLUS_ALBUM,
                SearchStrategyType.SONG_AS_ARTIST,
            ],
        )
        assert get_search_type_from_state(state) == "song_as_artist"


class TestSearchStrategyType:
    """Test SearchStrategyType enum."""

    def test_artist_plus_album_value(self):
        assert SearchStrategyType.ARTIST_PLUS_ALBUM.value == "artist_plus_album"

    def test_swapped_interpretation_value(self):
        assert SearchStrategyType.SWAPPED_INTERPRETATION.value == "swapped_interpretation"

    def test_track_on_compilation_value(self):
        assert SearchStrategyType.TRACK_ON_COMPILATION.value == "track_on_compilation"

    def test_song_as_artist_value(self):
        assert SearchStrategyType.SONG_AS_ARTIST.value == "song_as_artist"

    def test_is_string_enum(self):
        # Value can be accessed as string
        assert SearchStrategyType.ARTIST_PLUS_ALBUM.value == "artist_plus_album"


class TestExecuteSearchPipeline:
    """Test execute_search_pipeline function."""

    @pytest.mark.asyncio
    async def test_stops_after_finding_results(self, sample_library_item):
        """Test that pipeline stops after first strategy finds results."""
        from unittest.mock import AsyncMock, Mock

        # First strategy returns results
        first_search = AsyncMock(return_value=([sample_library_item], False))
        # Second strategy should not be called
        second_search = AsyncMock(return_value=([], None))

        strategies = build_strategies(
            search_library_func=first_search,
            search_alternative_func=second_search,
            search_compilations_func=AsyncMock(return_value=([], {})),
        )

        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
            artist="The Beatles",
        )

        mock_db = Mock()
        state = await execute_search_pipeline(parsed, mock_db, "test", strategies)

        assert len(state.results) == 1
        assert state.results[0] is sample_library_item
        first_search.assert_called_once()
        # Second search should not be called because first found results
        second_search.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_to_compilation_search_when_song_not_found(self, sample_library_item):
        """Test that pipeline continues to compilation search when song_not_found=True."""
        from unittest.mock import AsyncMock, Mock

        # Artist-only fallback (returns results but song not found)
        first_search = AsyncMock(return_value=([sample_library_item], True))  # fallback_used=True
        # Compilation search returns better results
        compilation_item = LibraryItem(
            id=2,
            title="Compilation Album",
            artist="Various Artists",
            call_letters="VA",
            artist_call_number=1,
            release_call_number=1,
        )
        compilation_search = AsyncMock(return_value=([compilation_item], {2: "Compilation"}))

        strategies = build_strategies(
            search_library_func=first_search,
            search_alternative_func=AsyncMock(return_value=([], None)),
            search_compilations_func=compilation_search,
        )

        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
            artist="The Beatles",
            song="Come Together",
        )

        mock_db = Mock()
        state = await execute_search_pipeline(parsed, mock_db, "test", strategies)

        # Should have compilation results (replaced artist-only results)
        assert len(state.results) == 1
        assert state.results[0].id == 2
        assert state.found_on_compilation is True
        assert state.song_not_found is False

    @pytest.mark.asyncio
    async def test_swapped_interpretation_with_dash_format(self, sample_library_item):
        """Test swapped interpretation parses 'Artist - Song' format."""
        from unittest.mock import AsyncMock, Mock

        # First search returns nothing
        first_search = AsyncMock(return_value=([], False))
        # Alternative search returns results when parts are swapped
        alternative_search = AsyncMock(return_value=([sample_library_item], None))

        strategies = build_strategies(
            search_library_func=first_search,
            search_alternative_func=alternative_search,
            search_compilations_func=AsyncMock(return_value=([], {})),
        )

        parsed = ParsedRequest(
            raw_message="Artist - Song",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        mock_db = Mock()
        state = await execute_search_pipeline(parsed, mock_db, "Artist - Song", strategies)

        assert len(state.results) == 1
        # Alternative search should be called with parsed parts
        alternative_search.assert_called_once()
        call_args = alternative_search.call_args[0]
        assert call_args[1] == "Artist"
        assert call_args[2] == "Song"

    @pytest.mark.asyncio
    async def test_song_as_artist_fallback(self, sample_library_item):
        """Test song-as-artist fallback when song parsed but no artist."""
        from unittest.mock import AsyncMock, Mock

        # All primary strategies return nothing
        first_search = AsyncMock(return_value=([], False))
        alternative_search = AsyncMock(return_value=([], None))
        compilation_search = AsyncMock(return_value=([], {}))
        # Song-as-artist returns results
        song_as_artist_search = AsyncMock(return_value=([sample_library_item], None))

        strategies = build_strategies(
            search_library_func=first_search,
            search_alternative_func=alternative_search,
            search_compilations_func=compilation_search,
            search_song_as_artist_func=song_as_artist_search,
        )

        parsed = ParsedRequest(
            raw_message="Laid Back",
            is_request=True,
            message_type=MessageType.REQUEST,
            song="Laid Back",  # Song parsed, but no artist
        )

        mock_db = Mock()
        state = await execute_search_pipeline(parsed, mock_db, "Laid Back", strategies)

        assert len(state.results) == 1
        song_as_artist_search.assert_called_once()
        # Should be called with parsed.song as the artist
        call_args = song_as_artist_search.call_args[0]
        assert call_args[1] == "Laid Back"

    @pytest.mark.asyncio
    async def test_tracks_strategies_tried(self):
        """Test that pipeline tracks which strategies were tried."""
        from unittest.mock import AsyncMock, Mock

        # All strategies return nothing
        strategies = build_strategies(
            search_library_func=AsyncMock(return_value=([], False)),
            search_alternative_func=AsyncMock(return_value=([], None)),
            search_compilations_func=AsyncMock(return_value=([], {})),
        )

        parsed = ParsedRequest(
            raw_message="Artist - Song",
            is_request=True,
            message_type=MessageType.REQUEST,
            artist="Artist",
            song="Song",
        )

        mock_db = Mock()
        state = await execute_search_pipeline(parsed, mock_db, "Artist - Song", strategies)

        # Should track all strategies that were tried
        assert SearchStrategyType.ARTIST_PLUS_ALBUM in state.strategies_tried
        assert SearchStrategyType.SWAPPED_INTERPRETATION in state.strategies_tried

    @pytest.mark.asyncio
    async def test_initializes_with_albums_for_search(self):
        """Test that pipeline initializes state with albums_for_search."""
        from unittest.mock import AsyncMock, Mock

        strategies = build_strategies(
            search_library_func=AsyncMock(return_value=([], False)),
            search_alternative_func=AsyncMock(return_value=([], None)),
            search_compilations_func=AsyncMock(return_value=([], {})),
        )

        parsed = ParsedRequest(
            raw_message="test",
            is_request=True,
            message_type=MessageType.REQUEST,
            artist="Artist",
        )

        mock_db = Mock()
        albums = ["Album 1", "Album 2"]
        state = await execute_search_pipeline(
            parsed, mock_db, "test", strategies, albums_for_search=albums
        )

        assert state.albums_for_search == albums
