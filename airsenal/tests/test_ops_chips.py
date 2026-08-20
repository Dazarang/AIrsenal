"""
Chip-availability and chip-state rules for the Pi automation (2026/27: all
four chips reset at the half boundary; one full set per half).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "ops" / "helpers"))
from gw_context import (
    CHIPS,
    active_chip_name,
    chip_stock,
    clear_decision,
    compute_available_chips,
    confidence_threshold,
    entry_for_gw,
    reconcile_state,
    record_decision,
)

SEASON = "2627"


def played(*items):
    return [{"chip": chip, "gw": gw} for chip, gw in items]


def test_all_available_mid_half_when_nothing_played():
    available, _ = compute_available_chips(5, SEASON, [], [])
    assert available == CHIPS


def test_gw1_excludes_every_chip_but_stock_is_full():
    # the GW1 optimizer path builds the squad from scratch and ignores chip flags
    available, _ = compute_available_chips(1, SEASON, [], [])
    assert available == []
    assert chip_stock(1, SEASON, [], []) == CHIPS


def test_stock_ignores_this_weeks_rules_but_not_usage():
    history = played(("free_hit", 19))
    assert chip_stock(20, SEASON, history, []) == CHIPS  # FH is a new set in GW20
    available, _ = compute_available_chips(20, SEASON, history, [])
    assert "free_hit" not in available
    assert "wildcard" not in chip_stock(20, SEASON, played(("wildcard", 22)), [])


def test_used_chip_gone_for_rest_of_half():
    available, _ = compute_available_chips(10, SEASON, played(("wildcard", 3)), [])
    assert "wildcard" not in available


def test_every_chip_resets_in_second_half():
    first_half = played(
        ("wildcard", 3), ("free_hit", 8), ("triple_captain", 12), ("bench_boost", 15)
    )
    available, _ = compute_available_chips(25, SEASON, first_half, [])
    assert available == CHIPS


def test_second_half_use_consumes_second_set():
    history = played(("bench_boost", 5), ("bench_boost", 22))
    available, _ = compute_available_chips(30, SEASON, history, [])
    assert "bench_boost" not in available


def test_free_hit_gw19_blocks_gw20_only():
    history = played(("free_hit", 19))
    available_20, notes = compute_available_chips(20, SEASON, history, [])
    assert "free_hit" not in available_20
    assert any("GW20" in n for n in notes)
    available_21, _ = compute_available_chips(21, SEASON, history, [])
    assert "free_hit" in available_21


def test_chip_active_this_gw_blocks_everything():
    available, notes = compute_available_chips(7, SEASON, played(("wildcard", 7)), [])
    assert available == []
    assert any("already active" in n for n in notes)


def test_pending_decision_blocks_within_half():
    entries = [{"chip": "triple_captain", "gw": 6, "season": SEASON}]
    available, _ = compute_available_chips(8, SEASON, [], entries)
    assert "triple_captain" not in available


def test_lapsed_decision_returns_to_pool():
    entries = [{"chip": "triple_captain", "gw": 6, "season": SEASON, "lapsed": True}]
    available, _ = compute_available_chips(8, SEASON, [], entries)
    assert "triple_captain" in available


def test_current_gw_unconfirmed_decision_stays_available_for_rerun():
    entries = [{"chip": "bench_boost", "gw": 8, "season": SEASON}]
    available, _ = compute_available_chips(8, SEASON, [], entries)
    assert "bench_boost" in available


def test_current_gw_confirmed_decision_is_blocked():
    entries = [{"chip": "bench_boost", "gw": 8, "season": SEASON, "confirmed": True}]
    available, _ = compute_available_chips(8, SEASON, [], entries)
    assert "bench_boost" not in available


def test_old_season_entries_ignored():
    entries = [{"chip": "wildcard", "gw": 8, "season": "2526", "confirmed": True}]
    available, _ = compute_available_chips(8, SEASON, [], entries)
    assert "wildcard" in available


def test_confidence_threshold_relaxes_near_expiry():
    assert confidence_threshold(None) == 0.6
    assert confidence_threshold(10) == 0.6
    assert confidence_threshold(3) == 0.6
    assert confidence_threshold(2) == 0.5
    assert confidence_threshold(0) == 0.5


# --- chip state -------------------------------------------------------------


def entry(gw, chip, **extra):
    return {"gw": gw, "season": SEASON, "chip": chip, **extra}


def test_reconcile_drops_previous_seasons_confirms_and_lapses():
    state = {
        "entries": [
            {"gw": 3, "season": "2526", "chip": "wildcard", "confirmed": True},
            entry(3, "wildcard"),  # activated: shows up in history
            entry(5, "triple_captain"),  # decided, never activated
            entry(6, "free_hit", confirmed=True),  # was active, cancelled again
            entry(8, "bench_boost"),  # this gameweek, active on my-team
            entry(8, "free_hit"),  # this gameweek, nothing happened yet
        ]
    }
    notes = reconcile_state(
        state,
        8,
        SEASON,
        played(("wildcard", 3)),
        active="bench_boost",
        available_now={"free_hit", "triple_captain"},
    )
    by_key = {(e["gw"], e["chip"]): e for e in state["entries"]}
    assert all(e["season"] == SEASON for e in state["entries"])
    assert by_key[(3, "wildcard")]["confirmed"] is True
    assert by_key[(5, "triple_captain")]["lapsed"] is True
    assert by_key[(6, "free_hit")]["lapsed"] is True
    assert by_key[(6, "free_hit")]["confirmed"] is False
    assert by_key[(8, "bench_boost")]["confirmed"] is True
    assert "lapsed" not in by_key[(8, "free_hit")]
    assert "confirmed" not in by_key[(8, "free_hit")]
    assert notes == [
        (
            "triple_captain was decided for GW5 but never played - it is back in "
            "the available pool."
        ),
        (
            "free_hit was decided for GW6 but never played - it is back in the "
            "available pool."
        ),
    ]
    # lapsed entries no longer reserve the chip
    assert "free_hit" in chip_stock(8, SEASON, [], state["entries"])


def test_record_replaces_unconfirmed_decision_for_gameweek():
    state = {"entries": [entry(4, "wildcard", confirmed=True), entry(8, "wildcard")]}
    record_decision(state, 8, SEASON, "triple_captain", 0.7, "why")
    assert [e["chip"] for e in state["entries"]] == ["wildcard", "triple_captain"]
    new = state["entries"][-1]
    assert new["gw"] == 8
    assert new["confirmed"] is False
    assert new["reasoning"] == "why"


def test_reconcile_lapses_confirmed_only_when_server_shows_chip_available():
    # history endpoint missing the chip is not enough on its own
    state = {"entries": [entry(6, "free_hit", confirmed=True)]}
    assert reconcile_state(state, 8, SEASON, [], None) == []
    assert state["entries"][0]["confirmed"] is True
    # ... nor when the my-team view says it is spent/unavailable
    assert reconcile_state(state, 8, SEASON, [], None, {"wildcard"}) == []
    assert state["entries"][0]["confirmed"] is True
    # the server handing the chip back as available is the proof
    notes = reconcile_state(state, 8, SEASON, [], None, {"free_hit", "wildcard"})
    assert len(notes) == 1
    assert state["entries"][0]["lapsed"] is True
    # unconfirmed past decisions lapse regardless of the server view
    state = {"entries": [entry(6, "triple_captain")]}
    reconcile_state(state, 8, SEASON, [], None)
    assert state["entries"][0]["lapsed"] is True


def test_clear_drops_unconfirmed_but_keeps_confirmed():
    state = {"entries": [entry(7, "bench_boost"), entry(8, "wildcard")]}
    clear_decision(state, 8)
    assert state["entries"] == [entry(7, "bench_boost")]
    state = {"entries": [entry(8, "wildcard", confirmed=True)]}
    clear_decision(state, 8)
    assert state["entries"] == [entry(8, "wildcard", confirmed=True)]


def test_record_same_chip_over_confirmed_is_a_noop_other_chip_refused():
    state = {"entries": [entry(8, "wildcard", confirmed=True)]}
    record_decision(state, 8, SEASON, "wildcard", 1, "already active")
    assert state["entries"] == [entry(8, "wildcard", confirmed=True)]
    with pytest.raises(ValueError, match="confirmed chip entry"):
        record_decision(state, 8, SEASON, "free_hit", 0.9, "")


def test_entry_for_gw_skips_lapsed_and_prefers_newest():
    state = {"entries": [entry(8, "wildcard", lapsed=True), entry(8, "bench_boost")]}
    assert entry_for_gw(state, 8)["chip"] == "bench_boost"
    assert entry_for_gw(state, 9) is None


def test_active_chip_name_maps_api_names():
    chips = [
        {"name": "bboost", "status_for_entry": "available"},
        {"name": "3xc", "status_for_entry": "active"},
    ]
    assert active_chip_name({"chips": chips}) == "triple_captain"
    assert active_chip_name({"chips": chips[:1]}) is None
    assert active_chip_name({}) is None
