"""
Apply the suggested transfers, then the lineup, for the Pi automation.

Usage: post_transfers.py --team-id T --posted PATH --applied PATH

Does what `airsenal apply transfers --yes` does, with the run markers tied to
the transfer POST itself:
  --posted   created immediately before the transfer POST is sent
  --applied  created as soon as that POST has succeeded, or when there is
             nothing to POST

No --posted file therefore means no POST was ever attempted (a retry is safe);
--posted without --applied means the outcome is unknown (never re-POST).
Exits nonzero on any failure; the markers say how far it got.
"""

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


def with_markers(
    post: Callable[[Any, dict[str, Any]], None], posted: Path, applied: Path
) -> Callable[[Any, dict[str, Any]], None]:
    """`post`, with `posted` written right before it and `applied` once it returns."""

    def wrapped(fetcher: Any, payload: dict[str, Any]) -> None:
        # post_transfers logs in first; without a session no request can be
        # sent, so fail before .posted exists and leave the retry open
        fetcher.login()
        if not fetcher.logged_in:
            msg = "FPL login failed - no transfer POST was sent"
            raise RuntimeError(msg)
        # v2 logs the payload only at debug level; keep the chip field auditable
        print(f"transfer payload: {json.dumps(payload)}", flush=True)
        posted.touch()
        post(fetcher, payload)
        applied.touch()

    return wrapped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-id", type=int, required=True)
    parser.add_argument("--posted", type=Path, required=True)
    parser.add_argument("--applied", type=Path, required=True)
    args = parser.parse_args()

    from airsenal.apply.lineup import set_lineup
    from airsenal.apply.transfers import make_transfers
    from airsenal.core.logging import configure_logging
    from airsenal.remote.fpl_api import FPLDataFetcher

    configure_logging()
    # patched on the class: make_transfers posts through whichever fetcher
    # instance it resolves, and post_transfers is its only transfer POST
    FPLDataFetcher.post_transfers = with_markers(  # type: ignore[method-assign]
        FPLDataFetcher.post_transfers, args.posted, args.applied
    )
    if make_transfers(args.team_id, skip_check=True) is None:
        # nothing to POST: the squad already matches the suggestions
        args.applied.touch()
    set_lineup(args.team_id, skip_check=True)


if __name__ == "__main__":
    main()
