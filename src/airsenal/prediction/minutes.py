"""Estimating how many minutes a player will play."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from airsenal.db.models import Fixture, Player, PlayerScore
from airsenal.db.queries.scores import get_recent_playerscore_rows, was_available
from airsenal.db.session import get_session
from airsenal.game.season import CURRENT_SEASON, get_previous_season


def get_previous_season_minutes(
    player: Player,
    n_matches_to_use: int = 10,
    season: str = CURRENT_SEASON,
    dbsession: Session | None = None,
) -> list[float]:
    """
    Minutes in the player's last `n_matches_to_use` matches of the previous season.

    Only the matches the player was available for, at any Premier League team,
    so a player signed from another club in the summer keeps their history.
    Most recent first; empty if the player has none.
    """
    dbsession = get_session(dbsession)
    query = (
        select(PlayerScore.minutes)
        .join(Fixture, PlayerScore.fixture)
        .where(
            PlayerScore.player_id == player.player_id,
            Fixture.season == get_previous_season(season),
            was_available(),
        )
    )
    return [
        float(minutes)
        for minutes in dbsession.scalars(
            # the date breaks the tie between the two matches of a double gameweek
            query.order_by(Fixture.gameweek.desc(), Fixture.date.desc()).limit(
                n_matches_to_use
            )
        )
    ]


def get_recent_minutes_for_player(
    player: Player,
    n_matches_to_use: int = 3,
    season: str = CURRENT_SEASON,
    *,
    last_gameweek: int,
    dbsession: Session | None = None,
) -> list[float]:
    """
    Minutes played in each of the last `n_matches_to_use` matches.

    Only the matches the player was available for, for the team they are at now.
    `last_gameweek` is inclusive.

    A player with fewer appearances than that is filled in from the previous
    season. With no appearances at all, the sample is every match that
    `get_previous_season_minutes` reads, so a gameweek 1 prediction is a spread
    rather than a single value; with some, one value is added: the mean of the
    previous season, or 0 if they did not play in it.
    """
    dbsession = get_session(dbsession)
    playerscores = get_recent_playerscore_rows(
        player,
        n_matches_to_use,
        season,
        last_gameweek,
        exclude_unavailable=True,
        current_team_only=True,
        dbsession=dbsession,
    )
    minutes = [float(r.minutes) for r in playerscores]
    if len(minutes) >= n_matches_to_use:
        return minutes

    previous = get_previous_season_minutes(player, season=season, dbsession=dbsession)
    # A weakness of the previous season's last matches is increased rotation at the
    # end of a season, when teams don't have anything to play for.
    if not previous:
        return [*minutes, 0.0]
    if not minutes:
        return previous
    return [*minutes, sum(previous) / len(previous)]


def is_absent(
    player: Player, root_gameweek: int, fixture_gameweek: int, season: str
) -> bool:
    """
    Whether this player is known to be unavailable for a gameweek.

    Injured or suspended as at `root_gameweek` and not expected back by
    `fixture_gameweek`. The attributes table carries those flags for every season,
    from the FPL API for the current one and from the per-day attributes history
    and the scraped absences for a past one.

    A minutes model that ignores this predicts minutes for players who are not
    going to play.
    """
    return player.is_injured_or_suspended(season, root_gameweek, fixture_gameweek)
