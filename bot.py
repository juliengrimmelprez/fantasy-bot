"""Fantasy basketball alert bot (v1): Telegram, with optional WhatsApp.

Each run:
  1. reads the league from ESPN (rosters, free agents, recent activity),
  2. compares it with the snapshot saved last run (state.json),
  3. sends a Telegram message (and WhatsApp via CallMeBot, if set) for anything worth acting on,
  4. saves the new snapshot.

Secrets come from environment variables (GitHub Secrets):
  ESPN_S2, ESPN_SWID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  (optional) CALLMEBOT_PHONE, CALLMEBOT_APIKEY
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

import config
import planner

STATE_FILE = Path(__file__).with_name("state.json")
MAX_SENT_KEYS = 500


# ---------------------------------------------------------------------------
# Value of a player, in fantasy points per game
# ---------------------------------------------------------------------------

def player_value(season_avg: float, projected_avg: float, last15_avg: float) -> float:
    """Blend of what he has done and what ESPN expects.

    Recent form (last 15 days) catches role changes; the projection keeps
    the number sensible early in the season when few games exist.
    """
    parts = [v for v in (season_avg, projected_avg, last15_avg) if v and v > 0]
    if not parts:
        return 0.0
    if last15_avg and last15_avg > 0:
        others = [v for v in (season_avg, projected_avg) if v and v > 0]
        base = sum(others) / len(others) if others else last15_avg
        return round(0.5 * last15_avg + 0.5 * base, 1)
    return round(sum(parts) / len(parts), 1)


# ---------------------------------------------------------------------------
# ESPN -> plain dicts (the only part that touches the espn_api library)
# ---------------------------------------------------------------------------

def _mpg(p) -> float | None:
    year = config.SEASON
    for key in (f"{year}_total", f"{year}_last_15", f"{year}_projected"):
        avg = (p.stats.get(key) or {}).get("avg") or {}
        m = avg.get("MIN") or avg.get("MPG")
        if m:
            return round(float(m), 1)
    return None


def _to_dict(p, fantasy_team: str | None, slot: str = "") -> dict:
    year = config.SEASON
    last15 = p.stats.get(f"{year}_last_15", {}).get("applied_avg", 0) or 0
    return {
        "id": str(p.playerId),
        "name": p.name,
        "nba_team": p.proTeam,
        "status": (p.injuryStatus or "ACTIVE").upper(),
        "positions": [s for s in p.eligibleSlots if s in ("PG", "SG", "SF", "PF", "C")],
        "fantasy_team": fantasy_team,
        "slot": slot or getattr(p, "lineupSlot", ""),
        "value": player_value(p.avg_points, p.projected_avg_points, last15),
        "mpg": _mpg(p),
        "return_date": p.expected_return_date.isoformat() if getattr(p, "expected_return_date", None) else None,
    }


def fetch_league_snapshot() -> dict:
    from espn_api.basketball import League

    league = League(
        league_id=config.LEAGUE_ID,
        year=config.SEASON,
        espn_s2=os.environ["ESPN_S2"],
        swid=os.environ["ESPN_SWID"],
    )

    players: dict[str, dict] = {}
    my_team_found = False
    for team in league.teams:
        if team.team_name.strip().lower() == config.MY_TEAM_NAME.lower():
            my_team_found = True
        for p in team.roster:
            players[str(p.playerId)] = _to_dict(p, team.team_name)

    if not my_team_found:
        names = ", ".join(t.team_name for t in league.teams)
        raise RuntimeError(f"Time '{config.MY_TEAM_NAME}' não encontrado. Times: {names}")

    for p in league.free_agents(size=config.FREE_AGENT_POOL):
        players.setdefault(str(p.playerId), _to_dict(p, None))

    activity = []
    for act in league.recent_activity(size=100):
        for (team, action, player_name, _pos) in act.actions:
            activity.append({
                "date": act.date,
                "team": getattr(team, "team_name", str(team)) if team else "",
                "action": action,
                "player": player_name if isinstance(player_name, str) else getattr(player_name, "name", str(player_name)),
            })

    from espn_api.basketball.constant import PRO_TEAM_MAP
    sched: dict[str, dict[int, int]] = {}
    for team_id, games in (getattr(league, "pro_schedule", {}) or {}).items():
        abbr = PRO_TEAM_MAP.get(team_id)
        if not abbr:
            continue
        for per, glist in games.items():
            if glist:
                sched.setdefault(abbr, {})[int(per)] = glist[0]["date"]

    opponent, my_score, opp_score = None, None, None
    me = next(t for t in league.teams if t.team_name.strip().lower() == config.MY_TEAM_NAME.lower())
    mp = league.currentMatchupPeriod
    if 0 < mp <= len(me.schedule):
        m = me.schedule[mp - 1]
        home = m.home_team is me
        other = m.away_team if home else m.home_team
        opponent = getattr(other, "team_name", None)

        def score(side):
            live = getattr(m, f"{side}_team_live_score", None)
            return live if live is not None else getattr(m, f"{side}_final_score", None)
        my_score, opp_score = (score("home"), score("away")) if home else (score("away"), score("home"))

    return {"players": players, "activity": activity, "league": league, "sched": sched,
            "today": league.scoringPeriodId, "matchup": mp, "opponent": opponent,
            "my_score": my_score, "opp_score": opp_score}


# ---------------------------------------------------------------------------
# Alert logic (pure functions: snapshot in, messages out)
# ---------------------------------------------------------------------------

def label(status: str) -> str:
    return config.STATUS_LABELS.get(status, status.replace("_", " ").title())


def is_out(status: str) -> bool:
    return status in config.OUT_STATUSES


_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a": 1, "a few": 3, "several": 4}


def timeline_from_news(texts: list[str]) -> tuple[str, int | None] | None:
    """Read an absence estimate from news text. Returns (label, days) or None."""
    import re
    from datetime import date
    months = ["january", "february", "march", "april", "may", "june", "july",
              "august", "september", "october", "november", "december"]
    for t in texts:
        t = t.lower()
        m = re.search(r"(?:return|back|until|target)\D{0,40}?\b(" + "|".join(months) + r")\b", t)
        if m:
            today = datetime.now(timezone.utc).date()
            mi = months.index(m.group(1)) + 1
            year = today.year if mi >= today.month else today.year + 1
            days = (date(year, mi, 1) - today).days
            return (f"{m.group(1)[:3]}/{year} (pelas notícias)", max(days, 0))
        if re.search(r"(rest of|remainder of) the (regular )?season|season-ending|out for the season", t):
            return ("resto da temporada", 180)
        m = re.search(r"(\d+|a few|several|one|two|three|four|five|six|seven|eight|nine|ten|a)[\s-]+(?:to[\s-]+\w+[\s-]+)?(week|month)s?", t)
        if m and re.search(r"out|sideline|miss|re-evaluat|reevaluat|return|recover|timetable", t):
            n = m.group(1)
            n = int(n) if n.isdigit() else _NUM_WORDS.get(n, 2)
            days = n * (30 if m.group(2) == "month" else 7)
            unit = "mês" if m.group(2) == "month" and n == 1 else ("meses" if m.group(2) == "month" else ("semana" if n == 1 else "semanas"))
            return (f"~{n} {unit} (pelas notícias)", days)
        if "indefinitely" in t or "no timetable" in t or "without a timetable" in t:
            return ("indefinido", None)
        if "day-to-day" in t:
            return ("dia a dia", 2)
    return None


def return_line(p: dict, today=None) -> str:
    """Human line about when the player is back, plus a long-absence flag."""
    from datetime import date
    today = today or datetime.now(timezone.utc).date()
    days = None
    if p.get("return_date"):
        d = date.fromisoformat(p["return_date"])
        days = max((d - today).days, 0)
        weeks = round(days / 7)
        span = f"{days} dias" if days < 14 else f"~{weeks} semanas"
        text = f"Volta prevista: {d:%d/%m} ({span})"
    elif p.get("timeline"):
        label, days = p["timeline"]
        text = f"Volta: {label}"
    else:
        text = "Volta: sem previsão"
    if days is None and p.get("timeline") and p["timeline"][0] == "indefinido":
        text += "\n🗓️ Ausência indefinida → reposição pode valer por semanas"
    elif days is not None and days >= config.LONG_ABSENCE_DAYS:
        text += "\n🗓️ Ausência longa → reposição vale por semanas"
    return text


def my_roster(players: dict) -> list[dict]:
    me = config.MY_TEAM_NAME.lower()
    return [p for p in players.values() if (p["fantasy_team"] or "").lower() == me]


def worst_player(players: dict) -> dict | None:
    roster = [p for p in my_roster(players) if p["slot"] != "IR"]
    return min(roster, key=lambda p: p["value"]) if roster else None


POS_NUM = {"PG": 1, "SG": 2, "SF": 3, "PF": 4, "C": 5}


def pos_distance(a: list[str], b: list[str]) -> int:
    na = [POS_NUM[x] for x in a if x in POS_NUM]
    nb = [POS_NUM[x] for x in b if x in POS_NUM]
    if not na or not nb:
        return 2
    return min(abs(x - y) for x in na for y in nb)


def minutes_boost(out_player: dict, players: dict) -> dict[str, dict]:
    """Estimate how the absent player's minutes spread over healthy teammates.

    Returns {pid: {"extra_min", "boost", "projected"}} for every healthy teammate
    (rostered or free), so shares are realistic even if we only suggest FAs.
    """
    freed = out_player.get("mpg") or config.DEFAULT_OUT_MPG
    mates = [
        p for p in players.values()
        if p["nba_team"] == out_player["nba_team"] and p["id"] != out_player["id"]
        and not is_out(p["status"]) and p["nba_team"] != "FA"
    ]
    weights = {}
    for p in mates:
        d = min(pos_distance(out_player["positions"], p["positions"]), len(config.POS_WEIGHTS) - 1)
        weights[p["id"]] = config.POS_WEIGHTS[d]
    total = sum(weights.values()) or 1
    out = {}
    for p in mates:
        mpg = p.get("mpg") or config.DEFAULT_MPG
        extra = min(freed * weights[p["id"]] / total, max(config.MAX_MPG - mpg, 0))
        fpm = (p["value"] / mpg) if mpg else 0
        boost = extra * fpm
        out[p["id"]] = {"extra_min": round(extra), "boost": round(boost, 1),
                        "projected": round(p["value"] + boost, 1)}
    return out


def beneficiaries(out_player: dict, players: dict) -> list[dict]:
    """Free teammates ranked by projected value *with* the extra minutes."""
    boosts = minutes_boost(out_player, players)
    cands = []
    for pid, b in boosts.items():
        p = players[pid]
        if p["fantasy_team"] is None and b["projected"] >= config.BENEFICIARY_MIN_FP:
            cands.append({**p, **b})
    cands.sort(key=lambda p: p["projected"], reverse=True)
    return cands[: config.MAX_SUGGESTIONS]


def ben_text(b: dict) -> str:
    pos = "/".join(b["positions"])
    if b["boost"] >= 1:
        return f"*{b['name']}* ({pos}, {b['value']} → ~{b['projected']:g} FP com +{b['extra_min']} min)"
    return f"*{b['name']}* ({pos}, {b['value']} FP)"


def _drop_hint(players: dict, pickup_value: float) -> str:
    w = worst_player(players)
    if w and pickup_value > w["value"]:
        return f"\nDrop sugerido: *{w['name']}* ({w['value']} FP)"
    return ""


def _my_player_extras(prev: dict, p: dict, new: dict, ctx: dict | None) -> str:
    """Lineup swap (late scratch) and IR hints for one of your players."""
    extra = ""
    mine = my_roster(new)
    ir_taken = any(q["slot"] == "IR" and q["id"] != p["id"] for q in mine)
    if is_out(p["status"]) and not is_out(prev["status"]):
        if ctx:
            today, sched, now_ms = ctx["today"], ctx["sched"], ctx["now_ms"]
            pdates = ctx["pdates"]
            tip = sched.get(p["nba_team"], {}).get(today)
            if tip and tip > now_ms and p["slot"] not in planner.BENCH_SLOTS and today in pdates:
                subs = sorted([q for q in mine if q["slot"] == "BE" and planner.plays(q, today, sched, pdates, after_ms=now_ms)],
                              key=lambda q: q["value"], reverse=True)
                when = datetime.fromtimestamp(tip / 1000, planner.ET).strftime("%H:%M")
                if subs:
                    extra += f"\n🔄 Está no seu lineup hoje (jogo {when}): troque por *{subs[0]['name']}* ({subs[0]['value']} FP)"
                else:
                    extra += f"\n🔄 Está no seu lineup hoje (jogo {when}) e nenhum reserva joga: vaga para um FA"
        if p["slot"] != "IR" and not ir_taken:
            extra += "\n🏥 Sua vaga de IR está livre: mova-o para o IR e libere uma vaga no elenco"
    if prev["status"] != p["status"] and not is_out(p["status"]) and p["slot"] == "IR":
        extra += "\n🏥 Ele está saudável no IR: tire-o de lá (a ESPN bloqueia suas transações enquanto isso)"
    return extra


def injury_alerts(old: dict, new: dict, ctx: dict | None = None) -> list[tuple[str, str]]:
    out = []
    for pid, p in new.items():
        prev = old.get(pid)
        if not prev or prev["status"] == p["status"]:
            continue
        mine = (p["fantasy_team"] or "").lower() == config.MY_TEAM_NAME.lower()
        key = f"inj:{pid}:{p['status']}"

        if mine:
            icon = "🚑" if is_out(p["status"]) else ("✅" if p["status"] == "ACTIVE" else "⚠️")
            msg = f"{icon} *Seu jogador: {p['name']}* ({p['nba_team']})\n{label(prev['status'])} → *{label(p['status'])}*"
            if is_out(p["status"]):
                msg += "\n" + return_line(p)
                for b in beneficiaries(p, new):
                    msg += f"\nReposição livre: {ben_text(b)}"
            msg += _my_player_extras(prev, p, new, ctx)
            out.append((key, msg))
            continue

        if is_out(p["status"]) and not is_out(prev["status"]) and p["value"] >= config.STAR_MIN_FP:
            bens = beneficiaries(p, new)
            if not bens:
                continue  # nobody free to grab: not actionable
            w = worst_player(new)
            if config.ACTIONABLE_ONLY and w and bens[0]["projected"] < w["value"] - config.ACTIONABLE_MARGIN_FP:
                continue  # best free option wouldn't make your roster
            owner = p["fantasy_team"] or "free agent"
            msg = f"🚨 *{p['name']} ({p['nba_team']}) → {label(p['status'])}*\n({owner}, {p['value']} FP)\n" + return_line(p)
            for i, b in enumerate(bens):
                tag = "ADD" if i == 0 else "alt."
                msg += f"\n{tag}: {ben_text(b)} — LIVRE"
            msg += _drop_hint(new, bens[0]["projected"])
            msg += f"\n{config.ADD_PLAYERS_URL}"
            out.append((key, msg))
    return out


def drop_alerts(new_players: dict, activity: list[dict], since_ms: int) -> list[tuple[str, str]]:
    out = []
    by_name = {p["name"]: p for p in new_players.values()}
    w = worst_player(new_players)
    for a in activity:
        if a["action"] != "DROPPED" or a["date"] <= since_ms:
            continue
        p = by_name.get(a["player"])
        if not p or p["fantasy_team"] is not None:
            continue  # unknown, or already picked up again
        if (a["team"] or "").lower() == config.MY_TEAM_NAME.lower():
            continue  # you dropped him
        if w and p["value"] < w["value"] + config.DROP_MARGIN_FP:
            continue
        msg = (
            f"🔁 *{a['team']} dropou {p['name']}* ({p['nba_team']}, {p['value']} FP)\n"
            f"Waiver de 1 dia: faça o claim."
        )
        msg += _drop_hint(new_players, p["value"])
        msg += f"\n{config.ADD_PLAYERS_URL}"
        out.append((f"drop:{p['id']}:{a['date']}", msg))
    return out


def trade_alerts(old: dict, new: dict) -> list[tuple[str, str]]:
    out = []
    for pid, p in new.items():
        prev = old.get(pid)
        if not prev or prev["nba_team"] == p["nba_team"]:
            continue
        mine = (p["fantasy_team"] or "").lower() == config.MY_TEAM_NAME.lower()
        if not mine and max(p["value"], prev["value"]) < config.TRADE_MIN_FP:
            continue
        verb = "dispensado (sem time)" if p["nba_team"] == "FA" else f"vai para *{p['nba_team']}*"
        who = "Seu jogador " if mine else ""
        msg = f"🔄 *Troca NBA:* {who}*{p['name']}* ({prev['nba_team']}) {verb}"
        # His old team loses minutes: anyone free to benefit?
        bens = beneficiaries({**prev, "id": pid}, new)
        for b in bens:
            msg += f"\nGanha minutos no {prev['nba_team']}: {ben_text(b)} — LIVRE"
        out.append((f"trade:{pid}:{p['nba_team']}", msg))
    return out


def fetch_recent_games(league, player_ids: list[str]) -> dict[str, list[dict]]:
    """Per-game fantasy points for these players: {pid: [{date, fp, min}], newest first}."""
    if not player_ids:
        return {}
    cards = league.player_info(playerId=[int(i) for i in player_ids])
    cards = cards if isinstance(cards, list) else ([cards] if cards else [])
    out: dict[str, list[dict]] = {}
    for pl in cards:
        games = []
        for key, st in pl.stats.items():
            if not key.isdigit() or not st.get("date"):
                continue
            total = st.get("total") or {}
            mins = total.get("MIN", total.get("MPG", 0)) or 0
            if mins <= 0 and not st.get("applied_total"):
                continue  # DNP
            games.append({"date": st["date"].replace(tzinfo=timezone.utc) if st["date"].tzinfo is None else st["date"],
                          "fp": round(st.get("applied_total", 0) or 0, 1), "min": round(mins)})
        games.sort(key=lambda g: g["date"], reverse=True)
        out[str(pl.playerId)] = games
    return out


def moving_avg(games: list[dict], n: int) -> float | None:
    window = games[:n]
    return round(sum(g["fp"] for g in window) / len(window), 1) if window else None


def hot_alerts(games_by_pid: dict, players: dict, now: datetime) -> list[tuple[str, str]]:
    """Free agents whose moving average just reached the bar (one alert per crossing)."""
    out = []
    w = worst_player(players)
    n = config.HOT_AVG_GAMES
    for pid, games in games_by_pid.items():
        p = players.get(pid)
        if not p or p["fantasy_team"] is not None:
            continue
        done = [g for g in games if (now - g["date"]).total_seconds() >= config.HOT_GAME_DONE_HOURS * 3600]
        if len(done) < config.HOT_MIN_GAMES:
            continue
        if (now - done[0]["date"]).total_seconds() > config.HOT_FRESH_HOURS * 3600:
            continue
        avg_now = moving_avg(done, n)
        avg_before = moving_avg(done[1:], n) if len(done[1:]) >= config.HOT_MIN_GAMES else None
        if avg_now < config.HOT_GAME_FP:
            continue
        if avg_before is not None and avg_before >= config.HOT_GAME_FP:
            continue  # already above the bar last game: alerted then
        k = min(n, len(done))
        recent = ", ".join(f"{g['fp']:g} ({g['min']} min)" for g in done[:k])
        mins_trend = done[0]["min"] - (sum(g["min"] for g in done[1:k + 1]) / max(len(done[1:k + 1]), 1))
        msg = (f"🔥 *{p['name']}* ({p['nba_team']}, {'/'.join(p['positions'])}, LIVRE)\n"
               f"Média dos últimos {k}: *{avg_now:g} FP* (ref {p['value']})\n"
               f"Jogos: {recent}")
        if mins_trend >= 4:
            msg += f"\nMinutos subindo (+{mins_trend:.0f} no último jogo)"
        if w and avg_now > w["value"]:
            msg += f"\nSeu pior hoje: *{w['name']}* ({w['value']} FP)"
        msg += f"\n{config.ADD_PLAYERS_URL}"
        out.append((f"hot:{pid}:{done[0]['date']:%Y%m%d}", msg))
    return out


def digest_message(games_by_pid: dict, players: dict, now: datetime, seen: set) -> tuple[str | None, list[str]]:
    """One message with every free agent who finished a HOT_GAME_FP+ game not yet reported."""
    rows, keys = [], []
    for pid, games in games_by_pid.items():
        p = players.get(pid)
        if not p or p["fantasy_team"] is not None:
            continue
        done = [g for g in games if (now - g["date"]).total_seconds() >= config.HOT_GAME_DONE_HOURS * 3600]
        if not done:
            continue
        g = done[0]
        key = f"{pid}:{g['date']:%Y%m%d}"
        if key in seen or g["fp"] < config.HOT_GAME_FP:
            continue
        if (now - g["date"]).total_seconds() > config.HOT_FRESH_HOURS * 3600:
            continue
        rows.append((g["fp"], p, g, moving_avg(done, config.HOT_AVG_GAMES) if len(done) >= config.HOT_MIN_GAMES else None))
        keys.append(key)
    if not rows:
        return None, []
    rows.sort(key=lambda r: r[0], reverse=True)
    lines = [f"📊 *Free agents com {config.HOT_GAME_FP:g}+ FP* ({len(rows)})"]
    for fp, p, g, mavg in rows:
        fire = f" · média {mavg:g} 🔥" if mavg is not None and mavg >= config.HOT_GAME_FP else (f" · média {mavg:g}" if mavg is not None else "")
        lines.append(f"• *{p['name']}* ({p['nba_team']}, {'/'.join(p['positions'])}) — {fp:g} FP, {g['min']} min · ref {p['value']}{fire}")
    w = worst_player(players)
    if w:
        lines.append(f"Seu pior hoje: {w['name']} ({w['value']} FP)")
    return "\n".join(lines), keys


def matchup_periods(league, state: dict, matchup: int, sched: dict | None = None) -> list[int]:
    """Scoring periods (days) of a matchup: settings give weeks, the schedule gives days."""
    cache = state.get("matchup_periods") or {}
    if not cache or state.get("matchup_periods_day") != datetime.now(planner.ET).date().isoformat():
        try:
            data = league.espn_request.league_get(params={"view": "mSettings"})
            cache = data["settings"]["scheduleSettings"]["matchupPeriods"]
            state["matchup_periods"] = cache
            state["matchup_periods_day"] = datetime.now(planner.ET).date().isoformat()
        except Exception as e:
            print(f"matchup periods lookup failed: {e!r}")
    weeks = sorted(int(x) for x in cache.get(str(matchup), []))
    return planner.week_days(sched or {}, weeks)


def build_context(snap: dict, state: dict, now: datetime) -> dict | None:
    league = snap.get("league")
    if league is None or not snap.get("sched"):
        return None
    today = snap["today"]
    periods = matchup_periods(league, state, snap["matchup"], snap["sched"])
    all_pers = sorted(set(periods) | {today})
    pdates = planner.period_dates(snap["sched"], all_pers)
    now_ms = int(now.timestamp() * 1000)
    players = snap["players"]
    me = config.MY_TEAM_NAME.lower()
    start_ms = min((ms for team in snap["sched"].values() for per, ms in team.items() if periods and per == periods[0]),
                   default=now_ms) - 14 * 3600 * 1000
    adds_used = sum(1 for a in snap["activity"] if (a["team"] or "").lower() == me
                    and a["action"] in ("FA ADDED", "WAIVER ADDED") and a["date"] >= start_ms)
    on_waivers = {a["player"] for a in snap["activity"]
                  if a["action"] == "DROPPED" and now_ms - a["date"] < config.WAIVER_HOURS * 3600 * 1000}
    return {
        "today": today, "periods": periods, "remaining": [p for p in periods if p >= today],
        "pdates": pdates, "sched": snap["sched"], "now_ms": now_ms,
        "roster": my_roster(players),
        "opp_roster": [p for p in players.values() if snap.get("opponent")
                       and (p["fantasy_team"] or "").lower() == snap["opponent"].lower()],
        "opponent": snap.get("opponent"), "my_score": snap.get("my_score"), "opp_score": snap.get("opp_score"),
        "adds_left": max(config.MATCHUP_ADD_LIMIT - adds_used, 0),
        "fas": sorted([p for p in players.values() if p["fantasy_team"] is None and p["name"] not in on_waivers
                       and p["status"] not in config.OUT_STATUSES], key=lambda p: p["value"], reverse=True)[:config.STREAM_CANDIDATES],
        "matchup": snap["matchup"],
    }


def schedule_alerts(ctx: dict, now: datetime) -> list[tuple[str, str]]:
    """Time-based messages: lineup check, weekly plan, daily plan step, matchup status."""
    out = []
    today, sched, pdates, now_ms = ctx["today"], ctx["sched"], ctx["pdates"], ctx["now_ms"]
    now_et = now.astimezone(planner.ET)
    tips = [ms for team in sched.values() for per, ms in team.items() if per == today]
    first_tip = min(tips) if tips else None

    # 1) lineup check before the day's first game
    if first_tip and today in pdates:
        lo, hi = config.LINEUP_CHECK_MIN_BEFORE
        mins = (first_tip - now_ms) / 60000
        if hi <= mins <= lo:
            issues = planner.lineup_issues(ctx["roster"], today, sched, pdates, now_ms)
            if issues:
                out.append((f"lineup:{today}", "🧩 *Checagem de lineup* (primeiro jogo em "
                            f"{mins:.0f} min)\n" + "\n".join(issues)))

    rem = ctx["remaining"]
    if not rem or now_et.hour < config.PLAN_HOUR_ET:
        return out

    plan = planner.streaming_plan(ctx["roster"], ctx["fas"], rem, sched, pdates, ctx["adds_left"], now_ms)
    gain = sum(sl["gain"] for sl in plan["slots"])
    status = week_projection(ctx, rem, gain)

    if today == ctx["periods"][0]:
        # weekly digest on the matchup's first day: projection + plan
        d0, d1 = pdates.get(ctx["periods"][0]), pdates.get(ctx["periods"][-1])
        title = f"🗓️ *Rodada {ctx['matchup']}* ({d0:%d/%m}–{d1:%d/%m}) vs {ctx['opponent'] or '?'}"
        body = title + "\n" + status + "\n\n" + planner.render_plan(plan, pdates, ctx["adds_left"], "*Plano de FAs*")
        out.append((f"week:{ctx['matchup']}", body))
    else:
        # every other day: where the week stands, plus today's moves if any
        lines = [f"📊 *Rodada {ctx['matchup']}* — {planner.day_label(pdates[today])}", status]
        todays = [st for sl in plan["slots"] for st in sl["steps"] if st["period"] == today]
        if todays:
            lines.append("\n📋 *Plano de hoje*")
            for st in todays:
                days = ", ".join(planner.day_label(pdates[q]) for q in st["games"])
                lines.append(f"• dropar {st['drop']['name']} → pegar *{st['add']['name']}* ({st['add']['nba_team']}, {st['add']['value']} FP; joga {days})")
            lines.append(config.ADD_PLAYERS_URL)
        out.append((f"daily:{today}", "\n".join(lines)))
    return out


def week_projection(ctx: dict, rem: list[int], plan_gain: float) -> str:
    """Whole-matchup forecast: points already scored + rest of the week, rosters as they are."""
    sched, pdates, now_ms = ctx["sched"], ctx["pdates"], ctx["now_ms"]
    my_now, opp_now = ctx.get("my_score") or 0, ctx.get("opp_score") or 0
    my_rest, my_g = planner.projected_remaining(ctx["roster"], rem, sched, pdates, now_ms)
    if ctx["opp_roster"]:
        opp_rest, opp_g = planner.projected_remaining(ctx["opp_roster"], rem, sched, pdates, now_ms)
    else:
        opp_rest, opp_g = 0.0, 0
    me_final, opp_final = my_now + my_rest, opp_now + opp_rest
    diff = me_final - opp_final
    close = abs(diff) < 0.05 * max(me_final, opp_final, 1)
    verdict = "apertado" if close else ("vitória provável" if diff > 0 else "derrota provável")
    lines = []
    if my_now or opp_now:
        lines.append(f"Placar até agora: você {my_now:.0f} × {opp_now:.0f}")
    lines.append(f"Projeção da rodada inteira *sem FAs*: você ~{me_final:.0f} × ~{opp_final:.0f} → *{verdict}* ({diff:+.0f})")
    lines.append(f"Jogos restantes: você {my_g} × {opp_g}")
    if plan_gain >= 1:
        with_plan = diff + plan_gain
        v2 = "vitória provável" if with_plan > 0 else "ainda atrás"
        lines.append(f"Com o plano de FAs: ~{me_final + plan_gain:.0f} → {v2} ({with_plan:+.0f})")
    return "\n".join(lines)


def enrich_timelines(league, old: dict, new: dict) -> None:
    """For players who just went OUT without an ESPN return date, read the news."""
    if league is None:
        return
    for pid, p in new.items():
        prev = old.get(pid)
        if not prev or not is_out(p["status"]) or is_out(prev["status"]) or p.get("return_date"):
            continue
        mine = (p["fantasy_team"] or "").lower() == config.MY_TEAM_NAME.lower()
        if not mine and p["value"] < config.STAR_MIN_FP:
            continue
        try:
            card = league.player_info(playerId=int(pid), include_news=True)
            items = (card.news or [])[:3]
            texts = [n.get("headline", "") for n in items] + [n.get("story", "") for n in items]
            p["timeline"] = timeline_from_news(texts)
        except Exception as e:
            print(f"News lookup failed for {p['name']}: {e!r}")


def compute_alerts(state: dict, snap: dict, ctx: dict | None = None) -> list[tuple[str, str]]:
    old, new = state["players"], snap["players"]
    since = state.get("last_activity_ms", 0)
    alerts = injury_alerts(old, new, ctx) + drop_alerts(new, snap["activity"], since) + trade_alerts(old, new)
    sent = set(state.get("sent_keys", []))
    return [(k, m) for k, m in alerts if k not in sent]


# ---------------------------------------------------------------------------
# Delivery: Telegram (primary) and WhatsApp via CallMeBot (optional)
# ---------------------------------------------------------------------------

def to_telegram_html(text: str) -> str:
    """Escape HTML, then turn WhatsApp-style *bold* into <b>bold</b>."""
    import html
    import re
    safe = html.escape(text, quote=False)
    return re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", safe)


def _post_with_retry(name: str, call) -> bool:
    for attempt in range(3):
        try:
            r = call()
            if r.ok:
                return True
            print(f"{name} HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            print(f"{name} error: {e}")
        time.sleep(3 * (attempt + 1))
    return False


def send_telegram(text: str) -> bool:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat,
        "text": to_telegram_html(text),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    return _post_with_retry("Telegram", lambda: requests.post(url, json=payload, timeout=30))


def send_callmebot(text: str) -> bool:
    url = (
        "https://api.callmebot.com/whatsapp.php?phone="
        + urllib.parse.quote(os.environ["CALLMEBOT_PHONE"])
        + "&text=" + urllib.parse.quote(text)
        + "&apikey=" + urllib.parse.quote(os.environ["CALLMEBOT_APIKEY"])
    )
    return _post_with_retry("CallMeBot", lambda: requests.get(url, timeout=30))


def send_alert(text: str) -> bool:
    """Send through every configured channel; success if any channel worked."""
    channels = []
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        channels.append(send_telegram)
    if os.environ.get("CALLMEBOT_PHONE") and os.environ.get("CALLMEBOT_APIKEY"):
        channels.append(send_callmebot)
    if not channels:
        print("[dry-run] would send:\n" + text + "\n")
        return True
    results = [send(text) for send in channels]
    return any(results)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"players": {}, "sent_keys": [], "last_activity_ms": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False))


def main() -> int:
    state = load_state()
    now = datetime.now(timezone.utc)

    try:
        snap = fetch_league_snapshot()
    except Exception as e:  # cookies expired, ESPN down, etc.
        print(f"ESPN fetch failed: {e!r}")
        last = state.get("last_error_alert", 0)
        if time.time() - last > config.ERROR_ALERT_HOURS * 3600:
            send_alert(f"⚠️ Fantasy bot: não consegui ler a ESPN.\n{str(e)[:200]}\nSe persistir, renove os cookies.")
            state["last_error_alert"] = time.time()
            save_state(state)
        return 0  # handled: avoid GitHub failure emails every run

    first_run = not state["players"]
    newest_ms = max([a["date"] for a in snap["activity"]] + [state.get("last_activity_ms", 0)])

    if first_run:
        w = worst_player(snap["players"])
        mine = my_roster(snap["players"])
        send_alert(
            f"🏀 Fantasy bot online!\n{len(mine)} jogadores no {config.MY_TEAM_NAME}. "
            f"Seu pior jogador hoje: {w['name']} ({w['value']} FP).\n"
            f"Você vai receber alertas de lesão, drops e trocas daqui pra frente."
        )
        alerts = []
    else:
        league = snap.get("league")
        enrich_timelines(league, state["players"], snap["players"])
        try:
            ctx = build_context(snap, state, now)
        except Exception as e:
            print(f"context failed: {e!r}")
            ctx = None
        alerts = compute_alerts(state, snap, ctx)
        if ctx:
            try:
                sent_now = set(state.get("sent_keys", []))
                alerts += [(k, m) for k, m in schedule_alerts(ctx, now) if k not in sent_now]
                state["plan_status"] = f"ok: day {ctx['today']}, matchup {ctx['matchup']} {ctx['periods'][:1]}..{ctx['periods'][-1:]}, adds left {ctx['adds_left']}, opp {ctx['opponent']}"
            except Exception as e:
                print(f"schedule alerts failed: {e!r}")
                state["plan_status"] = f"error: {e!r}"[:300]
        hot_due = time.time() - state.get("last_hot_check", 0) >= config.HOT_CHECK_MINUTES * 60
        digest_due = time.time() - state.get("last_digest", 0) >= config.DIGEST_EVERY_MINUTES * 60
        if league and (hot_due or digest_due):
            try:
                fa_ids = [pid for pid, p in snap["players"].items() if p["fantasy_team"] is None]
                games = fetch_recent_games(league, fa_ids)
                if hot_due:
                    sent_now = set(state.get("sent_keys", []))
                    alerts += [(k, m) for k, m in hot_alerts(games, snap["players"], now) if k not in sent_now]
                    state["last_hot_check"] = time.time()
                if digest_due:
                    seen = set(state.get("digest_seen", []))
                    msg, keys = digest_message(games, snap["players"], now, seen)
                    if msg is None or send_alert(msg):
                        state["digest_seen"] = (list(seen) + keys)[-1000:]
                        state["last_digest"] = time.time()
                state["hot_status"] = f"ok: {len(games)} FAs, {sum(len(g) for g in games.values())} games"
            except Exception as e:
                print(f"Hot-player check failed: {e!r}")
                state["hot_status"] = f"error: {e!r}"[:300]

    sent = list(state.get("sent_keys", []))
    for key, msg in alerts:
        if send_alert(msg):
            sent.append(key)
        else:
            print(f"Failed to send alert {key}; will retry next run")

    # Keep the old status for players whose alert failed, so it retries next run.
    failed_keys = {k for k, _ in alerts} - set(sent)
    players = {pid: {k: v for k, v in p.items() if k != "timeline"} for pid, p in snap["players"].items()}
    for k in failed_keys:
        if k.startswith("inj:") or k.startswith("trade:"):
            pid = k.split(":")[1]
            if pid in state["players"]:
                players[pid] = state["players"][pid]

    state.update({
        "players": players,
        "sent_keys": sent[-MAX_SENT_KEYS:],
        "last_activity_ms": newest_ms if not any(k.startswith("drop:") for k in failed_keys) else state.get("last_activity_ms", 0),
    })
    save_state(state)
    print(f"OK: {len(alerts)} alert(s), {len(players)} players tracked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
