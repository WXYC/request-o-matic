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
from tests.conftest import make_parsed_request


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
        parsed = make_parsed_request(artist="The Beatles")
        empty_state.albums_for_search = ["Abbey Road"]
        assert has_artist_or_album_or_song(parsed, empty_state, "test") is True

    def test_has_artist_or_album_or_song_with_artist_and_song(self, empty_state):
        parsed = make_parsed_request(artist="The Beatles", song="Come Together")
        assert has_artist_or_album_or_song(parsed, empty_state, "test") is True

    def test_has_artist_or_album_or_song_with_artist_only(self, empty_state):
        parsed = make_parsed_request(artist="The Beatles")
        assert has_artist_or_album_or_song(parsed, empty_state, "test") is True

    def test_has_artist_or_album_or_song_no_artist_but_has_song(self, empty_state):
        parsed = make_parsed_request(song="Come Together")
        empty_state.albums_for_search = ["Abbey Road"]
        assert has_artist_or_album_or_song(parsed, empty_state, "test") is True

    def test_has_artist_or_album_or_song_nothing(self, empty_state):
        parsed = make_parsed_request()
        assert has_artist_or_album_or_song(parsed, empty_state, "test") is False

    def test_no_results_and_ambiguous_format_with_dash(self, empty_state):
        parsed = make_parsed_request(raw_message="Artist - Song")
        assert no_results_and_ambiguous_format(parsed, empty_state, "Artist - Song") is True

    def test_no_results_and_ambiguous_format_with_results(self, state_with_results):
        parsed = make_parsed_request(raw_message="Artist - Song")
        assert no_results_and_ambiguous_format(parsed, state_with_results, "Artist - Song") is False

    def test_no_results_and_ambiguous_format_no_dash(self, empty_state):
        parsed = make_parsed_request(raw_message="Play song by artist")
        assert no_results_and_ambiguous_format(parsed, empty_state, "Play song by artist") is False

    def test_song_not_found_with_artist_and_song_true(self, empty_state):
        empty_state.song_not_found = True
        parsed = make_parsed_request(artist="The Beatles", song="Come Together")
        assert song_not_found_with_artist_and_song(parsed, empty_state, "test") is True

    def test_song_not_found_with_artist_and_song_false_when_found(self, empty_state):
        empty_state.song_not_found = False
        parsed = make_parsed_request(artist="The Beatles", song="Come Together")
        assert song_not_found_with_artist_and_song(parsed, empty_state, "test") is False

    def test_song_not_found_with_artist_and_song_false_no_artist(self, empty_state):
        empty_state.song_not_found = True
        parsed = make_parsed_request(song="Come Together")
        assert song_not_found_with_artist_and_song(parsed, empty_state, "test") is False

    def test_no_results_and_song_but_no_artist_true(self, empty_state):
        parsed = make_parsed_request(song="Laid Back", raw_message="Laid Back")
        assert no_results_and_song_but_no_artist(parsed, empty_state, "Laid Back") is True

    def test_no_results_and_song_but_no_artist_false_has_results(self, state_with_results):
        parsed = make_parsed_request(song="Laid Back", raw_message="Laid Back")
        assert no_results_and_song_but_no_artist(parsed, state_with_results, "Laid Back") is False

    def test_no_results_and_song_but_no_artist_false_has_artist(self, empty_state):
        parsed = make_parsed_request(artist="The Beatles", song="Come Together")
        assert no_results_and_song_but_no_artist(parsed, empty_state, "test") is False

    def test_no_results_and_song_but_no_artist_false_no_song(self, empty_state):
        parsed = make_parsed_request()
        assert no_results_and_song_but_no_artist(parsed, empty_state, "test") is False


class TestBuildStrategies:
    """Test build_strategies function."""

    def test_builds_three_strategies_without_song_as_artist(self):
        async def mock_strategy(db, parsed, state):
            pass

        strategies = build_strategies(
            search_library_func=mock_strategy,
            search_alternative_func=mock_strategy,
            search_compilations_func=mock_strategy,
        )
        assert len(strategies) == 3

    def test_builds_four_strategies_with_song_as_artist(self):
        async def mock_strategy(db, parsed, state):
            pass

        strategies = build_strategies(
            search_library_func=mock_strategy,
            search_alternative_func=mock_strategy,
            search_compilations_func=mock_strategy,
            search_song_as_artist_func=mock_strategy,
        )
        assert len(strategies) == 4

    def test_strategies_in_correct_order(self):
        async def mock_strategy(db, parsed, state):
            pass

        strategies = build_strategies(
            search_library_func=mock_strategy,
            search_alternative_func=mock_strategy,
            search_compilations_func=mock_strategy,
            search_song_as_artist_func=mock_strategy,
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
    """Test execute_search_pipeline function.

    Strategy execute functions follow the uniform (db, parsed, state) -> None
    signature and mutate state directly.
    """

    @pytest.mark.asyncio
    async def test_stops_after_finding_results(self, sample_library_item):
        """Test that pipeline stops after first strategy finds results."""
        from unittest.mock import AsyncMock, Mock

        call_log = []

        async def first_strategy(db, parsed, state):
            call_log.append("first")
            state.results = [sample_library_item]

        async def second_strategy(db, parsed, state):
            call_log.append("second")

        strategies = build_strategies(
            search_library_func=first_strategy,
            search_alternative_func=second_strategy,
            search_compilations_func=AsyncMock(),
        )

        parsed = make_parsed_request(artist="The Beatles")
        state = await execute_search_pipeline(parsed, Mock(), "test", strategies)

        assert len(state.results) == 1
        assert state.results[0] is sample_library_item
        assert call_log == ["first"]

    @pytest.mark.asyncio
    async def test_continues_to_compilation_search_when_song_not_found(self, sample_library_item):
        """Test that pipeline continues to compilation search when song_not_found=True."""
        from unittest.mock import Mock

        compilation_item = LibraryItem(
            id=2,
            title="Compilation Album",
            artist="Various Artists",
            call_letters="VA",
            artist_call_number=1,
            release_call_number=1,
        )

        async def library_strategy(db, parsed, state):
            state.results = [sample_library_item]
            state.song_not_found = True  # fallback used

        async def compilation_strategy(db, parsed, state):
            state.results = [compilation_item]
            state.found_on_compilation = True
            state.song_not_found = False
            state.discogs_titles = {2: "Compilation"}

        async def noop_strategy(db, parsed, state):
            pass

        strategies = build_strategies(
            search_library_func=library_strategy,
            search_alternative_func=noop_strategy,
            search_compilations_func=compilation_strategy,
        )

        parsed = make_parsed_request(artist="The Beatles", song="Come Together")
        state = await execute_search_pipeline(parsed, Mock(), "test", strategies)

        assert len(state.results) == 1
        assert state.results[0].id == 2
        assert state.found_on_compilation is True
        assert state.song_not_found is False

    @pytest.mark.asyncio
    async def test_swapped_interpretation_with_dash_format(self, sample_library_item):
        """Test swapped interpretation strategy runs for 'Artist - Song' format."""
        from unittest.mock import Mock

        async def noop_strategy(db, parsed, state):
            pass

        async def alternative_strategy(db, parsed, state):
            state.results = [sample_library_item]
            state.song_not_found = False

        strategies = build_strategies(
            search_library_func=noop_strategy,
            search_alternative_func=alternative_strategy,
            search_compilations_func=noop_strategy,
        )

        parsed = make_parsed_request(raw_message="Artist - Song")
        state = await execute_search_pipeline(parsed, Mock(), "Artist - Song", strategies)

        assert len(state.results) == 1
        assert SearchStrategyType.SWAPPED_INTERPRETATION in state.strategies_tried

    @pytest.mark.asyncio
    async def test_song_as_artist_fallback(self, sample_library_item):
        """Test song-as-artist fallback when song parsed but no artist."""
        from unittest.mock import Mock

        call_log = []

        async def noop_strategy(db, parsed, state):
            pass

        async def song_as_artist_strategy(db, parsed, state):
            call_log.append(("song_as_artist", parsed.song))
            state.results = [sample_library_item]
            state.song_not_found = False

        strategies = build_strategies(
            search_library_func=noop_strategy,
            search_alternative_func=noop_strategy,
            search_compilations_func=noop_strategy,
            search_song_as_artist_func=song_as_artist_strategy,
        )

        parsed = make_parsed_request(song="Laid Back", raw_message="Laid Back")
        state = await execute_search_pipeline(parsed, Mock(), "Laid Back", strategies)

        assert len(state.results) == 1
        assert call_log == [("song_as_artist", "Laid Back")]

    @pytest.mark.asyncio
    async def test_tracks_strategies_tried(self):
        """Test that pipeline tracks which strategies were tried."""
        from unittest.mock import Mock

        async def noop_strategy(db, parsed, state):
            pass

        strategies = build_strategies(
            search_library_func=noop_strategy,
            search_alternative_func=noop_strategy,
            search_compilations_func=noop_strategy,
        )

        parsed = make_parsed_request(artist="Artist", song="Song", raw_message="Artist - Song")
        state = await execute_search_pipeline(parsed, Mock(), "Artist - Song", strategies)

        assert SearchStrategyType.ARTIST_PLUS_ALBUM in state.strategies_tried
        assert SearchStrategyType.SWAPPED_INTERPRETATION in state.strategies_tried

    @pytest.mark.asyncio
    async def test_initializes_with_albums_for_search(self):
        """Test that pipeline initializes state with albums_for_search."""
        from unittest.mock import Mock

        async def noop_strategy(db, parsed, state):
            pass

        strategies = build_strategies(
            search_library_func=noop_strategy,
            search_alternative_func=noop_strategy,
            search_compilations_func=noop_strategy,
        )

        parsed = make_parsed_request(artist="Artist")
        albums = ["Album 1", "Album 2"]
        state = await execute_search_pipeline(
            parsed, Mock(), "test", strategies, albums_for_search=albums
        )

        assert state.albums_for_search == albums
