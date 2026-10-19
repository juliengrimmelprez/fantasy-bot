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
    for act in league.recent_activity(size=50):
        for (team, action, player_name, _pos) in act.actions:
            activity.append({
                "date": act.date,
                "team": getattr(team, "team_name", str(team)) if team else "",
                "action": action,
                "player": player_name if isinstance(player_name, str) else getattr(player_name, "name", str(player_name)),
            })

    return {"players": players, "activity": activity, "league": league}


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
    for t in texts:
        t = t.lower()
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


def beneficiaries(out_player: dict, players: dict) -> list[dict]:
    """Free agents on the same NBA team, best first; same position ranks higher."""
    pos = set(out_player["positions"])
    cands = [
        p for p in players.values()
        if p["fantasy_team"] is None
        and p["nba_team"] == out_player["nba_team"]
        and p["id"] != out_player["id"]
        and not is_out(p["status"])
        and p["value"] >= config.BENEFICIARY_MIN_FP
    ]
    cands.sort(key=lambda p: (bool(pos & set(p["positions"])), p["value"]), reverse=True)
    return cands[: config.MAX_SUGGESTIONS]


def _drop_hint(players: dict, pickup_value: float) -> str:
    w = worst_player(players)
    if w and pickup_value > w["value"]:
        return f"\nDrop sugerido: *{w['name']}* ({w['value']} FP)"
    return ""


def injury_alerts(old: dict, new: dict) -> list[tuple[str, str]]:
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
                    msg += f"\nReposição livre: *{b['name']}* ({b['value']} FP)"
            out.append((key, msg))
            continue

        if is_out(p["status"]) and not is_out(prev["status"]) and p["value"] >= config.STAR_MIN_FP:
            bens = beneficiaries(p, new)
            if not bens:
                continue  # nobody free to grab: not actionable
            w = worst_player(new)
            if config.ACTIONABLE_ONLY and w and bens[0]["value"] < w["value"] - config.ACTIONABLE_MARGIN_FP:
                continue  # best free option wouldn't make your roster
            owner = p["fantasy_team"] or "free agent"
            msg = f"🚨 *{p['name']} ({p['nba_team']}) → {label(p['status'])}*\n({owner}, {p['value']} FP)\n" + return_line(p)
            for i, b in enumerate(bens):
                tag = "ADD" if i == 0 else "alt."
                msg += f"\n{tag}: *{b['name']}* ({'/'.join(b['positions'])}, {b['value']} FP) — LIVRE"
            msg += _drop_hint(new, bens[0]["value"])
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
            msg += f"\nGanha minutos no {prev['nba_team']}: *{b['name']}* ({b['value']} FP) — LIVRE"
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


def hot_alerts(games_by_pid: dict, players: dict, now: datetime) -> list[tuple[str, str]]:
    """Free agents on a run of good games (or one huge game, flagged as fluke)."""
    out = []
    w = worst_player(players)
    for pid, games in games_by_pid.items():
        p = players.get(pid)
        if not p or p["fantasy_team"] is not None:
            continue
        done = [g for g in games if (now - g["date"]).total_seconds() >= config.HOT_GAME_DONE_HOURS * 3600]
        if not done or (now - done[0]["date"]).total_seconds() > config.HOT_FRESH_HOURS * 3600:
            continue
        streak = 0
        for g in done:
            if g["fp"] >= config.HOT_GAME_FP:
                streak += 1
            else:
                break
        last = done[0]
        if streak >= config.HOT_MIN_STREAK:
            head = f"🔥 *{p['name']}* ({p['nba_team']}, LIVRE) — {streak} jogos bons seguidos"
            note = "Sequência = sinal mais confiável."
        elif streak == 1 and last["fp"] >= config.HOT_SINGLE_GAME_FP:
            head = f"📈 *{p['name']}* ({p['nba_team']}, LIVRE) — 1 jogo enorme"
            note = "Só 1 jogo: pode ser fluke. Vale observar o próximo."
        else:
            continue
        recent = ", ".join(f"{g['fp']:g} ({g['min']} min)" for g in done[:4])
        msg = f"{head}\nÚltimos: {recent}\nValor de referência: {p['value']} FP\n{note}"
        streak_avg = sum(g["fp"] for g in done[:max(streak, 1)]) / max(streak, 1)
        if w and streak_avg > w["value"]:
            msg += f"\nSeu pior hoje: *{w['name']}* ({w['value']} FP)"
        msg += f"\n{config.ADD_PLAYERS_URL}"
        out.append((f"hot:{pid}:{last['date']:%Y%m%d}", msg))
    return out


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
            texts = [n.get("headline", "") + " " + n.get("story", "") for n in (card.news or [])[:3]]
            p["timeline"] = timeline_from_news(texts)
        except Exception as e:
            print(f"News lookup failed for {p['name']}: {e!r}")


def compute_alerts(state: dict, snap: dict) -> list[tuple[str, str]]:
    old, new = state["players"], snap["players"]
    since = state.get("last_activity_ms", 0)
    alerts = injury_alerts(old, new) + drop_alerts(new, snap["activity"], since) + trade_alerts(old, new)
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
        alerts = compute_alerts(state, snap)
        last_hot = state.get("last_hot_check", 0)
        if league and time.time() - last_hot >= config.HOT_CHECK_MINUTES * 60:
            try:
                fa_ids = [pid for pid, p in snap["players"].items() if p["fantasy_team"] is None]
                games = fetch_recent_games(league, fa_ids)
                sent_now = set(state.get("sent_keys", []))
                alerts += [(k, m) for k, m in hot_alerts(games, snap["players"], now) if k not in sent_now]
                state["last_hot_check"] = time.time()
            except Exception as e:
                print(f"Hot-player check failed: {e!r}")

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
