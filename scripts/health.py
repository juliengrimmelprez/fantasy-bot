"""Check every credential the bot uses, without sending any message.

Writes a report to stdout. Run via the "Health check" workflow.
Secret values are never printed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

results: list[tuple[str, str, str]] = []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"{type(e).__name__}: {str(e)[:200]}"
    results.append((name, "✅" if ok else "❌", detail))


def espn():
    s2, swid = os.environ.get("ESPN_S2", ""), os.environ.get("ESPN_SWID", "")
    if not s2 or not swid:
        return False, "secret missing"
    if not (swid.startswith("{") and swid.endswith("}")):
        return False, "ESPN_SWID should include the curly braces {…}"
    from espn_api.basketball import League
    lg = League(config.LEAGUE_ID, config.SEASON, espn_s2=s2, swid=swid)
    names = [t.team_name.strip() for t in lg.teams]
    mine = next((t for t in lg.teams if t.team_name.strip().lower() == config.MY_TEAM_NAME.lower()), None)
    if not mine:
        return False, f"logged in but '{config.MY_TEAM_NAME}' not found ({len(names)} teams)"
    fa = lg.free_agents(size=5)
    return True, f"league read OK: {len(names)} teams, {len(mine.roster)} on your roster, free agents OK ({len(fa)})"


def telegram():
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        return False, "secret missing"
    me = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=20).json()
    if not me.get("ok"):
        return False, f"bot token rejected: {me.get('description')}"
    ch = requests.get(f"https://api.telegram.org/bot{tok}/getChat", params={"chat_id": chat}, timeout=20).json()
    if not ch.get("ok"):
        return False, f"bot @{me['result']['username']} OK, but chat id rejected: {ch.get('description')}"
    return True, f"bot @{me['result']['username']} OK, chat reachable"


def callmebot():
    phone, key = os.environ.get("CALLMEBOT_PHONE", ""), os.environ.get("CALLMEBOT_APIKEY", "")
    if not phone and not key:
        return True, "not configured (optional, WhatsApp off)"
    if not phone or not key:
        return False, "only one of the two secrets is set"
    return True, "configured (not test-sent, to avoid a WhatsApp message)"


def bot_state():
    st = json.loads(Path(__file__).resolve().parents[1].joinpath("state.json").read_text())
    import time
    notes = []
    le = st.get("last_error_alert")
    if le:
        notes.append(f"last ESPN read error {((time.time() - le) / 3600):.0f}h ago")
    for k in ("hot_status", "plan_status"):
        v = st.get(k, "")
        notes.append(f"{k}: {v}")
    bad = any(str(st.get(k, "")).startswith("error") for k in ("hot_status", "plan_status"))
    return not bad, "; ".join(notes)


check("ESPN cookies (espn_s2 / SWID)", espn)
check("Telegram bot token + chat id", telegram)
check("CallMeBot (WhatsApp)", callmebot)
check("Bot internal status", bot_state)

print("# Health check\n")
for name, icon, detail in results:
    print(f"- {icon} **{name}** — {detail}")
print("\nGitHub token used by cron-job.org: checked from the run history (a dispatch every ~2 min = token valid).")
