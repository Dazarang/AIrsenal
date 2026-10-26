"""
Weekly review of a finished gameweek for the Pi automation.

Usage:
  gw_review.py last-checked             print the newest gameweek whose points
                                        are final (finished and data_checked),
                                        or nothing; reads bootstrap-static JSON
                                        on stdin
  gw_review.py report --gw N            print the Telegram HTML review of
                                        gameweek N and append its numbers to
                                        state/gw_reviews.jsonl

The review compares what the team scored with the gameweek average, the
captain with the best starter, and AIrsenal's predictions with the points
actually scored: for the starting XI, and for every player as a check on the
model as a whole. Predictions come from the run that prepared the gameweek:
the newest prediction tag whose first gameweek is N.

Only the result goes to stdout; everything airsenal prints is sent to
stderr so callers can parse stdout.
"""

import argparse
import contextlib
import html
import json
import os
import sys
from pathlib import Path

DATA_ROOT = Path(
    os.environ.get("AIRSENAL_DATA_ROOT", str(Path.home() / "airsenal-data"))
)
REVIEWS_FILE = DATA_ROOT / "state" / "gw_reviews.jsonl"
RUN_DIR = DATA_ROOT / "run"
N_MISSES = 3


def last_checked_gameweek(events: list[dict]) -> int | None:
    """The newest gameweek whose points are final, or None before the first one."""
    checked = [e["id"] for e in events if e.get("finished") and e.get("data_checked")]
    return max(checked) if checked else None


def season_of(events: list[dict]) -> str:
    """The season string ("2627") of bootstrap-static's events, from GW1's deadline."""
    start = int(events[0]["deadline_time"][:4])
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def live_points(live: dict) -> dict[int, tuple[int, int]]:
    """(points, minutes) by element id from an event/N/live response."""
    return {
        e["id"]: (e["stats"]["total_points"], e["stats"]["minutes"])
        for e in live["elements"]
    }


def counted_picks(picks: dict) -> list[tuple[int, int]]:
    """
    (element, multiplier) for each pick as set at the deadline.

    A finished gameweek's positions and multipliers already include the
    automatic substitutions: a starter who did not play sits on the bench with
    multiplier 0. The review judges the choices that were made, so it undoes
    the substitutions and gives the captain the multiplier back.
    """
    chip = picks.get("active_chip")
    captain_multiplier = 3 if chip == "3xc" else 2
    if chip == "bboost":
        elements = {p["element"] for p in picks["picks"]}
    else:
        elements = {p["element"] for p in picks["picks"] if p["position"] <= 11}
        for sub in picks.get("automatic_subs", []):
            elements.discard(sub["element_in"])
            elements.add(sub["element_out"])
    return [
        (p["element"], captain_multiplier if p["is_captain"] else 1)
        for p in sorted(picks["picks"], key=lambda p: p["position"])
        if p["element"] in elements
    ]


def captain_review(picks: dict, points: dict[int, tuple[int, int]]) -> dict:
    """
    The captain's points against the best starter's.

    `lost` is what captaining the best starter would have added over what the
    armband actually earned, which is the vice-captain's points when the
    captain did not play, and nothing when neither did.
    """
    counted = counted_picks(picks)
    captain, multiplier = next((e, m) for e, m in counted if m > 1)
    best = max((e for e, _ in counted), key=lambda e: points.get(e, (0, 0))[0])
    best_points = points.get(best, (0, 0))[0]
    armband = sum(
        points.get(p["element"], (0, 0))[0] * (p["multiplier"] - 1)
        for p in picks["picks"]
        if p["multiplier"] > 1
    )
    return {
        "captain": captain,
        "captain_points": points.get(captain, (0, 0))[0],
        "best": best,
        "best_points": best_points,
        "lost": best_points * (multiplier - 1) - armband,
    }


def prediction_review(
    predicted: dict[int, float],
    picks: dict,
    points: dict[int, tuple[int, int]],
) -> dict:
    """
    Predicted against actual points, for the XI and for every predicted player.

    `xi_predicted` uses the deadline multipliers (captain counted twice);
    `misses` are the starters furthest from their prediction, worst first.
    """
    counted = counted_picks(picks)
    xi_predicted = sum(predicted.get(e, 0.0) * m for e, m in counted)
    misses = sorted(
        ((e, predicted.get(e, 0.0), points.get(e, (0, 0))[0]) for e, _ in counted),
        key=lambda m: abs(m[2] - m[1]),
        reverse=True,
    )[:N_MISSES]
    errors = [abs(points[e][0] - p) for e, p in predicted.items() if e in points]
    played = [
        abs(points[e][0] - p)
        for e, p in predicted.items()
        if e in points and points[e][1] > 0
    ]
    return {
        "xi_predicted": xi_predicted,
        "xi_scored": sum(points.get(e, (0, 0))[0] * m for e, m in counted),
        "misses": misses,
        "mae": sum(errors) / len(errors) if errors else None,
        "n": len(errors),
        "mae_played": sum(played) / len(played) if played else None,
        "n_played": len(played),
    }


def season_review(history: dict, events: list[dict], captain_lost: list[int]) -> dict:
    """Season totals against the average manager's."""
    averages = {e["id"]: e.get("average_entry_score") or 0 for e in events}
    current = history["current"]
    return {
        "points": sum(h["points"] - h["event_transfers_cost"] for h in current),
        "average": sum(averages.get(h["event"], 0) for h in current),
        "bench": sum(h["points_on_bench"] for h in current),
        "captain_lost": sum(captain_lost),
    }


def format_review(
    gw: int,
    entry: dict,
    previous_rank: int | None,
    event: dict,
    captain: dict,
    predictions: dict | None,
    season: dict,
    names: dict[int, str],
) -> str:
    """The Telegram HTML message."""
    esc = html.escape

    def name(element: int) -> str:
        return esc(names.get(element, str(element)))

    hits = entry["event_transfers_cost"]
    lines = [
        f"\U0001f4ca <b>AIrsenal GW{gw} review</b>",
        "",
        f"Points: {entry['points'] - hits}"
        + (f" (incl. -{hits} hit)" if hits else "")
        + f" | average {event.get('average_entry_score')}"
        + f" | top {event.get('highest_score')}",
    ]
    rank = f"Rank: {entry['overall_rank']:,}"
    if previous_rank:
        rank += f" (was {previous_rank:,})"
    lines.append(rank)
    lines.append(
        f"Captain: {name(captain['captain'])} {captain['captain_points']}"
        f" | best starter {name(captain['best'])} {captain['best_points']}"
        + (f" | lost {captain['lost']}" if captain["lost"] else "")
    )
    lines.append(f"Bench: {entry['points_on_bench']} pts")

    lines.append("")
    if predictions is None:
        lines.append("Predictions: none found for this gameweek")
    else:
        lines.append(
            f"Deadline XI: predicted {predictions['xi_predicted']:.1f}"
            f" | scored {predictions['xi_scored']} ({entry['points']} after subs)"
        )
        lines.append(
            "Biggest misses: "
            + ", ".join(f"{name(e)} {p:.1f}➜{a}" for e, p, a in predictions["misses"])
        )
        if predictions["mae"] is not None:
            lines.append(
                f"Model error: {predictions['mae']:.2f} pts/player"
                f" ({predictions['n']} players);"
                f" {predictions['mae_played']:.2f} for the"
                f" {predictions['n_played']} who played"
            )

    lines.append("")
    lines.append(
        f"Season: {season['points']} vs average {season['average']}"
        f" ({season['points'] - season['average']:+d})"
        f" | captain lost {season['captain_lost']}"
        f" | bench {season['bench']}"
    )
    return "\n".join(lines)


def predictions_for_gameweek(
    gw: int, season: str, tag: str | None = None
) -> tuple[str, dict[int, float]] | None:
    """
    The tag and predicted points by FPL element id of the run that prepared `gw`.

    `tag` is the one the run recorded. Without it, the run is taken to be the
    newest prediction tag whose first gameweek is `gw`: a run's tag covers its
    next few gameweeks, and the run for the following gameweek starts one
    later. None when no such tag exists.
    """
    from sqlalchemy import func, select

    from airsenal.db.models import Fixture, PlayerPrediction
    from airsenal.db.queries.predictions import get_predictions_for_gameweeks
    from airsenal.db.session import get_session

    dbsession = get_session()
    tag = (
        tag
        or dbsession.scalars(
            select(PlayerPrediction.tag)
            .join(Fixture, PlayerPrediction.fixture_id == Fixture.fixture_id)
            .where(Fixture.season == season)
            .group_by(PlayerPrediction.tag)
            .having(func.min(Fixture.gameweek) == gw)
            .order_by(func.max(PlayerPrediction.id).desc())
            .limit(1)
        ).first()
    )
    if tag is None:
        return None
    predicted: dict[int, float] = {}
    for row in get_predictions_for_gameweeks(
        [gw], tag, season=season, dbsession=dbsession
    ):
        if row.player.fpl_api_id is not None:
            # a double gameweek has one row per fixture
            predicted[row.player.fpl_api_id] = (
                predicted.get(row.player.fpl_api_id, 0.0) + row.predicted_points
            )
    return tag, predicted


def cmd_last_checked(_args) -> str:
    gw = last_checked_gameweek(json.load(sys.stdin).get("events", []))
    return "" if gw is None else str(gw)


def cmd_report(args) -> str:
    from airsenal.remote.fpl_api import get_fetcher
    from airsenal.remote.fpl_http import API_HOME, get_json

    fetcher = get_fetcher()
    summary = fetcher.get_current_summary_data()
    events = summary["events"]
    names = {e["id"]: e["web_name"] for e in summary["elements"]}
    history = fetcher.get_fpl_team_history_data()
    by_gw = {h["event"]: h for h in history["current"]}
    entry = by_gw[args.gw]
    previous = by_gw.get(args.gw - 1)

    def gameweek_data(gw: int) -> tuple[dict, dict[int, tuple[int, int]]]:
        live = get_json(fetcher.auth.session, f"{API_HOME}/event/{gw}/live/")
        return fetcher.get_fpl_team_data(gw), live_points(live)

    # the season's captain choices, every played gameweek before this one
    captain_lost = [
        captain_review(*gameweek_data(gw))["lost"] for gw in by_gw if gw < args.gw
    ]
    picks, points = gameweek_data(args.gw)
    captain = captain_review(picks, points)
    captain_lost.append(captain["lost"])

    # the season of the gameweek reviewed, not today's: GW38 can be reviewed in June
    season_name = season_of(events)
    tag_file = RUN_DIR / f"gw{args.gw}.tag"
    recorded = tag_file.read_text().strip() if tag_file.exists() else None
    found = predictions_for_gameweek(args.gw, season_name, recorded)
    tag, predictions = (
        (None, None)
        if found is None
        else (found[0], prediction_review(found[1], picks, points))
    )
    season = season_review(
        {"current": [h for h in history["current"] if h["event"] <= args.gw]},
        events,
        captain_lost,
    )
    event = next(e for e in events if e["id"] == args.gw)

    message = format_review(
        args.gw,
        entry,
        previous["overall_rank"] if previous else None,
        event,
        captain,
        predictions,
        season,
        names,
    )
    # one line per review; a resent review adds another, the last one counts
    record = {
        "season": season_name,
        "gw": args.gw,
        "tag": tag,
        "points": entry["points"],
        "hits": entry["event_transfers_cost"],
        "average": event.get("average_entry_score"),
        "overall_rank": entry["overall_rank"],
        "bench": entry["points_on_bench"],
        "captain": captain,
        "predictions": predictions,
        "season_totals": season,
    }
    REVIEWS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REVIEWS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    return message


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("last-checked")
    p.set_defaults(func=cmd_last_checked)

    p = sub.add_parser("report")
    p.add_argument("--gw", type=int, required=True)
    p.set_defaults(func=cmd_report)

    args = parser.parse_args()
    with contextlib.redirect_stdout(sys.stderr):
        output = args.func(args)
    if output:
        print(output)


if __name__ == "__main__":
    main()
