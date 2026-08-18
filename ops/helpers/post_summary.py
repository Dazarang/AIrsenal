"""
Transfer-suggestion inspection and server-side verification for the Pi
automation.

Subcommands:
  snapshot                              print latest TransferSuggestion
                                        timestamp (or "none")
  check --gw N --team-id T --since TS   JSON: {new_rows, n_transfers, chip,
                                        transfers, text}
  transfer-count --gw N --team-id T     print count of applied transfers for
                                        the GW from the public API (-1 if the
                                        API call fails)
  verify --gw N --team-id T --expected K --pre-count M --chip C
                                        exit 0 iff the applied transfers are
                                        visible server-side; JSON on stdout
                                        includes chip_status for C
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gw_context import API_CHIP_NAMES


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


def cmd_snapshot(_args) -> None:
    print(latest_timestamp())


def cmd_check(args) -> None:
    from airsenal.framework.utils import (
        CURRENT_SEASON,
        get_bank,
        get_player_name,
        session,
    )
    from airsenal.scripts.get_transfer_suggestions import get_transfer_suggestions

    new_rows = latest_timestamp() != args.since
    rows = get_transfer_suggestions(
        session, gameweek=args.gw, season=CURRENT_SEASON, fpl_team_id=args.team_id
    )
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

    json.dump(
        {
            "new_rows": new_rows,
            "n_transfers": n_transfers,
            "chip": chip,
            "transfers_out": players_out,
            "transfers_in": players_in,
            "text": text,
        },
        sys.stdout,
    )


def applied_transfer_count(team_id: int, gw: int) -> int:
    from airsenal.framework.utils import fetcher

    transfers = fetcher.get_fpl_transfer_data(team_id)
    return sum(1 for t in transfers if t.get("event") == gw)


def cmd_transfer_count(args) -> None:
    try:
        print(applied_transfer_count(args.team_id, args.gw))
    except Exception as e:
        print(f"transfer-count failed: {e}", file=sys.stderr)
        print(-1)


def cmd_verify(args) -> None:
    from airsenal.framework.utils import fetcher

    result: dict = {}
    if args.gw == 1:
        # pre-season squad changes are not listed by the transfers endpoint
        result["transfers_ok"] = True
        result["note"] = "GW1: transfer endpoint not applicable pre-season"
    elif args.pre_count < 0:
        result["transfers_ok"] = False
        result["note"] = "pre-count unavailable, cannot verify"
    else:
        try:
            post = applied_transfer_count(args.team_id, args.gw)
        except Exception as e:
            post = -1
            result["note"] = f"post-count failed: {e}"
        result["post_count"] = post
        result["transfers_ok"] = post >= args.pre_count + args.expected

    chip_status = ""
    if args.chip in ("wildcard", "free_hit"):
        try:
            hist = fetcher.get_fpl_team_history_data(args.team_id)
            played = {
                (API_CHIP_NAMES.get(c["name"], c["name"]), c["event"])
                for c in hist.get("chips", [])
            }
            chip_status = "confirmed" if (args.chip, args.gw) in played else "absent"
        except Exception:
            chip_status = "unknown"
    result["chip_status"] = chip_status

    json.dump(result, sys.stdout)
    sys.exit(0 if result["transfers_ok"] else 1)


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

    p = sub.add_parser("transfer-count")
    p.add_argument("--gw", type=int, required=True)
    p.add_argument("--team-id", type=int, required=True)
    p.set_defaults(func=cmd_transfer_count)

    p = sub.add_parser("verify")
    p.add_argument("--gw", type=int, required=True)
    p.add_argument("--team-id", type=int, required=True)
    p.add_argument("--expected", type=int, required=True)
    p.add_argument("--pre-count", type=int, required=True)
    p.add_argument("--chip", default="none")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
