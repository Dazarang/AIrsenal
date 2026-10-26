"""The sample of minutes the default minutes model is built from."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from airsenal.core.caching import clear_query_caches
from airsenal.db.models import (
    Base,
    Fixture,
    Player,
    PlayerAttributes,
    PlayerScore,
    Result,
)
from airsenal.prediction.minutes import (
    get_previous_season_minutes,
    get_recent_minutes_for_player,
)

PREVIOUS_SEASON = "2425"
SEASON = "2526"
OLD_TEAM = "AAA"
NEW_TEAM = "BBB"


@pytest.fixture
def dbsession():
    # gameweek lookups are cached on their arguments, not on the session
    clear_query_caches()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    clear_query_caches()


def _player(dbsession: Session) -> Player:
    """A player at NEW_TEAM this season."""
    player = Player(player_id=1, fpl_api_id=1, name="Signing")
    dbsession.add(player)
    dbsession.add(
        PlayerAttributes(
            player=player,
            season=SEASON,
            gameweek=1,
            price=50,
            team=NEW_TEAM,
            position="MID",
        )
    )
    return player


def _play(
    dbsession: Session,
    minutes: int,
    player: Player,
    team: str,
    gameweek: int,
    season: str,
) -> PlayerScore:
    """One played fixture, and the player's minutes in it."""
    fixture = Fixture(
        gameweek=gameweek,
        home_team=team,
        away_team="ZZZ",
        season=season,
        tag="test",
    )
    result = Result(fixture=fixture, home_score=1, away_score=0)
    dbsession.add_all([fixture, result])
    dbsession.flush()
    score = PlayerScore(
        player=player,
        player_team=team,
        opponent="ZZZ",
        points=2,
        goals=0,
        assists=0,
        bonus=0,
        conceded=0,
        minutes=minutes,
        fixture=fixture,
        result=result,
    )
    dbsession.add(score)
    return score


def _unplayed(dbsession: Session, gameweek: int) -> None:
    """A fixture still to come, which marks where this season's results end."""
    dbsession.add(
        Fixture(
            gameweek=gameweek,
            home_team=NEW_TEAM,
            away_team="ZZZ",
            season=SEASON,
            tag="test",
        )
    )


def _recent(dbsession: Session, player: Player, last_gameweek: int) -> list[float]:
    dbsession.commit()
    return get_recent_minutes_for_player(
        player,
        n_matches_to_use=3,
        season=SEASON,
        last_gameweek=last_gameweek,
        dbsession=dbsession,
    )


def test_a_summer_signing_keeps_the_minutes_from_their_old_club(dbsession):
    """At gameweek 1 the sample is last season's matches, at whatever club."""
    player = _player(dbsession)
    for gameweek, minutes in enumerate([10, 90, 0, 75], start=1):
        _play(dbsession, minutes, player, OLD_TEAM, gameweek, PREVIOUS_SEASON)
    _unplayed(dbsession, 1)
    # most recent first, and only as many as were asked for
    assert _recent(dbsession, player, last_gameweek=0) == [75.0, 0.0, 90.0]


def test_a_short_sample_is_filled_with_the_previous_season_mean(dbsession):
    player = _player(dbsession)
    for gameweek, minutes in enumerate([30, 90], start=1):
        _play(dbsession, minutes, player, OLD_TEAM, gameweek, PREVIOUS_SEASON)
    _play(dbsession, 90, player, NEW_TEAM, 1, SEASON)
    _unplayed(dbsession, 2)
    assert _recent(dbsession, player, last_gameweek=1) == [90.0, 60.0]


def test_a_short_sample_with_no_previous_season_is_filled_with_zero(dbsession):
    """A newcomer is not assumed to keep their place on one appearance."""
    player = _player(dbsession)
    _play(dbsession, 90, player, NEW_TEAM, 1, SEASON)
    _unplayed(dbsession, 2)
    assert _recent(dbsession, player, last_gameweek=1) == [90.0, 0.0]


def test_a_player_with_no_history_plays_no_minutes(dbsession):
    player = _player(dbsession)
    _unplayed(dbsession, 1)
    assert _recent(dbsession, player, last_gameweek=0) == [0.0]


def test_a_full_sample_ignores_the_previous_season(dbsession):
    player = _player(dbsession)
    _play(dbsession, 0, player, OLD_TEAM, 1, PREVIOUS_SEASON)
    for gameweek, minutes in enumerate([90, 80, 70], start=1):
        _play(dbsession, minutes, player, NEW_TEAM, gameweek, SEASON)
    _unplayed(dbsession, 4)
    assert _recent(dbsession, player, last_gameweek=3) == [70.0, 80.0, 90.0]


def test_previous_season_minutes_leave_out_matches_the_player_was_unavailable_for(
    dbsession,
):
    player = _player(dbsession)
    _play(dbsession, 90, player, OLD_TEAM, 1, PREVIOUS_SEASON)
    injured = _play(dbsession, 0, player, OLD_TEAM, 2, PREVIOUS_SEASON)
    injured.chance_of_playing = 0
    dbsession.commit()
    minutes = get_previous_season_minutes(player, season=SEASON, dbsession=dbsession)
    assert minutes == [90.0]
