from types import SimpleNamespace

import pytest

from airsenal.scripts import make_transfers as mt
from airsenal.scripts.make_transfers import (
    build_init_priced_transfers,
    build_transfer_payload,
)

FETCHER = SimpleNamespace(FPL_TEAM_ID=123)


def test_build_transfer_payload_no_chip():
    payload = build_transfer_payload([], 5, FETCHER, None)
    assert payload["chip"] is None
    assert payload["wildcard"] is False
    assert payload["freehit"] is False
    assert payload["entry"] == 123
    assert payload["event"] == 5


@pytest.mark.parametrize(
    ("chip", "key"), [("wildcard", "wildcard"), ("free_hit", "freehit")]
)
def test_build_transfer_payload_api_chips(chip, key):
    payload = build_transfer_payload([], 5, FETCHER, chip)
    # the current FPL client sends the chip name; the boolean is the legacy form
    assert payload["chip"] == key
    assert payload[key] is True


@pytest.mark.parametrize("chip", ["triple_captain", "bench_boost"])
def test_build_transfer_payload_manual_chips_not_in_payload(chip):
    payload = build_transfer_payload([], 5, FETCHER, chip)
    assert payload["chip"] is None
    assert "triplecaptain" not in payload
    assert "benchboost" not in payload
    assert payload["wildcard"] is False
    assert payload["freehit"] is False


class FakeFetcher:
    """Owns players with API ids 11..15 (prices 50); never logs in."""

    FPL_TEAM_ID = 123

    def __init__(self, *_args, **_kwargs):
        self.posted: list[dict] = []

    def get_current_picks(self, _team_id=None):
        return [{"element": api_id, "selling_price": 50} for api_id in range(11, 16)]

    def get_player_summary_data(self):
        return {api_id: {"now_cost": 50} for api_id in range(11, 30)}

    def post_transfers(self, payload):
        self.posted.append(payload)


POSITIONS = {14: "DEF", 15: "MID", 21: "DEF", 22: "MID"}


def suggest(monkeypatch, api_ids):
    # DB player_id == API id here
    monkeypatch.setattr(
        mt,
        "get_transfer_suggestions",
        lambda *_a, **_k: [SimpleNamespace(player_id=i) for i in api_ids],
    )
    monkeypatch.setattr(
        mt, "get_player", lambda pid: SimpleNamespace(fpl_api_id=pid, player_id=pid)
    )
    monkeypatch.setattr(
        mt,
        "get_player_from_api_id",
        lambda api_id: SimpleNamespace(
            position=lambda _season: POSITIONS.get(api_id, "FWD")
        ),
    )


@pytest.fixture
def initial_squad_env(monkeypatch):
    # the suggested squad is the one already owned
    suggest(monkeypatch, range(11, 16))
    fetcher = FakeFetcher()
    monkeypatch.setattr(mt, "FPLDataFetcher", lambda *_a, **_k: fetcher)
    return fetcher


def test_build_init_priced_transfers_nothing_to_change(initial_squad_env):
    assert build_init_priced_transfers(initial_squad_env, 123) == []


def test_build_init_priced_transfers_partial_overlap(monkeypatch):
    # 13 of the suggested 15 are owned already: two like-for-like transfers
    suggest(monkeypatch, [11, 12, 13, 21, 22])
    priced = build_init_priced_transfers(FakeFetcher(), 123)
    assert priced == [
        {
            "element_in": 21,
            "purchase_price": 50,
            "element_out": 14,
            "selling_price": 50,
        },
        {
            "element_in": 22,
            "purchase_price": 50,
            "element_out": 15,
            "selling_price": 50,
        },
    ]


def test_make_transfers_skips_post_when_squad_already_matches(
    initial_squad_env, monkeypatch, capsys
):
    monkeypatch.setattr(
        mt,
        "get_gw_transfer_suggestions",
        lambda _team: ([[], list(range(11, 16))], 123, 1, None),
    )
    assert mt.make_transfers(123, skip_check=True) is True
    assert initial_squad_env.posted == []
    assert "No transfers needed" in capsys.readouterr().out
