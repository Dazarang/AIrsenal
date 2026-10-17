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


class FakeFetcher:
    def __init__(self, logs_in=True):
        self.logs_in = logs_in
        self.logged_in = False

    def login(self):
        self.logged_in = self.logs_in


def test_posted_is_written_before_the_post_and_applied_after_it(tmp_path):
    posted, applied = tmp_path / "gw5.posted", tmp_path / "gw5.applied"
    seen = []

    def post(_fetcher, payload):
        seen.append((payload, posted.exists(), applied.exists()))

    with_markers(post, posted, applied)(FakeFetcher(), {"transfers": []})

    assert seen == [({"transfers": []}, True, False)]
    assert applied.exists()


def test_failed_post_leaves_posted_without_applied(tmp_path):
    posted, applied = tmp_path / "gw5.posted", tmp_path / "gw5.applied"

    def post(_fetcher, _payload):
        msg = "connection reset"
        raise ConnectionError(msg)

    with pytest.raises(ConnectionError):
        with_markers(post, posted, applied)(FakeFetcher(), {})

    assert posted.exists()
    assert not applied.exists()


def test_failed_login_raises_before_posted_so_a_retry_stays_open(tmp_path):
    posted, applied = tmp_path / "gw5.posted", tmp_path / "gw5.applied"
    calls = []

    with pytest.raises(RuntimeError, match="login failed"):
        with_markers(lambda *a: calls.append(a), posted, applied)(
            FakeFetcher(logs_in=False), {}
        )

    assert calls == []
    assert not posted.exists()
    assert not applied.exists()
