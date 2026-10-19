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

    return {"players": players, "activity": activity}


# ---------------------------------------------------------------------------
# Alert logic (pure functions: snapshot in, messages out)
# ---------------------------------------------------------------------------

def label(status: str) -> str:
    return config.STATUS_LABELS.get(status, status.replace("_", " ").title())


def is_out(status: str) -> bool:
    return status in config.OUT_STATUSES


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
                for b in beneficiaries(p, new):
                    msg += f"\nReposição livre: *{b['name']}* ({b['value']} FP)"
            out.append((key, msg))
            continue

        if is_out(p["status"]) and not is_out(prev["status"]) and p["value"] >= config.STAR_MIN_FP:
            bens = beneficiaries(p, new)
            if not bens:
                continue  # nobody free to grab: not actionable
            owner = p["fantasy_team"] or "free agent"
            msg = f"🚨 *{p['name']} ({p['nba_team']}) → {label(p['status'])}*\n({owner}, {p['value']} FP)"
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
        alerts = compute_alerts(state, snap)

    sent = list(state.get("sent_keys", []))
    for key, msg in alerts:
        if send_alert(msg):
            sent.append(key)
        else:
            print(f"Failed to send alert {key}; will retry next run")

    # Keep the old status for players whose alert failed, so it retries next run.
    failed_keys = {k for k, _ in alerts} - set(sent)
    players = snap["players"]
    for k in failed_keys:
        if k.startswith("inj:") or k.startswith("trade:"):
            pid = k.split(":")[1]
            if pid in state["players"]:
                players[pid] = state["players"][pid]

    state.update({
        "players": players,
        "sent_keys": sent[-MAX_SENT_KEYS:],
        "last_activity_ms": newest_ms if not any(k.startswith("drop:") for k in failed_keys) else state.get("last_activity_ms", 0),
        "last_run": now.isoformat(timespec="seconds"),
    })
    save_state(state)
    print(f"OK: {len(alerts)} alert(s), {len(players)} players tracked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
