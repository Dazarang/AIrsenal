"""
Gameweek context + chip state management for the Pi automation.

Subcommands (airsenal imports only where needed, so the light state
subcommands stay fast):
  assert-gw --gw N                      exit 3 if AIrsenal's NEXT_GAMEWEEK != N
  context --gw N --deadline-epoch E     emit the chip-research context JSON
  validate --context ctx.json           read decision JSON on stdin, print
                                        validated chip name or "none"
  record --gw N --season S --chip C --confidence X --reasoning R
  clear --gw N                          drop any unconfirmed decision for the GW
  confirm --gw N                        mark a recorded decision as confirmed
  reasoning --gw N                      print stored reasoning for the GW
  chip-for-gw --gw N                    print the recorded chip for the GW or none
  slack --gw N                          print "chips_left gws_left slack"
  active-chip                           chip active on the (logged-in) my-team
                                        view for the upcoming gameweek, or none

Only the result goes to stdout; everything the framework prints is sent to
stderr so callers can parse stdout.
"""

import argparse
import contextlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CHIPS = ["wildcard", "free_hit", "triple_captain", "bench_boost"]
API_CHIP_NAMES = {
    "wildcard": "wildcard",
    "freehit": "free_hit",
    "bboost": "bench_boost",
    "3xc": "triple_captain",
}
MIN_CONFIDENCE = 0.6
MIN_CONFIDENCE_URGENT = 0.5  # when chips are close to expiring (slack <= 2)


def confidence_threshold(slack: int | None) -> float:
    if slack is not None and slack <= 2:
        return MIN_CONFIDENCE_URGENT
    return MIN_CONFIDENCE


STATE_FILE = (
    Path(os.environ.get("AIRSENAL_DATA_ROOT", str(Path.home() / "airsenal-data")))
    / "state"
    / "chips_state.json"
)


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"entries": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def entry_for_gw(state: dict, gw: int) -> dict | None:
    """Newest non-lapsed entry for the gameweek (the state holds one season)."""
    return next(
        (
            e
            for e in reversed(state["entries"])
            if e.get("gw") == gw and not e.get("lapsed")
        ),
        None,
    )


def half_range(gw: int) -> tuple[int, int]:
    return (1, 19) if gw <= 19 else (20, 38)


def active_chip_name(my_team: dict) -> str | None:
    """Chip active for the upcoming gameweek in the my-team view, if any."""
    return next(
        (
            API_CHIP_NAMES.get(c["name"], c["name"])
            for c in my_team.get("chips", [])
            if c.get("status_for_entry") == "active"
        ),
        None,
    )


def reconcile_state(
    state: dict,
    gw: int,
    season: str,
    played: list[dict],
    active: str | None,
    available_now: set[str] | None = None,
) -> list[str]:
    """
    Bring past decisions in line with reality (mutates state, returns notes):
    previous seasons are dropped; decisions are confirmed when the chip shows
    up in the played history, or is active right now for this gameweek; past
    decisions absent from the history are lapsed (never activated, or
    activated and cancelled again before the deadline). A past decision that
    was already confirmed is only lapsed when the server's own chip statuses
    (available_now, from my-team) also show the chip as still available -
    never on the history endpoint alone.
    """
    notes: list[str] = []
    state["entries"] = [e for e in state["entries"] if e.get("season") == season]
    for e in state["entries"]:
        if e.get("lapsed"):
            continue
        if any(p["chip"] == e["chip"] and p["gw"] == e["gw"] for p in played):
            e["confirmed"] = True
        elif e["gw"] == gw:
            if e["chip"] == active:
                e["confirmed"] = True
        elif e["gw"] < gw and (
            not e.get("confirmed")
            or (available_now is not None and e["chip"] in available_now)
        ):
            e["lapsed"] = True
            e["confirmed"] = False
            notes.append(
                f"{e['chip']} was decided for GW{e['gw']} but never played - "
                "it is back in the available pool."
            )
    return notes


def clear_decision(state: dict, gw: int) -> None:
    """Drop any unconfirmed decision for the gameweek (mutates state)."""
    state["entries"] = [
        e for e in state["entries"] if e.get("gw") != gw or e.get("confirmed")
    ]


def record_decision(
    state: dict, gw: int, season: str, chip: str, confidence: float, reasoning: str
) -> None:
    """
    Replace any unconfirmed decision for the gameweek with this one (mutates
    state). A confirmed entry for the same chip is left alone; raises
    ValueError if a confirmed entry for a different chip exists.
    """
    confirmed = [
        e
        for e in state["entries"]
        if e.get("gw") == gw and e.get("season") == season and e.get("confirmed")
    ]
    if confirmed:
        if any(e["chip"] == chip for e in confirmed):
            return
        msg = f"refusing to overwrite a confirmed chip entry for GW{gw} {season}"
        raise ValueError(msg)
    clear_decision(state, gw)
    state["entries"].append(
        {
            "gw": gw,
            "season": season,
            "chip": chip,
            "confidence": confidence,
            "reasoning": reasoning,
            "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "confirmed": False,
        }
    )


def chip_stock(
    gw: int, season: str, played: list[dict], entries: list[dict]
) -> list[str]:
    """
    Chips still unused (and not reserved by a pending decision) in the half
    that contains gw. 2026/27 rules: ALL four chips reset at the half boundary
    - one full set per half, the first set dies at the GW19 deadline.
    """
    lo, hi = half_range(gw)
    used_this_half = {p["chip"] for p in played if lo <= p["gw"] <= hi}
    pending = {
        e["chip"]
        for e in entries
        if e.get("season") == season
        and lo <= e["gw"] <= hi
        and not e.get("lapsed")
        and (e["gw"] != gw or e.get("confirmed"))
    }
    return [c for c in CHIPS if c not in used_this_half and c not in pending]


def compute_available_chips(
    gw: int, season: str, played: list[dict], entries: list[dict]
) -> tuple[list[str], list[str]]:
    """
    Chips the pipeline may play THIS gameweek: the stock minus the per-week
    rules. Returns (available, notes).
    """
    notes: list[str] = []
    available = chip_stock(gw, season, played, entries)
    if gw == 1:
        # free_hit is banned in GW1 and the wildcard is pointless (pre-season
        # transfers are unlimited); the GW1 optimizer path builds the squad
        # from scratch and ignores every chip flag, so triple_captain /
        # bench_boost could not be optimized for either
        available = []
    if (
        gw == 20
        and "free_hit" in available
        and any(p["chip"] == "free_hit" and p["gw"] == 19 for p in played)
    ):
        available.remove("free_hit")
        notes.append("free_hit is blocked in GW20 because it was played in GW19.")
    if any(p["gw"] == gw for p in played):
        available = []
        notes.append(
            "a chip is already active for this gameweek - no further chip "
            "may be played."
        )
    return available, notes


def chips_played(history: dict) -> list[dict]:
    return [
        {"chip": API_CHIP_NAMES.get(c["name"], c["name"]), "gw": c["event"]}
        for c in history.get("chips", [])
    ]


def team_performance(history: dict, fetcher) -> dict:
    """Recent gameweek returns vs the global average - the wildcard signal."""
    averages = {
        e["id"]: e.get("average_entry_score")
        for e in fetcher.get_current_summary_data().get("events", [])
    }
    recent = history.get("current", [])[-5:]
    if not recent:
        return {"note": "season not started - no performance history yet"}
    perf = {
        "recent_gameweeks": [
            {
                "gw": h["event"],
                "points": h["points"],
                "global_average": averages.get(h["event"]),
                "overall_rank": h["overall_rank"],
                "points_on_bench": h.get("points_on_bench"),
            }
            for h in recent
        ],
        "total_points": recent[-1]["total_points"],
        "overall_rank": recent[-1]["overall_rank"],
    }
    if len(recent) >= 3:
        # positive = rank improved over the last 3 gameweeks
        perf["rank_change_last_3_gws"] = (
            recent[-3]["overall_rank"] - recent[-1]["overall_rank"]
        )
    return perf


def cmd_assert_gw(args) -> None:
    from airsenal.framework.utils import NEXT_GAMEWEEK

    if args.gw != NEXT_GAMEWEEK:
        print(
            f"GAMEWEEK MISMATCH: scheduler says GW{args.gw} but AIrsenal "
            f"NEXT_GAMEWEEK is {NEXT_GAMEWEEK}. Chip flags would be silently "
            "dropped - aborting.",
            file=sys.stderr,
        )
        sys.exit(3)


def cmd_context(args) -> str:
    from sqlalchemy import select

    from airsenal.framework.schema import Fixture, Team
    from airsenal.framework.utils import (
        CURRENT_SEASON,
        NEXT_GAMEWEEK,
        fetcher,
        get_bank,
        get_free_transfers,
        get_player,
        session,
    )

    gw = args.gw
    if gw != NEXT_GAMEWEEK:
        print(f"NEXT_GAMEWEEK={NEXT_GAMEWEEK} != {gw}", file=sys.stderr)
        sys.exit(3)
    team_id = int(os.environ["FPL_TEAM_ID"])
    notes: list[str] = []

    history = fetcher.get_fpl_team_history_data(team_id)
    played = chips_played(history)
    _, hi = half_range(gw)

    # the logged-in my-team view is the only pre-deadline source for the chip
    # that is active right now and for the server's own availability flags
    try:
        my_team_chips = fetcher.get_current_squad_data(team_id)["chips"]
    except Exception as e:
        my_team_chips = None
        notes.append(f"could not cross-check chips with the API ({type(e).__name__})")
    active = active_chip_name({"chips": my_team_chips or []})
    api_available = None
    if my_team_chips is not None:
        api_available = {
            API_CHIP_NAMES.get(c["name"], c["name"])
            for c in my_team_chips
            if c.get("status_for_entry") == "available"
        }

    state = load_state()
    notes.extend(
        reconcile_state(state, gw, CURRENT_SEASON, played, active, api_available)
    )
    save_state(state)

    available, avail_notes = compute_available_chips(
        gw, CURRENT_SEASON, played, state["entries"]
    )
    notes.extend(avail_notes)
    if available and active:
        available = []
        notes.append(
            f"{active} is already active for this gameweek - no further chip "
            "may be played."
        )
    elif available and api_available is not None:
        if dropped := sorted(set(available) - api_available):
            notes.append(f"excluded by server-side chip status: {', '.join(dropped)}")
        available = [c for c in available if c in api_available]

    # current squad with availability flags
    squad_info: list[dict] = []
    try:
        from airsenal.framework.optimization_utils import get_starting_squad

        squad = get_starting_squad(
            next_gw=gw, season=CURRENT_SEASON, fpl_team_id=team_id, use_api=True
        )
        summary = fetcher.get_player_summary_data()
        try:
            from airsenal.framework.utils import (
                get_latest_prediction_tag,
                get_predicted_points_for_player,
            )

            tag = get_latest_prediction_tag()
        except Exception:
            tag = None
        for p in squad.players:
            player = get_player(p.player_id)
            data = summary.get(player.fpl_api_id, {}) if player else {}
            predicted = None
            if tag and player:
                try:
                    predicted = round(
                        get_predicted_points_for_player(player, tag).get(gw, 0), 1
                    )
                except Exception:
                    predicted = None
            squad_info.append(
                {
                    "name": player.name if player else str(p.player_id),
                    "position": getattr(p, "position", None),
                    "team": getattr(p, "team", None),
                    "status": data.get("status"),
                    "news": data.get("news") or None,
                    "chance_next_round": data.get("chance_of_playing_next_round"),
                    "form": data.get("form"),
                    "total_points": data.get("total_points"),
                    "predicted_points_this_gw": predicted,
                }
            )
    except Exception as e:  # context is best-effort: research degrades gracefully
        notes.append(f"could not fetch current squad ({type(e).__name__}: {e})")

    try:
        performance = team_performance(history, fetcher)
    except Exception as e:
        performance = {}
        notes.append(f"could not build performance history ({type(e).__name__})")

    try:
        free_transfers = get_free_transfers(fpl_team_id=team_id, gameweek=gw)
    except Exception:
        free_transfers = None
    try:
        bank = get_bank(fpl_team_id=team_id) / 10
    except Exception:
        bank = None
    try:
        squad_value = (
            sum(p["selling_price"] for p in fetcher.get_current_picks(team_id)) / 10
        )
    except Exception:
        squad_value = None

    # per-team fixture counts for the next 6 gameweeks (blank/double detection)
    fixtures = session.scalars(
        select(Fixture).where(
            Fixture.season == CURRENT_SEASON,
            Fixture.gameweek >= gw,
            Fixture.gameweek < gw + 6,
        )
    ).all()
    counts: dict[int, Counter] = {}
    for f in fixtures:
        counts.setdefault(f.gameweek, Counter())
        counts[f.gameweek][f.home_team] += 1
        counts[f.gameweek][f.away_team] += 1
    # base team set from the Team table, not the fixture window - otherwise a
    # team blanking in every window gameweek is invisible near season end
    all_teams = set(
        session.scalars(select(Team.name).where(Team.season == CURRENT_SEASON)).all()
    ) or {t for c in counts.values() for t in c}
    fixture_info = {
        str(g): {
            "doubles": sorted(t for t, n in c.items() if n >= 2),
            "blanks": sorted(all_teams - set(c)),
        }
        for g, c in sorted(counts.items())
    }

    deadline = datetime.fromtimestamp(args.deadline_epoch, tz=timezone.utc)
    ctx = {
        "gameweek": gw,
        "season": CURRENT_SEASON,
        "season_name": f"20{CURRENT_SEASON[:2]}/{CURRENT_SEASON[2:]}",
        "deadline_utc": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "half": 1 if gw <= 19 else 2,
        "gameweeks_left_in_half": hi - gw + 1,
        "chips_available": available,
        "active_chip": active,
        "chips_played_this_season": played,
        "squad": squad_info,
        "performance": performance,
        "free_transfers": free_transfers,
        "bank": bank,
        "squad_value": squad_value,
        "fixtures_by_gameweek": fixture_info,
        "notes": notes,
    }
    return json.dumps(ctx, indent=2)


def cmd_validate(args) -> str:
    with open(args.context) as f:
        ctx = json.load(f)
    try:
        decision = json.load(sys.stdin)
    except json.JSONDecodeError:
        return "none"
    available = ctx.get("chips_available", [])
    slack = None
    if ctx.get("gameweeks_left_in_half") is not None and available:
        slack = ctx["gameweeks_left_in_half"] - len(available)
    chip = decision.get("chip")
    confidence = decision.get("confidence", 0)
    if (
        decision.get("play_chip") is True
        and chip in available
        and isinstance(confidence, (int, float))
        and confidence >= confidence_threshold(slack)
    ):
        return chip
    return "none"


def cmd_record(args) -> None:
    state = load_state()
    try:
        record_decision(
            state, args.gw, args.season, args.chip, args.confidence, args.reasoning
        )
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    save_state(state)


def cmd_clear(args) -> None:
    state = load_state()
    clear_decision(state, args.gw)
    save_state(state)


def cmd_confirm(args) -> None:
    state = load_state()
    if e := entry_for_gw(state, args.gw):
        e["confirmed"] = True
        save_state(state)


def cmd_reasoning(args) -> str:
    e = entry_for_gw(load_state(), args.gw)
    return e.get("reasoning", "") if e else ""


def cmd_chip_for_gw(args) -> str:
    e = entry_for_gw(load_state(), args.gw)
    return e["chip"] if e else "none"


def cmd_slack(args) -> str:
    """'chips_left gws_left slack' for the current half (public API only)."""
    from airsenal.framework.utils import CURRENT_SEASON, fetcher

    team_id = int(os.environ["FPL_TEAM_ID"])
    played = chips_played(fetcher.get_fpl_team_history_data(team_id))
    stock = chip_stock(args.gw, CURRENT_SEASON, played, load_state()["entries"])
    _, hi = half_range(args.gw)
    gws_left = hi - args.gw + 1
    return f"{len(stock)} {gws_left} {gws_left - len(stock)}"


def cmd_active_chip(_args) -> str:
    from airsenal.framework.utils import fetcher

    team_id = int(os.environ["FPL_TEAM_ID"])
    return active_chip_name(fetcher.get_current_squad_data(team_id)) or "none"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("assert-gw")
    p.add_argument("--gw", type=int, required=True)
    p.set_defaults(func=cmd_assert_gw)

    p = sub.add_parser("context")
    p.add_argument("--gw", type=int, required=True)
    p.add_argument("--deadline-epoch", type=int, required=True)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("validate")
    p.add_argument("--context", required=True)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("record")
    p.add_argument("--gw", type=int, required=True)
    p.add_argument("--season", required=True)
    p.add_argument("--chip", required=True, choices=CHIPS)
    p.add_argument("--confidence", type=float, default=0.0)
    p.add_argument("--reasoning", default="")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("clear")
    p.add_argument("--gw", type=int, required=True)
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("confirm")
    p.add_argument("--gw", type=int, required=True)
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("reasoning")
    p.add_argument("--gw", type=int, required=True)
    p.set_defaults(func=cmd_reasoning)

    p = sub.add_parser("chip-for-gw")
    p.add_argument("--gw", type=int, required=True)
    p.set_defaults(func=cmd_chip_for_gw)

    p = sub.add_parser("slack")
    p.add_argument("--gw", type=int, required=True)
    p.set_defaults(func=cmd_slack)

    p = sub.add_parser("active-chip")
    p.set_defaults(func=cmd_active_chip)

    args = parser.parse_args()
    # the framework prints diagnostics (DB fallbacks, lookups) to stdout;
    # keep them off the result channel
    with contextlib.redirect_stdout(sys.stderr):
        output = args.func(args)
    if output is not None:
        print(output)


if __name__ == "__main__":
    main()
