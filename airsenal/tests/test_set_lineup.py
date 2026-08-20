from airsenal.framework.data_fetcher import FPLDataFetcher
from airsenal.scripts.set_lineup import active_team_chip


def chip(name, status, chip_type="team"):
    return {"name": name, "status_for_entry": status, "chip_type": chip_type}


def test_active_team_chip_is_kept_with_the_lineup():
    my_team = {"chips": [chip("bboost", "available"), chip("3xc", "active")]}
    assert active_team_chip(my_team) == "3xc"


def test_post_lineup_sends_the_chip_with_the_picks(monkeypatch):
    fetcher = FPLDataFetcher(fpl_team_id=1)
    fetcher.logged_in = True
    sent: list[dict] = []
    monkeypatch.setattr(
        fetcher, "_post_data", lambda _url, data, **_kw: sent.append(data)
    )
    fetcher.post_lineup([{"element": 5}], chip="3xc")
    fetcher.post_lineup([{"element": 5}])
    assert sent == [
        {"chip": "3xc", "picks": [{"element": 5}]},
        {"chip": None, "picks": [{"element": 5}]},
    ]


def test_no_active_team_chip():
    assert active_team_chip({"chips": [chip("bboost", "available")]}) is None
    assert active_team_chip({"chips": []}) is None
    # transfer chips are not controlled by the lineup request (the FPL client
    # only ever sends the team chip here)
    assert active_team_chip({"chips": [chip("wildcard", "active", "transfer")]}) is None
