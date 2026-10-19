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


def test_actionable_uses_projected_value_with_extra_minutes():
    # Backup Center is 18 FP, below a worst of 20, but with the starter's
    # minutes he projects higher, so the alert goes out ("middle ground").
    old = league(**{"5": P("5", "My Worst", "MIA", 20, "King Coopa", pos=("PG",))})
    new = dict(old)
    new["1"] = P("1", "Star Center", "DET", 40, "Lucky", status="OUT")
    alerts = bot.injury_alerts(old, new)
    assert len(alerts) == 1 and "Backup Center* (C, 18 → ~" in alerts[0][1]
    # Even with the boost he wouldn't beat a 35 FP worst player: silent.
    old["5"] = new["5"] = P("5", "My Worst", "MIA", 35, "King Coopa", pos=("PG",))
    assert bot.injury_alerts(old, new) == []


def test_position_weighting_with_cascade():
    team = {
        "out": {**P("out", "Star PG", "BOS", 40, "Lucky", pos=("PG",)), "mpg": 34},
        "pg2": {**P("pg2", "Backup PG", "BOS", 18, None, pos=("PG",)), "mpg": 18},
        "sg2": {**P("sg2", "Backup SG", "BOS", 18, None, pos=("SG",)), "mpg": 18},
        "sf2": {**P("sf2", "Backup SF", "BOS", 18, None, pos=("SF",)), "mpg": 18},
        "c2":  {**P("c2", "Backup C", "BOS", 18, None, pos=("C",)), "mpg": 18},
    }
    b = bot.minutes_boost(team["out"], team)
    assert b["pg2"]["extra_min"] > b["sg2"]["extra_min"] > b["sf2"]["extra_min"] > b["c2"]["extra_min"]
    assert b["c2"]["extra_min"] >= 0 and b["sf2"]["extra_min"] > 0   # cascade: farther still gets a bit
    best = bot.beneficiaries(team["out"], team)[0]
    assert best["name"] == "Backup PG"


def test_minutes_cap():
    team = {
        "out": {**P("out", "Star C", "BOS", 40, "Lucky"), "mpg": 34},
        "c2": {**P("c2", "Big Min C", "BOS", 30, None), "mpg": 33},
    }
    assert bot.minutes_boost(team["out"], team)["c2"]["extra_min"] <= 3   # MAX_MPG 36


def _g(hours_ago, fp, mins=30, now=datetime(2026, 11, 5, 12, tzinfo=timezone.utc)):
    return {"date": now - timedelta(hours=hours_ago), "fp": fp, "min": mins}


def test_hot_moving_average_crossing():
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    players = league()
    games = {
        "8": [_g(10, 42), _g(34, 26), _g(58, 24), _g(82, 10)],   # avg3 30.7, before 20 → crosses
        "2": [_g(10, 31), _g(34, 33), _g(58, 35)],              # avg3 33, before 34 → already above
        "3": [_g(10, 45), _g(34, 12), _g(58, 10)],              # one big game, avg3 22 → no
        "7": [_g(10, 50), _g(34, 50)],                           # rostered
    }
    out = dict(bot.hot_alerts(games, players, now))
    assert list(k.split(":")[1] for k in out) == ["8"]
    assert "Média dos últimos 3: *30.7 FP*" in out[next(iter(out))]


def test_hot_needs_two_games():
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    assert bot.hot_alerts({"8": [_g(10, 50)]}, league(), now) == []


def test_hot_ignores_unfinished_and_stale_games():
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    players = league()
    assert bot.hot_alerts({"8": [_g(1, 90), _g(25, 10), _g(49, 10)]}, players, now) == []   # 90 FP game still in progress: ignored
    assert bot.hot_alerts({"8": [_g(60, 40), _g(84, 40)]}, players, now) == []  # too old


def test_timeline_month_names_and_headline_first():
    label, days = bot.timeline_from_news([
        "Butler (knee) could return sometime between January and February",
        "ACL tears usually take nine months to recover from",
    ])
    assert label.startswith("jan/") and days is not None


def test_hourly_digest_consolidates_and_dedups():
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    players = league()
    games = {
        "8": [_g(10, 34), _g(34, 31)],   # 2 in a row → 🔥2
        "2": [_g(5, 48)],                # single big game
        "3": [_g(5, 22)],                # below 30, left out
        "7": [_g(5, 50)],                # rostered, left out
        "4": [_g(1, 40)],                # still playing, left out
    }
    msg, keys = bot.digest_message(games, players, now, set())
    assert msg.splitlines()[1].startswith("• *Backup Center*")    # sorted by FP
    assert "média 32.5 🔥" in msg and "Bench Guard" not in msg and "Role Wing" not in msg
    assert len(keys) == 2
    assert bot.digest_message(games, players, now, set(keys)) == (None, [])   # no repeats


def test_single_big_game_can_lift_average():
    # 60 + 10 = avg 35 over 2 games: the average decides, not the streak.
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    assert len(bot.hot_alerts({"2": [_g(10, 60), _g(34, 10)]}, league(), now)) == 1
