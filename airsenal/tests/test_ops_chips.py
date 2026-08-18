"""
Chip-availability rules for the Pi automation (2026/27: all four chips reset
at the half boundary; one full set per half).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "ops" / "helpers"))
from gw_context import CHIPS, compute_available_chips, confidence_threshold

SEASON = "2627"


def played(*items):
    return [{"chip": chip, "gw": gw} for chip, gw in items]


def test_all_available_mid_half_when_nothing_played():
    available, _ = compute_available_chips(5, SEASON, [], [])
    assert available == CHIPS


def test_gw1_excludes_wildcard_and_free_hit():
    available, _ = compute_available_chips(1, SEASON, [], [])
    assert available == ["triple_captain", "bench_boost"]


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
