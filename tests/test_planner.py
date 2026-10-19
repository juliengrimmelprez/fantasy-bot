"""Offline tests for week planning (streaming plan, lineup check, IR, late scratch)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
import planner  # noqa: E402

ET = timezone(timedelta(hours=-4))
MON = datetime(2026, 10, 26, 19, 30, tzinfo=ET)   # a Monday, 7:30 pm tips


def tip(per):          # period 1 = Monday ... 7 = Sunday
    return int((MON + timedelta(days=per - 1)).timestamp() * 1000)


def P(pid, name, team, value, fteam="King Coopa", slot="UT", status="ACTIVE", pos=("SF",), ret=None):
    return {"id": pid, "name": name, "nba_team": team, "value": value, "fantasy_team": fteam,
            "slot": slot, "status": status, "positions": list(pos), "return_date": ret, "mpg": 28}


def world():
    sched = {}
    for i in range(9):                                   # 9 core teams play every day
        sched[f"T{i}"] = {d: tip(d) for d in range(1, 8)}
    sched["CAM"] = {2: tip(2), 6: tip(6)}                # Camara's team
    sched["YYY"] = {2: tip(2), 3: tip(3)}
    sched["ZZZ"] = {5: tip(5)}
    sched["AAA"] = {6: tip(6), 7: tip(7)}
    sched["BAD"] = {4: tip(4)}
    roster = [P(f"c{i}", f"Core{i}", f"T{i}", 35) for i in range(9)]
    roster.append(P("w", "Camara", "CAM", 23.8, slot="BE"))
    roster.append(P("ir", "Hurt", "T0", 40, slot="IR", status="OUT"))
    fas = [P("y", "YYY", "YYY", 26, None, slot=""), P("z", "ZZZ", "ZZZ", 27, None, slot=""),
           P("a", "AAA", "AAA", 25, None, slot=""), P("b", "Bad", "BAD", 10, None, slot="")]
    periods = list(range(1, 8))
    pdates = planner.period_dates(sched, periods)
    return roster, fas, periods, sched, pdates


def test_streaming_plan_rotates_through_free_agents():
    roster, fas, periods, sched, pdates = world()
    now_ms = tip(1) - 10 * 3600 * 1000
    plan = planner.streaming_plan(roster, fas, periods, sched, pdates, adds_left=10, now_ms=now_ms)
    assert plan["worst"]["name"] == "Camara" and plan["worst_games"] == 2
    steps = [(s["period"], s["drop"]["name"], s["add"]["name"], s["games"]) for s in plan["slots"][0]["steps"]]
    assert steps == [(2, "Camara", "YYY", [2, 3]), (5, "YYY", "ZZZ", [5]), (6, "ZZZ", "AAA", [6, 7])]
    assert plan["slots"][0]["gain"] == round(26 * 2 + 27 + 25 * 2 - 23.8 * 2, 1)
    text = planner.render_plan(plan, pdates, 10, "t")
    assert "dropar Camara → pegar *YYY*" in text and "Ter 27" in text


def test_days_with_ten_playing_dont_count():
    roster, fas, periods, sched, pdates = world()
    sched["X10"] = {6: tip(6), 7: tip(7)}
    roster.append(P("x", "Tenth", "X10", 35))
    pdates = planner.period_dates(sched, periods)
    plan = planner.streaming_plan(roster, fas, periods, sched, pdates, adds_left=10, now_ms=tip(1) - 3600_000)
    adds = [s["add"]["name"] for s in plan["slots"][0]["steps"]]
    assert "AAA" not in adds          # AAA only plays when the lineup is already full


def test_never_drops_30_plus_and_respects_adds():
    roster, fas, periods, sched, pdates = world()
    roster[-2]["value"] = 31          # Camara now a keeper
    plan = planner.streaming_plan(roster, fas, periods, sched, pdates, adds_left=10, now_ms=tip(1) - 3600_000)
    assert plan["slots"] == []
    roster, fas, periods, sched, pdates = world()
    plan = planner.streaming_plan(roster, fas, periods, sched, pdates, adds_left=1, now_ms=tip(1) - 3600_000)
    assert plan["adds_used"] == 1


def test_ir_player_never_plays():
    roster, fas, periods, sched, pdates = world()
    ir = next(p for p in roster if p["slot"] == "IR")
    assert not any(planner.plays(ir, d, sched, pdates) for d in periods)


def test_lineup_issues_today():
    roster, fas, periods, sched, pdates = world()
    roster[0]["status"] = "OUT"                               # a starter is out today
    roster[-2]["slot"] = "BE"                                 # Camara on bench, no game Monday
    sched["BEN"] = {1: tip(1)}
    roster.append(P("bn", "BenchGuy", "BEN", 20, slot="BE"))
    issues = planner.lineup_issues(roster, 1, sched, planner.period_dates(sched, periods), tip(1) - 3600_000)
    assert "Core0" in issues[0] and "BenchGuy" in issues[0]


def test_late_scratch_and_ir_hints():
    roster, fas, periods, sched, pdates = world()
    sched["BEN"] = {1: tip(1)}
    roster.append(P("bn", "BenchGuy", "BEN", 20, slot="BE"))
    roster = [p for p in roster if p["slot"] != "IR"]          # IR slot free
    old = {p["id"]: dict(p) for p in roster}
    new = {p["id"]: dict(p) for p in roster}
    new["c0"]["status"] = "OUT"
    ctx = {"today": 1, "sched": sched, "now_ms": tip(1) - 3600_000, "pdates": planner.period_dates(sched, periods)}
    msg = bot.injury_alerts(old, new, ctx)[0][1]
    assert "troque por *BenchGuy*" in msg and "vaga de IR está livre" in msg


def test_healthy_player_stuck_in_ir():
    roster, fas, periods, sched, pdates = world()
    old = {p["id"]: dict(p) for p in roster}
    new = {p["id"]: dict(p) for p in roster}
    new["ir"]["status"] = "ACTIVE"
    msg = bot.injury_alerts(old, new)[0][1]
    assert "saudável no IR" in msg


def test_projected_remaining_top10_per_day():
    roster, fas, periods, sched, pdates = world()
    pts, games = planner.projected_remaining(roster, [1], sched, pdates, tip(1) - 3600_000)
    assert games == 9 and pts == 9 * 35


def test_week_days_mapping():
    # opening night on a Tuesday: week 1 = Tue..Sun, week 2 = next Mon..Sun
    tue = datetime(2026, 10, 20, 19, 30, tzinfo=ET)
    sched = {"A": {i + 1: int((tue + timedelta(days=i)).timestamp() * 1000) for i in range(14)}}
    assert planner.week_days(sched, [1]) == [1, 2, 3, 4, 5, 6]
    assert planner.week_days(sched, [2]) == [7, 8, 9, 10, 11, 12, 13]
