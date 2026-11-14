"""Offline tests for the extra alerts."""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extras  # noqa: E402

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


def P(pid, name, value, fteam=None, status="ACTIVE", season=None, l15=None, mpg=30, slot="UT", ret=None, team="BOS"):
    return {"id": pid, "name": name, "nba_team": team, "positions": ["SF"], "value": value,
            "fantasy_team": fteam, "status": status, "season_avg": season if season is not None else value,
            "last15": l15 if l15 is not None else value, "mpg": mpg, "slot": slot, "return_date": ret}


def g(hours_ago, fp, mins=30):
    return {"date": NOW - timedelta(hours=hours_ago), "fp": fp, "min": mins}


def test_returning_free_agent():
    old = {"1": P("1", "Back Star", 34, status="OUT"), "2": P("2", "Back Scrub", 12, status="OUT"),
           "3": P("3", "Owned", 40, "Lucky", status="OUT")}
    new = {"1": P("1", "Back Star", 34), "2": P("2", "Back Scrub", 12), "3": P("3", "Owned", 40, "Lucky")}
    out = extras.returning_fa_alerts(old, new, worst_value=24)
    assert len(out) == 1 and "Back Star" in out[0][1]


def test_cold_player_crossing_with_minutes_drop():
    players = {"m": P("m", "Mine", 35, "King Coopa", season=35, mpg=34)}
    games = {"m": [g(10, 18, 26), g(34, 20, 27), g(58, 22, 28), g(82, 38, 34)]}
    best = P("f", "Best FA", 26)
    out = extras.cold_alerts(games, players, NOW, best)
    assert len(out) == 1
    msg = out[0][1]
    assert "Mine esfriando" in msg and "Minutos caindo" in msg and "Best FA" in msg
    # already cold last time: no repeat
    games = {"m": [g(10, 18), g(34, 20), g(58, 22), g(82, 15)]}
    assert extras.cold_alerts(games, players, NOW, best) == []


def test_trade_ideas():
    players = {
        "a": P("a", "Hot Mine", 30, "King Coopa", season=28, l15=36),
        "b": P("b", "Slump Star", 34, "Lucky", season=40, l15=30),
        "c": P("c", "Hurt Star", 0, "Bull Market", status="OUT", season=45, ret="2026-11-30"),
        "d": P("d", "Fine Other", 35, "Lucky", season=35, l15=36),
    }
    msg = extras.trade_ideas(players, date(2026, 11, 20))
    assert "Hot Mine" in msg and "Slump Star" in msg and "Hurt Star" in msg and "Fine Other" not in msg
    assert extras.trade_ideas(players, date(2027, 2, 1)) is None   # after the deadline


def test_playoff_race():
    teams = [{"name": f"T{i}", "wins": 10 - i, "losses": i, "ties": 0, "points_for": 5000 - 100 * i, "standing": i + 1}
             for i in range(14)]
    teams[9]["name"] = "King Coopa"
    sched = {"King Coopa": [f"T{i}" for i in range(15)]}
    msg = extras.playoff_race(teams, sched, current_matchup=5, n_regular=15)
    assert "Você: *10º*" in msg and "Distância para o 8º" in msg and "— linha dos playoffs —" in msg


def test_league_trade_lopsided_and_skips_mine():
    players = {"1": P("1", "Star", 45, "Lucky"), "2": P("2", "Bench", 18, "Bull Market")}
    act = [{"date": 2000, "team": "Bull Market", "action": "TRADED", "player": "Star"},
           {"date": 2000, "team": "Lucky", "action": "TRADED", "player": "Bench"},
           {"date": 3000, "team": "King Coopa", "action": "TRADED", "player": "Bench"}]
    out = extras.league_trade_alerts(act, players, since_ms=1000)
    assert len(out) == 1 and "Desequilibrada" in out[0][1]


def test_recap():
    msg = extras.recap_message(4, "CCCP", 1380, 1360, [("A", 200), ("B", 90)], {"A": 150, "B": 120})
    assert "Vitória" in msg and "+20" in msg and "Acima do esperado: *A* (+50)" in msg
