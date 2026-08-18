from types import SimpleNamespace

import pytest

from airsenal.scripts.make_transfers import build_transfer_payload

FETCHER = SimpleNamespace(FPL_TEAM_ID=123)


def test_build_transfer_payload_no_chip():
    payload = build_transfer_payload([], 5, FETCHER, None)
    assert payload["wildcard"] is False
    assert payload["freehit"] is False
    assert payload["entry"] == 123
    assert payload["event"] == 5


@pytest.mark.parametrize(
    ("chip", "key"), [("wildcard", "wildcard"), ("free_hit", "freehit")]
)
def test_build_transfer_payload_api_chips(chip, key):
    payload = build_transfer_payload([], 5, FETCHER, chip)
    assert payload[key] is True


@pytest.mark.parametrize("chip", ["triple_captain", "bench_boost"])
def test_build_transfer_payload_manual_chips_not_in_payload(chip):
    payload = build_transfer_payload([], 5, FETCHER, chip)
    assert "triplecaptain" not in payload
    assert "benchboost" not in payload
    assert payload["wildcard"] is False
    assert payload["freehit"] is False
