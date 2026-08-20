"""
Transfer-suggestion inspection and server-side verification for the Pi
automation.

Subcommands:
  snapshot                              print latest TransferSuggestion
                                        timestamp (or "none")
  check --gw N --team-id T --since TS   JSON: {new_rows, n_transfers, n_out,
                                        chip, transfers_out, transfers_in,
                                        bank, text}
  telegram ...                          HTML summary for the Telegram report
  picks-elements --team-id T            JSON list of the element ids currently
                                        picked (logged-in my-team view)
  verify --gw N --team-id T --chip C    print "<transfers> <chip_status>":
                                        transfers = ok | mismatch | unknown
                                        (do the current picks contain every
                                        suggested IN and none of the OUTs?),
                                        chip_status = confirmed | absent |
                                        unknown (is chip C active?), or -
                                        when C is none

Only the result goes to stdout; everything the framework prints is sent to
stderr so callers can parse stdout.
"""

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gw_context import CHIPS, active_chip_name


def latest_timestamp() -> str:
    from sqlalchemy import select

    from airsenal.framework.schema import TransferSuggestion
    from airsenal.framework.utils import session

    ts = session.scalars(
        select(TransferSuggestion.timestamp).order_by(
            TransferSuggestion.timestamp.desc()
        )
    ).first()
    return ts or "none"


def suggestion_rows(gw: int, team_id: int) -> list:
    from airsenal.framework.utils import CURRENT_SEASON, session
    from airsenal.scripts.get_transfer_suggestions import get_transfer_suggestions

    return get_transfer_suggestions(
        session, gameweek=gw, season=CURRENT_SEASON, fpl_team_id=team_id
    )


def my_team(team_id: int) -> dict | None:
    """Logged-in my-team view (picks, chips, transfers), or None on failure."""
    from airsenal.framework.utils import fetcher

    try:
        return fetcher.get_current_squad_data(team_id)
    except Exception as e:
        print(f"my-team fetch failed: {e}", file=sys.stderr)
        return None


def current_picks(team_id: int) -> list[dict]:
    data = my_team(team_id)
    return sorted(data["picks"], key=lambda p: p["position"]) if data else []


def cmd_snapshot(_args) -> str:
    return latest_timestamp()


def cmd_check(args) -> str:
    from airsenal.framework.utils import get_bank, get_player_name

    new_rows = latest_timestamp() != args.since
    rows = suggestion_rows(args.gw, args.team_id)
    players_out = [get_player_name(r.player_id) for r in rows if r.in_or_out < 0]
    players_in = [get_player_name(r.player_id) for r in rows if r.in_or_out >= 0]
    chip = rows[0].chip_played if rows else None
    try:
        bank = get_bank(fpl_team_id=args.team_id) / 10
    except Exception:
        bank = None

    if players_out:
        n_transfers = len(players_out)
        text = (
            f"transfers ({n_transfers}): OUT {', '.join(players_out)} -> "
            f"IN {', '.join(players_in)}"
        )
    elif players_in:
        n_transfers = len(players_in)
        text = f"initial squad ({n_transfers} picks): {', '.join(players_in)}"
    else:
        n_transfers = 0
        text = "no transfers suggested"
    text += f"\nchip: {chip or 'none'}"
    if bank is not None:
        text += f" | bank: {bank:.1f}"

    return json.dumps(
        {
            "new_rows": new_rows,
            "n_transfers": n_transfers,
            "n_out": len(players_out),
            "chip": chip,
            "transfers_out": players_out,
            "transfers_in": players_in,
            "bank": None if bank is None else f"{bank:.1f}",
            "text": text,
        }
    )


def cmd_telegram(args) -> str:
    """Build the HTML-formatted Telegram summary for a completed run."""
    import html as html_mod

    from airsenal.framework.utils import (
        CURRENT_SEASON,
        get_bank,
        get_player,
        get_player_from_api_id,
        get_player_name,
    )

    esc = html_mod.escape
    pos_emoji = {"GK": "\U0001f9e4", "DEF": "\U0001f6e1", "MID": "⚡", "FWD": "⚽"}
    lines = [f"\U0001f916 <b>AIrsenal GW{args.gw}</b> {esc(args.mode)}".rstrip()]

    rows = suggestion_rows(args.gw, args.team_id)
    outs = [str(get_player_name(r.player_id)) for r in rows if r.in_or_out < 0]
    ins = [str(get_player_name(r.player_id)) for r in rows if r.in_or_out >= 0]
    chip = rows[0].chip_played if rows else None

    picks = [] if args.dry_run else current_picks(args.team_id)

    if not outs and picks and args.pre_picks:
        # full-rebuild weeks (GW1, wildcard) have no OUT suggestion rows: the
        # real moves are the diff between the pre-apply and post-apply squads
        try:
            with open(args.pre_picks) as f:
                pre_elements = json.load(f)
        except Exception:
            pre_elements = []
        post_elements = [p["element"] for p in picks]
        outs = [
            str(get_player_from_api_id(e))
            for e in pre_elements
            if e not in post_elements
        ]
        ins = [
            str(get_player_from_api_id(e))
            for e in post_elements
            if e not in pre_elements
        ]

    def squad_section(grouped: dict[str, list[str]], bench: list[str]) -> None:
        for pos in ("GK", "DEF", "MID", "FWD"):
            if grouped.get(pos):
                lines.append(f"<b>{pos_emoji[pos]} {pos}</b>")
                lines.append(" | ".join(grouped[pos]))
        if bench:
            lines.append("<b>\U0001fa91 Bench</b>")
            lines.append(" | ".join(bench))

    lines.append("")
    if picks:

        def label(p: dict) -> tuple[str, str | None]:
            player = get_player_from_api_id(p["element"])
            name = esc(str(player)) if player else str(p["element"])
            if p.get("is_captain"):
                name += " (C)"
            elif p.get("is_vice_captain"):
                name += " (VC)"
            return name, player.position(CURRENT_SEASON) if player else None

        grouped: dict[str, list[str]] = {}
        for name, pos in (label(p) for p in picks[:11]):
            grouped.setdefault(pos, []).append(name)
        squad_section(grouped, [label(p)[0] for p in picks[11:]])
    elif ins:
        # dry run / picks unavailable: show the suggested squad instead
        grouped = {}
        for r in rows:
            if r.in_or_out >= 0 and (player := get_player(r.player_id)):
                grouped.setdefault(player.position(CURRENT_SEASON), []).append(
                    esc(player.name)
                )
        squad_section(grouped, [])

    if outs:
        lines.append("")
        lines.append(f"<b>\U0001f504 Transfers ({len(outs)})</b>")
        lines.extend(
            f"OUT {esc(o)} ➜ IN {esc(i)}" for o, i in zip(outs, ins, strict=False)
        )

    try:
        bank = f"{get_bank(fpl_team_id=args.team_id) / 10:.1f}"
    except Exception:
        bank = "?"
    if args.bank_before and args.bank_before != bank:
        bank = f"{args.bank_before} ➜ {bank}"
    lines.append("")
    # the orchestrator's decision wins: a chip strategy with zero transfers
    # writes no suggestion rows, so the rows alone would say "none"
    chip_line = f"\U0001f0cf chip: {args.chip or chip or 'none'}"
    if args.chip_note:
        chip_line += f" {esc(args.chip_note)}"
    lines.append(chip_line)
    lines.append(f"\U0001f4b0 bank: {bank}")
    if args.pred:
        lines.append(f"\U0001f4c8 pred: {esc(args.pred)}")
    return "\n".join(lines)


def cmd_picks_elements(args) -> str:
    return json.dumps([p["element"] for p in current_picks(args.team_id)])


def transfers_applied(picks: list[dict], ins: set[int], outs: set[int]) -> bool:
    """Do the current picks contain every suggested IN and none of the OUTs?"""
    elements = {p["element"] for p in picks}
    return ins <= elements and not (outs & elements)


def cmd_verify(args) -> str:
    from airsenal.framework.utils import get_player

    data = my_team(args.team_id)
    if data is None:
        return "unknown unknown"

    api_ids: dict[int, set[int]] = {1: set(), -1: set()}
    for r in suggestion_rows(args.gw, args.team_id):
        if player := get_player(r.player_id):
            api_ids[1 if r.in_or_out >= 0 else -1].add(player.fpl_api_id)
    if not api_ids[1]:
        transfers = "unknown"  # nothing suggested, nothing to compare against
    elif transfers_applied(data["picks"], api_ids[1], api_ids[-1]):
        transfers = "ok"
    else:
        transfers = "mismatch"

    chip_status = "-"
    if args.chip in CHIPS:
        chip_status = "confirmed" if active_chip_name(data) == args.chip else "absent"
    return f"{transfers} {chip_status}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("snapshot")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("check")
    p.add_argument("--gw", type=int, required=True)
    p.add_argument("--team-id", type=int, required=True)
    p.add_argument("--since", required=True)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("telegram")
    p.add_argument("--gw", type=int, required=True)
    p.add_argument("--team-id", type=int, required=True)
    p.add_argument("--mode", default="")
    p.add_argument("--pred", default="")
    p.add_argument("--bank-before", default="")
    p.add_argument("--pre-picks", default="", help="JSON file of pre-apply element ids")
    p.add_argument("--chip-note", default="", help="appended to the chip line")
    p.add_argument("--chip", default="", help="the run's chip decision")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_telegram)

    p = sub.add_parser("picks-elements")
    p.add_argument("--team-id", type=int, required=True)
    p.set_defaults(func=cmd_picks_elements)

    p = sub.add_parser("verify")
    p.add_argument("--gw", type=int, required=True)
    p.add_argument("--team-id", type=int, required=True)
    p.add_argument("--chip", default="none")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    # the framework prints diagnostics (DB fallbacks, lookups) to stdout;
    # keep them off the result channel
    with contextlib.redirect_stdout(sys.stderr):
        output = args.func(args)
    if output is not None:
        print(output)


if __name__ == "__main__":
    main()
