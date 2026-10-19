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
