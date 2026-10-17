"""The login flow is retried from scratch on a transient failure."""

import pytest
from curl_cffi import requests

from airsenal.remote import fpl_auth
from airsenal.remote.errors import RemoteConnectionError
from airsenal.remote.fpl_auth import LOGIN_ATTEMPTS, FPLAuth, _LoginStepError


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(fpl_auth.time, "sleep", lambda _s: None)
    auth = FPLAuth(session=object())
    auth.FPL_LOGIN, auth.FPL_PASSWORD = "user", "pass"
    return auth


def scripted(monkeypatch, auth, failures):
    """Make each login pass raise the next of `failures`, then succeed."""
    attempts = []

    def flow():
        attempts.append(1)
        if len(attempts) <= len(failures):
            raise failures[len(attempts) - 1]

    monkeypatch.setattr(auth, "_login_flow", flow)
    return attempts


def test_a_failed_step_is_retried_until_it_succeeds(monkeypatch, auth):
    attempts = scripted(monkeypatch, auth, [_LoginStepError("transient")] * 2)
    auth.login()
    assert auth.logged_in is True
    assert auth.login_failed is False
    assert len(attempts) == LOGIN_ATTEMPTS


def test_a_network_error_is_retried(monkeypatch, auth):
    attempts = scripted(
        monkeypatch, auth, [requests.exceptions.ConnectionError("boom")]
    )
    auth.login()
    assert auth.logged_in is True
    assert len(attempts) == 2


def test_every_attempt_failing_latches_the_failure(monkeypatch, auth):
    attempts = scripted(monkeypatch, auth, [_LoginStepError("no token")] * 9)
    auth.login()
    assert auth.logged_in is False
    assert auth.login_failed is True
    assert len(attempts) == LOGIN_ATTEMPTS
    auth.login()
    assert len(attempts) == LOGIN_ATTEMPTS


def test_a_network_error_on_the_last_attempt_is_a_remote_error(monkeypatch, auth):
    scripted(monkeypatch, auth, [requests.exceptions.ConnectionError("boom")] * 9)
    with pytest.raises(RemoteConnectionError):
        auth.login()
    assert auth.logged_in is False
    # remembered: a second call does not run the three sign-ons again
    auth.login()
    assert auth.login_failed is True


def test_a_session_passed_in_is_kept_between_attempts(monkeypatch, auth):
    session = auth.session
    scripted(monkeypatch, auth, [_LoginStepError("transient")])
    auth.login()
    assert auth.session is session
