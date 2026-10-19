"""Offline tests of the alert rules with a fake league. Run: python -m pytest -q"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


def P(pid, name, team, value, fteam=None, status="ACTIVE", pos=("C",), slot=""):
    return {"id": pid, "name": name, "nba_team": team, "status": status,
            "positions": list(pos), "fantasy_team": fteam, "slot": slot, "value": value}


def league(**over):
    base = {
        "1": P("1", "Star Center", "DET", 40, "Lucky"),
        "2": P("2", "Backup Center", "DET", 18, None),
        "3": P("3", "Bench Guard", "DET", 16, None, pos=("PG",)),
        "4": P("4", "My Guy", "BOS", 35, "King Coopa", pos=("SF",)),
        "5": P("5", "My Worst", "MIA", 14, "King Coopa", pos=("PG",)),
        "6": P("6", "My IR", "MIA", 5, "King Coopa", slot="IR"),
        "7": P("7", "Role Wing", "LAL", 22, "Bull Market", pos=("SF",)),
        "8": P("8", "Free Scrub", "DET", 8, None),
    }
    base.update(over)
    return base


def test_star_out_suggests_same_position_free_agent_first():
    old = league()
    new = league(**{"1": P("1", "Star Center", "DET", 40, "Lucky", status="OUT")})
    alerts = bot.injury_alerts(old, new)
    assert len(alerts) == 1
    msg = alerts[0][1]
    assert "ADD: *Backup Center*" in msg            # same position ranks first
    assert "Bench Guard" in msg                      # alternative
    assert "Free Scrub" not in msg                   # below min value
    assert "Drop sugerido: *My Worst*" in msg        # IR player ignored


def test_star_out_without_free_beneficiary_is_silent():
    old = league(**{"2": P("2", "Backup Center", "DET", 18, "Bull Market"),
                    "3": P("3", "Bench Guard", "DET", 16, "Lucky", pos=("PG",))})
    new = dict(old)
    new["1"] = P("1", "Star Center", "DET", 40, "Lucky", status="OUT")
    assert bot.injury_alerts(old, new) == []


def test_my_player_any_status_change_alerts():
    old = league()
    new = league(**{"4": P("4", "My Guy", "BOS", 35, "King Coopa", status="DAY_TO_DAY", pos=("SF",))})
    alerts = bot.injury_alerts(old, new)
    assert len(alerts) == 1 and "Seu jogador: My Guy" in alerts[0][1]


def test_minor_player_out_is_ignored():
    old = league()
    new = league(**{"7": P("7", "Role Wing", "LAL", 22, "Bull Market", status="OUT", pos=("SF",))})
    assert bot.injury_alerts(old, new) == []


def test_drop_alert_only_when_better_than_my_worst():
    new = league(**{"7": P("7", "Role Wing", "LAL", 22, None, pos=("SF",))})
    act = [{"date": 2000, "team": "Bull Market", "action": "DROPPED", "player": "Role Wing"},
           {"date": 2000, "team": "Lucky", "action": "DROPPED", "player": "Free Scrub"}]
    alerts = bot.drop_alerts(new, act, since_ms=1000)
    assert len(alerts) == 1 and "dropou Role Wing" in alerts[0][1]
    assert bot.drop_alerts(new, act, since_ms=2000) == []   # already seen


def test_nba_trade_alert_and_old_team_beneficiary():
    old = league()
    new = league(**{"1": P("1", "Star Center", "SAC", 40, "Lucky")})
    alerts = bot.trade_alerts(old, new)
    assert len(alerts) == 1
    assert "vai para *SAC*" in alerts[0][1] and "Backup Center" in alerts[0][1]


def test_dedup_via_sent_keys():
    old = league()
    new = league(**{"1": P("1", "Star Center", "DET", 40, "Lucky", status="OUT")})
    state = {"players": old, "sent_keys": ["inj:1:OUT"], "last_activity_ms": 0}
    assert bot.compute_alerts(state, {"players": new, "activity": []}) == []


def test_player_value_blend():
    assert bot.player_value(30, 26, 0) == 28.0
    assert bot.player_value(0, 26, 0) == 26.0
    assert bot.player_value(20, 20, 40) == 30.0     # hot streak counts half


def test_telegram_html_formatting():
    out = bot.to_telegram_html("🚨 *A & B <x>* ok *C*")
    assert out == "🚨 <b>A &amp; B &lt;x&gt;</b> ok <b>C</b>"


# --- v1.1: return dates, actionability, hot free agents ---------------------
from datetime import date, datetime, timedelta, timezone  # noqa: E402


def test_timeline_from_news():
    assert bot.timeline_from_news(["Claxton (hamstring) will be re-evaluated in two weeks"]) == ("~2 semanas (pelas notícias)", 14)
    assert bot.timeline_from_news(["Porzingis remains out indefinitely"]) == ("indefinido", None)
    assert bot.timeline_from_news(["He will miss the rest of the season"])[1] == 180
    assert bot.timeline_from_news(["Scored 20 points"]) is None


def test_return_line_long_absence_flag():
    p = {"return_date": "2026-11-20"}
    line = bot.return_line(p, today=date(2026, 10, 19))
    assert "20/11" in line and "Ausência longa" in line
    short = bot.return_line({"return_date": "2026-10-23"}, today=date(2026, 10, 19))
    assert "4 dias" in short and "longa" not in short
    assert bot.return_line({}) == "Volta: sem previsão"


def test_star_alert_suppressed_when_replacement_worse_than_my_worst():
    old = league(**{"5": P("5", "My Worst", "MIA", 20, "King Coopa", pos=("PG",))})
    new = dict(old)
    new["1"] = P("1", "Star Center", "DET", 40, "Lucky", status="OUT")
    assert bot.injury_alerts(old, new) == []          # best free option 18 < 20
    new["5"] = old["5"] = P("5", "My Worst", "MIA", 17, "King Coopa", pos=("PG",))
    assert len(bot.injury_alerts(old, new)) == 1      # 18 > 17: worth it


def _g(hours_ago, fp, mins=30, now=datetime(2026, 11, 5, 12, tzinfo=timezone.utc)):
    return {"date": now - timedelta(hours=hours_ago), "fp": fp, "min": mins}


def test_hot_streak_and_fluke():
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    players = league()
    games = {
        "8": [_g(10, 34), _g(34, 31), _g(58, 12)],    # Free Scrub: 2 good games in a row
        "2": [_g(10, 48), _g(34, 15)],                 # Backup Center: 1 huge game
        "3": [_g(10, 33), _g(34, 20)],                 # Bench Guard: 1 good game only
        "7": [_g(10, 50), _g(34, 50)],                 # Role Wing: rostered, ignored
    }
    out = dict(bot.hot_alerts(games, players, now))
    assert len(out) == 2
    streak = next(m for k, m in out.items() if k.startswith("hot:8:"))
    assert "2 jogos bons seguidos" in streak and "34" in streak
    fluke = next(m for k, m in out.items() if k.startswith("hot:2:"))
    assert "fluke" in fluke


def test_hot_ignores_unfinished_and_stale_games():
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    players = league()
    assert bot.hot_alerts({"8": [_g(1, 60), _g(25, 10)]}, players, now) == []   # still playing
    assert bot.hot_alerts({"8": [_g(60, 40), _g(84, 40)]}, players, now) == []  # too old
