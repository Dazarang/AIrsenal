"""
test that we get valid responses from the API.
"""

import json
import random
import re

import pytest
from curl_cffi import requests as cffi_requests

from airsenal.framework.data_fetcher import (
    LOGIN_ATTEMPTS,
    FPLDataFetcher,
    FPLLoginError,
)
from airsenal.framework.utils import NEXT_GAMEWEEK


def test_instantiate_fetchers():
    """
    check we can instantiate the classes
    """
    fpl = FPLDataFetcher()
    assert fpl


def test_get_summary_data():
    """
    get summary of all players' data for this season.
    """
    fetcher = FPLDataFetcher()
    data = fetcher.get_current_summary_data()
    assert isinstance(data, dict)
    assert len(data) > 0


@pytest.mark.skipif(NEXT_GAMEWEEK == 1, reason="No team data before start of season")
def test_get_team_data():
    """
    should give current list of players in our team
    """
    fetcher = FPLDataFetcher()
    data = fetcher.get_fpl_team_data(1)["picks"]
    assert isinstance(data, list)
    assert len(data) == 15


@pytest.mark.skipif(NEXT_GAMEWEEK == 1, reason="No team data before start of season")
def test_get_team_history_data():
    """
    gameweek history for our team id
    """
    fetcher = FPLDataFetcher()
    data = fetcher.get_fpl_team_history_data()
    assert isinstance(data, dict)
    assert len(data) > 0


def test_get_event_data():
    """
    gameweek list with deadlines and status
    """
    fetcher = FPLDataFetcher()
    data = fetcher.get_event_data()
    assert isinstance(data, dict)
    assert len(data) > 0


def test_get_player_summary_data():
    """
    summary for individual players
    """
    fetcher = FPLDataFetcher()
    data = fetcher.get_player_summary_data()
    assert isinstance(data, dict)
    assert len(data) > 0


def test_get_current_team_data():
    """
    summary for current teams
    """
    fetcher = FPLDataFetcher()
    data = fetcher.get_current_team_data()
    assert isinstance(data, dict)
    assert len(data) > 0


@pytest.mark.skipif(NEXT_GAMEWEEK == 1, reason="No data yet for gameweek 1")
def test_get_fpl_team_data_gw1():
    """
    which players are in our squad for gw1
    """
    fetcher = FPLDataFetcher()
    data = fetcher.get_fpl_team_data(1)
    assert isinstance(data, dict)
    assert "picks" in data
    players = [p["element"] for p in data["picks"]]
    assert len(players) == 15


@pytest.mark.skipif(NEXT_GAMEWEEK == 1, reason="No data yet for gameweek 1")
def test_get_fpl_team_data_gw1_different_fpl_team_ids():
    """
    which players are in a couple of different squads for gw 1
    """
    fetcher = FPLDataFetcher()
    # assume that fpl_team_ids < 100 will all have squads for
    # gameweek 1, and that they will be different..
    team_id_1 = random.randint(1, 50)
    team_id_2 = random.randint(51, 100)
    data_1 = fetcher.get_fpl_team_data(1, fpl_team_id=team_id_1)
    players_1 = [p["element"] for p in data_1["picks"]]
    assert len(players_1) == 15
    data_2 = fetcher.get_fpl_team_data(1, fpl_team_id=team_id_2)
    players_2 = [p["element"] for p in data_2["picks"]]
    assert len(players_2) == 15
    # check they are different
    assert sorted(players_1) != sorted(players_2)


@pytest.mark.skipif(NEXT_GAMEWEEK == 1, reason="No data yet for gameweek 1")
def test_get_detailed_player_data():
    """
    for player_id=1, list of gameweek data
    """
    fetcher = FPLDataFetcher()

    data = fetcher.get_gameweek_data_for_player(1)
    assert isinstance(data, dict)
    assert len(data) > 0


def test_login_retries_transient_failures(monkeypatch):
    fetcher = FPLDataFetcher()
    fetcher.FPL_LOGIN, fetcher.FPL_PASSWORD = "user", "pass"
    attempts = []

    def flaky_login():
        attempts.append(1)
        if len(attempts) < LOGIN_ATTEMPTS:
            msg = "transient"
            raise FPLLoginError(msg)

    monkeypatch.setattr(fetcher, "_try_login", flaky_login)
    monkeypatch.setattr("airsenal.framework.data_fetcher.time.sleep", lambda _s: None)
    with pytest.warns(UserWarning, match="retrying"):
        fetcher.login()
    assert fetcher.logged_in is True
    assert fetcher.login_failed is False
    assert len(attempts) == LOGIN_ATTEMPTS


def test_login_gives_up_after_max_attempts(monkeypatch):
    fetcher = FPLDataFetcher()
    fetcher.FPL_LOGIN, fetcher.FPL_PASSWORD = "user", "pass"
    attempts = []

    def always_fails():
        attempts.append(1)
        msg = "Failed to extract access token."
        raise FPLLoginError(msg)

    monkeypatch.setattr(fetcher, "_try_login", always_fails)
    monkeypatch.setattr("airsenal.framework.data_fetcher.time.sleep", lambda _s: None)
    with pytest.warns(UserWarning, match="Failed to extract access token"):
        fetcher.login()
    assert fetcher.logged_in is False
    assert fetcher.login_failed is True
    assert len(attempts) == LOGIN_ATTEMPTS
    # latched: no further attempts
    with pytest.warns(UserWarning, match="previously failed"):
        fetcher.login()
    assert len(attempts) == LOGIN_ATTEMPTS


# --- login flow, driven through a scripted fake session --------------------


class FakeResponse:
    def __init__(self, text="", json_data=None, headers=None, status_code=200):
        self.text = text
        self._json = json_data
        self.headers = headers or {}
        self.status_code = status_code
        self.content = json.dumps(json_data).encode() if json_data is not None else b""

    def json(self):
        if self._json is None:
            msg = "not json"
            raise json.JSONDecodeError(msg, "", 0)
        return self._json


AUTH_HTML = (
    '... "accessToken":"TOKEN1" ... <input type="hidden" name="state" value="STATE1"/>'
)


def login_script():
    """One scripted response per request of a successful login flow."""
    return [
        FakeResponse(text=AUTH_HTML),  # 1 GET authorize
        FakeResponse(json_data={"interactionId": "I1", "id": "R1"}),  # 2 start
        FakeResponse(json_data={"id": "R2"}),  # 3 login post 1
        FakeResponse(json_data={"id": "R3", "connectionId": "C1"}),  # 4 login post 2
        FakeResponse(json_data={"dvResponse": "DV"}),  # 5 login post 3
        FakeResponse(
            headers={
                "Location": "https://fantasy.premierleague.com/?code=CODE1&state=S"
            }
        ),  # 6 resume
        FakeResponse(json_data={"access_token": "AT1"}),  # 7 token
        FakeResponse(json_data={"player": {"id": 1}}),  # 8 /me/
    ]


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        return self.script.pop(0)

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)


def make_fetcher(script):
    fetcher = FPLDataFetcher(rsession=FakeSession(script))
    fetcher.FPL_LOGIN, fetcher.FPL_PASSWORD = "user", "pass"
    return fetcher


def test_try_login_success_sets_header_only_after_me_check():
    fetcher = make_fetcher(login_script())
    fetcher._try_login()
    assert fetcher.headers == {"X-API-Authorization": "Bearer AT1"}
    calls = fetcher.rsession.calls
    assert [m for m, _, _ in calls] == ["GET"] + ["POST"] * 6 + ["GET"]
    assert calls[3][2]["json"]["parameters"]["username"] == "user"
    assert calls[5][2]["data"] == {"dvResponse": "DV", "state": "STATE1"}
    assert calls[6][2]["data"]["code"] == "CODE1"
    assert calls[7][2]["headers"] == {"X-API-Authorization": "Bearer AT1"}


@pytest.mark.parametrize(
    ("step", "broken", "message"),
    [
        (
            0,
            FakeResponse(text="<html>no token</html>"),
            "Failed to extract access token.",
        ),
        (0, FakeResponse(text='"accessToken":"T"'), "Failed to extract state."),
        (1, FakeResponse(json_data={}), "Failed to extract interaction ID."),
        (1, FakeResponse(text="garbage"), "Failed to extract interaction ID."),
        (2, FakeResponse(json_data={}), "Interaction Post 1 Failed (id generation)"),
        (
            3,
            FakeResponse(json_data={"id": "R3"}),
            "Interaction Post 2 Failed (connectionID generation)",
        ),
        (
            4,
            FakeResponse(json_data=[]),
            "Interaction Post 3 Failed (dvResponse generation)",
        ),
        (5, FakeResponse(headers={}), "Failed to extract auth code."),
        (6, FakeResponse(json_data={"error": "x"}), "Failed to retrieve access token."),
        (
            7,
            FakeResponse(json_data={"detail": "anonymous"}),
            "All login steps succeeded but team data retrieval failed.",
        ),
    ],
)
def test_try_login_raises_at_every_failure_point(step, broken, message):
    script = login_script()
    script[step] = broken
    fetcher = make_fetcher(script)
    with pytest.raises(FPLLoginError, match=re.escape(message)):
        fetcher._try_login()
    assert fetcher.logged_in is False


def test_login_retries_network_errors_then_succeeds(monkeypatch):
    fetcher = make_fetcher(login_script())
    real_try_login = fetcher._try_login
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            msg = "boom"
            raise cffi_requests.exceptions.ConnectionError(msg)
        return real_try_login()

    monkeypatch.setattr(fetcher, "_try_login", flaky)
    monkeypatch.setattr("airsenal.framework.data_fetcher.time.sleep", lambda _s: None)
    with pytest.warns(UserWarning, match="retrying"):
        fetcher.login()
    assert fetcher.logged_in is True
    assert len(attempts) == 2
