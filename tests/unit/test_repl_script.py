"""Unit tests for scripts/repl.py output rendering.

Companion to tests/unit/test_lookup_script.py. Both scripts are documented
operator tools (docs/scripts.md) and both read the same `/request` response, so
both have to survive the server's degraded `parsing_unavailable` shape.
"""

import pytest

from routers.request import DEGRADED_SEARCH
from scripts.repl import print_result
from services.slack import build_slack_blocks
from tests.factories import make_degraded_response, make_library_item


def _response_with(item: dict) -> dict:
    """A minimal healthy `/request` body carrying one library result."""
    return {
        "parsed": {"is_request": True, "message_type": "request", "artist": "Juana Molina"},
        "library_results": [item],
        "artwork": None,
    }


class TestDegradedParsingOutput:
    def test_null_parsed_reports_unavailable_instead_of_raising(self, capsys):
        """`parsed: null` must render a diagnosis, not an AttributeError.

        `data.get("parsed", {})` returns None for a present-but-null key rather
        than the `{}` default, so the subsequent `.get` blew up the REPL. An
        operator sitting in the REPL is the most likely person to be present
        when parsing goes degraded, so this is exactly where a clear message
        matters most.
        """
        print_result(make_degraded_response())
        out = capsys.readouterr().out
        assert "unavailable" in out.lower()

    def test_degraded_output_does_not_claim_a_parse(self, capsys):
        """The degraded notice must not print empty parse slots alongside itself.

        `Is Request: None` / `Type: None` reads as "parsed, found nothing",
        which is a different and misleading diagnosis.
        """
        print_result(make_degraded_response())
        out = capsys.readouterr().out
        assert "Is Request:" not in out

    def test_normal_response_still_renders_parse_fields(self, capsys):
        """Regression guard: the degraded branch must not swallow the happy path."""
        print_result(
            {
                "parsed": {
                    "is_request": True,
                    "message_type": "request",
                    "artist": "Juana Molina",
                    "song": "la paradoja",
                    "album": "DOGA",
                },
                "library_results": [],
                "artwork": None,
            }
        )
        out = capsys.readouterr().out
        assert "Juana Molina" in out
        assert "la paradoja" in out

    def test_search_unavailable_is_not_reported_as_no_results(self, capsys):
        """An LML outage must not render as "not in the library".

        `search_unavailable` leaves `parsed` populated and `library_results`
        empty, so inferring degradation from a null `parsed` misses it entirely
        and the operator is told the record isn't in the catalog.
        """
        print_result(make_degraded_response(DEGRADED_SEARCH))
        out = capsys.readouterr().out.lower()
        assert "unavailable" in out
        assert "no results found." not in out


class TestReplShelfLocation:
    """The REPL must print the locator the service composed, like its siblings.

    `scripts/lookup.py` stopped re-deriving `Location:` from `call_letters`,
    `artist_call_number` and `release_call_number` in #298, because the derived
    string drops genre -- the shelf *section* -- and lands on a slot that can
    hold dozens of records across a dozen genres. The REPL reads the same
    `/request` body and renders the same line, so the divergence lived here
    too; `services/slack.py` has rendered `item.call_number` all along.
    """

    def test_location_prints_the_composed_call_number(self, capsys):
        """The wire `call_number` is printed verbatim, genre and format included."""
        item = make_library_item(
            artist="Jessica Pratt",
            title="On Your Own Love Again",
            genre="Rock",
            format="CD",
            call_letters="PRA",
            artist_call_number=4,
            release_call_number=2,
        )

        print_result(_response_with(item.model_dump(mode="json")))

        assert "Location: Rock CD PRA 4/2" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("components", "expected"),
        [
            pytest.param(
                {"call_letters": "CAT", "artist_call_number": 7, "release_call_number": 1},
                "CAT 7/1",
                id="both-components-present",
            ),
            pytest.param(
                {"call_letters": "V/A", "artist_call_number": 0, "release_call_number": 3},
                "V/A 0/3",
                id="zero-artist-number-is-a-value-not-a-blank",
            ),
        ],
    )
    def test_location_falls_back_to_components_when_call_number_absent(
        self, capsys, components, expected
    ):
        """A deployed service predating the field still yields a usable locator."""
        print_result(_response_with({"title": "Moon Pix", "artist": "Cat Power", **components}))

        # Exact, not containment -- see the lookup counterpart for why.
        location_line = next(
            line for line in capsys.readouterr().out.splitlines() if "Location:" in line
        )
        assert location_line.strip() == f"Location: {expected}"

    @pytest.mark.parametrize(
        "item",
        [
            pytest.param({"artist": "Juana Molina", "title": "DOGA"}, id="nothing-to-print"),
            pytest.param(
                {
                    "artist": "Juana Molina",
                    "title": "DOGA",
                    "call_number": "",
                    "call_letters": "MOL",
                    "artist_call_number": 0,
                    "release_call_number": 3,
                },
                id="empty-call-number-does-not-resurrect-the-derivation",
            ),
            pytest.param(
                {
                    "artist": "Juana Molina",
                    "title": "DOGA",
                    "call_number": None,
                    "call_letters": "MOL",
                    "artist_call_number": 0,
                    "release_call_number": 3,
                },
                id="null-call-number-does-not-resurrect-the-derivation",
            ),
            pytest.param(
                {
                    "artist": "Juana Molina",
                    "title": "DOGA",
                    "call_letters": "",
                    "artist_call_number": 7,
                    "release_call_number": 1,
                },
                id="empty-call-letters-do-not-print-a-letterless-locator",
            ),
            pytest.param(
                {
                    "artist": "Juana Molina",
                    "title": "DOGA",
                    "call_letters": "MOL",
                    "artist_call_number": None,
                    "release_call_number": None,
                },
                id="letters-with-no-numbers-do-not-print-a-bare-slash",
            ),
        ],
    )
    def test_no_locator_omits_the_line_rather_than_inventing_one(self, capsys, item):
        """The REPL prints optional fields only when populated -- see its `genre` branch."""
        print_result(_response_with(item))

        assert "Location:" not in capsys.readouterr().out

    def test_repl_and_slack_render_the_same_locator(self, capsys):
        """One record, one shelf slot, whichever surface names it.

        Pinned against `item.call_number` on both sides, which is also what
        `tests/unit/test_lookup_script.py` pins the `lookup` CLI against, so
        all three surfaces are transitively held to one string.
        """
        item = make_library_item(
            artist="Duke Ellington & John Coltrane",
            title="Duke Ellington & John Coltrane",
            genre="Jazz",
            format="CD",
            call_letters="ELL",
            artist_call_number=12,
            release_call_number=3,
        )

        print_result(_response_with(item.model_dump(mode="json")))
        repl_line = next(
            line for line in capsys.readouterr().out.splitlines() if "Location:" in line
        )
        repl_locator = repl_line.split("Location:", 1)[1].strip()

        # build_slack_blocks emits [header, item]; the item block's text is
        # `*artist*` / title / `_call_number_`. Index the known slot rather
        # than scanning for underscores, which a title like `_o_` would match.
        blocks = build_slack_blocks("Now playing", [(item, None)])
        slack_line = blocks[1]["text"]["text"].splitlines()[2]
        slack_locator = slack_line.removeprefix("_").removesuffix("_")

        assert repl_locator == slack_locator == item.call_number
