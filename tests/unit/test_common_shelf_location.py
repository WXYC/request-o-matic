"""Direct pins for `scripts._common.shelf_location`.

The two CLIs render the helper's result through truthiness (`or "(none)"` and
a walrus), so a `""` return would pass every CLI test while breaking the
documented `str | None` contract for any third caller that checks `is None`.
This file pins the contract itself; the CLI suites pin what each surface prints.
"""

import pytest

from scripts._common import shelf_location


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        pytest.param({"call_number": "Rock CD PRA 4/2"}, "Rock CD PRA 4/2", id="composed"),
        pytest.param({"call_number": "(external)"}, "(external)", id="row-less-marker-is-verbatim"),
        pytest.param({"call_number": "", "call_letters": "MOL"}, None, id="empty-is-authoritative"),
        pytest.param(
            {"call_number": None, "call_letters": "MOL"}, None, id="null-is-authoritative"
        ),
        pytest.param(
            {"call_letters": "CAT", "artist_call_number": 7, "release_call_number": 1},
            "CAT 7/1",
            id="absent-falls-back",
        ),
        pytest.param(
            {"call_letters": "V/A", "artist_call_number": 0, "release_call_number": 3},
            "V/A 0/3",
            id="fallback-keeps-zero",
        ),
        pytest.param(
            {"call_letters": "CAT", "artist_call_number": None, "release_call_number": None},
            None,
            id="fallback-needs-a-number",
        ),
        pytest.param({"call_letters": ""}, None, id="empty-letters"),
        pytest.param({}, None, id="empty-item"),
    ],
)
def test_shelf_location_contract(item, expected):
    """Returns the locator string, or exactly None when there is nothing to print."""
    result = shelf_location(item)

    assert result == expected
    assert (result is None) == (expected is None)
