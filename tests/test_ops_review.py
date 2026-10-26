"""Pi automation weekly review: captain, predictions and season sums."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "ops" / "helpers"))
from gw_review import (
    captain_review,
    counted_picks,
    format_review,
    last_checked_gameweek,
    live_points,
    prediction_review,
    season_of,
    season_review,
)


def make_picks(captain=8, chip=None):
    """Fifteen picks, elements 1-15 in positions 1-15, no automatic subs."""
    captain_multiplier = 3 if chip == "3xc" else 2
    return {
        "active_chip": chip,
        "automatic_subs": [],
        "picks": [
            {
                "element": e,
                "position": e,
                "is_captain": e == captain,
                "multiplier": captain_multiplier
                if e == captain
                else int(e <= 11 or chip == "bboost"),
            }
            for e in range(15, 0, -1)
        ],
    }


def substitute(picks, element_out, element_in):
    """Apply an automatic sub the way a finished gameweek's picks show it."""
    by_element = {p["element"]: p for p in picks["picks"]}
    out, sub = by_element[element_out], by_element[element_in]
    out["position"], sub["position"] = sub["position"], out["position"]
    sub["multiplier"], out["multiplier"] = 1, 0
    picks["automatic_subs"].append(
        {"element_in": element_in, "element_out": element_out}
    )
    return picks


# elements 1-15: GW8-like, captain 8 scored -1, starter 5 scored 17
POINTS = dict.fromkeys(range(1, 16), (2, 90))
POINTS |= {5: (17, 90), 8: (-1, 79), 12: (0, 0), 13: (9, 90)}


def test_last_checked_needs_both_finished_and_data_checked():
    events = [
        {"id": 7, "finished": True, "data_checked": True},
        {"id": 8, "finished": True, "data_checked": False},
        {"id": 9, "finished": False, "data_checked": False},
    ]
    assert last_checked_gameweek(events) == 7
    assert last_checked_gameweek(events[2:]) is None


def test_live_points_reads_points_and_minutes():
    live = {"elements": [{"id": 3, "stats": {"total_points": 6, "minutes": 90}}]}
    assert live_points(live) == {3: (6, 90)}


def test_season_of_reads_gw1_deadline():
    assert season_of([{"deadline_time": "2026-08-21T17:30:00Z"}]) == "2627"
    assert season_of([{"deadline_time": "2099-08-21T17:30:00Z"}]) == "9900"


def test_counted_picks_use_positions_not_the_autosub_multipliers():
    counted = counted_picks(make_picks())
    assert [e for e, _ in counted] == list(range(1, 12))
    assert dict(counted)[8] == 2


def test_bench_boost_counts_fifteen_and_triple_captain_triples():
    assert len(counted_picks(make_picks(chip="bboost"))) == 15
    assert dict(counted_picks(make_picks(chip="3xc")))[8] == 3


def test_captain_review_compares_with_the_best_starter_only():
    review = captain_review(make_picks(), POINTS)
    # the bench's 9 points (element 13) do not count as a captain option
    assert review == {
        "captain": 8,
        "captain_points": -1,
        "best": 5,
        "best_points": 17,
        "lost": 18,
    }


def test_captain_review_doubles_the_loss_under_triple_captain():
    assert captain_review(make_picks(chip="3xc"), POINTS)["lost"] == 36


def test_counted_picks_undo_automatic_subs():
    # GW8: 391 on for 447 and 14 on for 165, both starters played 0 minutes
    picks = substitute(substitute(make_picks(), 3, 13), 10, 14)
    counted = dict(counted_picks(picks))
    assert sorted(counted) == list(range(1, 12))
    assert counted[8] == 2


def test_captain_subbed_off_gives_the_armband_to_the_vice():
    # the captain played 0 minutes and was replaced by a bench player; the
    # vice-captain (5) scored 17 and took the armband
    picks = substitute(make_picks(), 8, 13)
    vice = next(p for p in picks["picks"] if p["element"] == 5)
    vice["multiplier"] = 2
    points = POINTS | {8: (0, 0)}

    review = captain_review(picks, points)

    assert review["captain"] == 8
    assert review["captain_points"] == 0
    assert review["best"] == 5
    # the vice already earned what captaining the best starter would have
    assert review["lost"] == 0
    assert dict(counted_picks(picks))[8] == 2


def test_captain_and_vice_both_absent_loses_the_whole_best_score():
    picks = substitute(make_picks(), 8, 13)
    assert captain_review(picks, POINTS | {8: (0, 0)})["lost"] == 17


def test_prediction_review_scores_the_deadline_xi_not_the_subs():
    picks = substitute(make_picks(), 3, 13)
    predicted = dict.fromkeys(range(1, 16), 1.0) | {3: 5.0, 13: 2.0}
    review = prediction_review(predicted, picks, POINTS)
    assert review["xi_predicted"] == pytest.approx(10 * 1.0 + 5.0 + 1.0)
    # the deadline XI scored: 3 played 0, captain 8 counted twice
    assert review["xi_scored"] == 2 * 9 + 17 + 0 + 2 * -1


def test_captain_review_is_zero_when_the_captain_was_the_best_starter():
    assert captain_review(make_picks(captain=5), POINTS)["lost"] == 0


def test_prediction_review_counts_the_captain_twice_and_lists_misses():
    predicted = dict.fromkeys(range(1, 16), 4.0) | {8: 6.6, 99: 1.0}
    review = prediction_review(predicted, make_picks(), POINTS)

    assert review["xi_predicted"] == pytest.approx(10 * 4.0 + 2 * 6.6)
    assert review["misses"] == [(5, 4.0, 17), (8, 6.6, -1), (1, 4.0, 2)]
    # 99 has a prediction but no live entry: not scored
    assert review["n"] == 15
    assert review["n_played"] == 14
    assert review["mae"] == pytest.approx((13 + 7.6 + 4 + 5 + 11 * 2) / 15)


def test_prediction_review_missing_prediction_counts_as_zero():
    review = prediction_review({}, make_picks(), POINTS)
    assert review["xi_predicted"] == 0
    assert review["mae"] is None
    assert review["mae_played"] is None


def test_season_review_nets_hits_and_sums_only_played_gameweeks():
    history = {
        "current": [
            {"event": 2, "points": 60, "event_transfers_cost": 4, "points_on_bench": 3},
            {"event": 3, "points": 40, "event_transfers_cost": 0, "points_on_bench": 5},
        ]
    }
    events = [
        {"id": 1, "average_entry_score": 50},
        {"id": 2, "average_entry_score": 70},
        {"id": 3, "average_entry_score": 45},
    ]
    assert season_review(history, events, [0, 18]) == {
        "points": 96,
        "average": 115,
        "bench": 8,
        "captain_lost": 18,
    }


def test_format_review_escapes_names_and_handles_missing_predictions():
    entry = {
        "points": 38,
        "event_transfers_cost": 4,
        "overall_rank": 5196546,
        "points_on_bench": 2,
    }
    captain = captain_review(make_picks(), POINTS)
    season = {"points": 450, "average": 461, "bench": 40, "captain_lost": 30}
    names = {5: "Gabriel", 8: "<Palmer>"}
    text = format_review(
        8,
        entry,
        4426110,
        {"average_entry_score": 45, "highest_score": 120},
        captain,
        None,
        season,
        names,
    )
    assert "Points: 34 (incl. -4 hit) | average 45 | top 120" in text
    assert "Rank: 5,196,546 (was 4,426,110)" in text
    assert "Captain: &lt;Palmer&gt; -1 | best starter Gabriel 17 | lost 18" in text
    assert "Predictions: none found for this gameweek" in text
    assert "Season: 450 vs average 461 (-11) | captain lost 30 | bench 40" in text


def test_format_review_with_predictions():
    entry = {
        "points": 34,
        "event_transfers_cost": 0,
        "overall_rank": 1,
        "points_on_bench": 2,
    }
    predicted = dict.fromkeys(range(1, 16), 4.0) | {8: 6.6}
    text = format_review(
        8,
        entry,
        None,
        {"average_entry_score": 45, "highest_score": 120},
        captain_review(make_picks(captain=5), POINTS),
        prediction_review(predicted, make_picks(), POINTS),
        {"points": 34, "average": 45, "bench": 2, "captain_lost": 0},
        {5: "Gabriel", 8: "Palmer"},
    )
    assert "hit" not in text
    assert "(was" not in text
    assert "lost" not in text.splitlines()[4]
    assert "Deadline XI: predicted 53.2 | scored 33 (34 after subs)" in text
    assert "Biggest misses: Gabriel 4.0➜17, Palmer 6.6➜-1, 1 4.0➜2" in text
    assert "who played" in text
