"""Pi automation helpers: squad value from the picks dict, POST markers."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "ops" / "helpers"))
from gw_context import squad_value
from post_transfers import with_markers


def test_squad_value_sums_selling_prices_of_picks_keyed_by_element():
    picks = {
        101: {"element": 101, "selling_price": 55},
        202: {"element": 202, "selling_price": 101},
    }
    assert squad_value(picks) == pytest.approx(15.6)


def test_posted_is_written_before_the_post_and_applied_after_it(tmp_path):
    posted, applied = tmp_path / "gw5.posted", tmp_path / "gw5.applied"
    seen = []

    def post(payload):
        seen.append((payload, posted.exists(), applied.exists()))

    with_markers(post, posted, applied)({"transfers": []})

    assert seen == [({"transfers": []}, True, False)]
    assert applied.exists()


def test_failed_post_leaves_posted_without_applied(tmp_path):
    posted, applied = tmp_path / "gw5.posted", tmp_path / "gw5.applied"

    def post(_payload):
        msg = "connection reset"
        raise ConnectionError(msg)

    with pytest.raises(ConnectionError):
        with_markers(post, posted, applied)({})

    assert posted.exists()
    assert not applied.exists()
