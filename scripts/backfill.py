"""Replay the last N days and print the alerts the bot would have sent.

Nothing is sent to Telegram/WhatsApp; the report goes to stdout only.
Run in GitHub Actions via the "Backfill report" workflow.

Limits: ESPN keeps no history of injury statuses or NBA team changes, so
injuries are reconstructed from *current* statuses plus recent player news,
and NBA trades can't be replayed. League transactions are exact.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
import config  # noqa: E402

DAYS = int(os.environ.get("BACKFILL_DAYS", "7"))
ET = timezone(timedelta(hours=-4))


def fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, ET).strftime("%a %d/%m %H:%M")


def main() -> None:
    from espn_api.basketball import League

    now = datetime.now(timezone.utc)
    since_ms = int((now - timedelta(days=DAYS)).timestamp() * 1000)

    snap = bot.fetch_league_snapshot()
    players = snap["players"]
    by_name = {p["name"]: p for p in players.values()}
    worst = bot.worst_player(players)
    me = config.MY_TEAM_NAME.lower()

    league = League(config.LEAGUE_ID, config.SEASON,
                    espn_s2=os.environ["ESPN_S2"], swid=os.environ["ESPN_SWID"])

    # ---- league transactions -------------------------------------------
    acts = []
    for offset in range(0, 400, 100):
        page = league.recent_activity(size=100, offset=offset)
        if not page:
            break
        for a in page:
            for (team, action, pname, _pos) in a.actions:
                acts.append({
                    "date": a.date,
                    "team": getattr(team, "team_name", str(team)) if team else "",
                    "action": action,
                    "player": pname if isinstance(pname, str) else getattr(pname, "name", str(pname)),
                })
        if min(a.date for a in page) < since_ms:
            break
    acts = sorted([a for a in acts if a["date"] >= since_ms], key=lambda a: a["date"])

    print(f"# Backfill — últimos {DAYS} dias (até {now.astimezone(ET):%d/%m %H:%M} ET)\n")
    print(f"Seu pior jogador hoje (referência para drops): {worst['name']} ({worst['value']} FP)\n")
    print(f"(debug) jogadores com minutos/jogo: {sum(1 for p in players.values() if p.get('mpg'))}/{len(players)}")
    try:
        sample = league.teams[0].roster[0]
        print("(debug) stats keys:", sorted(sample.stats.keys())[:12])
        for k, v in sample.stats.items():
            if isinstance(v, dict) and v.get("avg"):
                print("(debug)", k, "avg keys:", sorted(v["avg"].keys())[:30])
    except Exception as e:
        print("(debug) err", e)
    print()

    counts = Counter(a["action"] for a in acts)
    print(f"## Movimentação da liga: {len(acts)} ações — " + ", ".join(f"{k}: {v}" for k, v in counts.items()) + "\n")

    alerts: list[tuple[int, str, str]] = []  # (date_ms, kind, message)

    print("## Drops (todos), com quem pegou depois e em quanto tempo\n")
    for a in acts:
        if a["action"] != "DROPPED":
            continue
        p = by_name.get(a["player"])
        val = p["value"] if p else None
        pickup = next((b for b in acts if b["player"] == a["player"] and b["date"] > a["date"]
                       and b["action"] in ("FA ADDED", "WAIVER ADDED")), None)
        if pickup:
            hrs = (pickup["date"] - a["date"]) / 3_600_000
            after = f"→ pego por {pickup['team']} ({pickup['action']}) {hrs:.1f}h depois"
        else:
            after = "→ ainda livre" if (p and p["fantasy_team"] is None) else "→ (sem registro de pickup na janela)"
        would = (
            p is not None and (a["team"] or "").lower() != me
            and val is not None and val >= worst["value"] + config.DROP_MARGIN_FP
        )
        flag = "🔔 ALERTA" if would else "  silencioso"
        print(f"- {fmt_ms(a['date'])} | {flag} | {a['team']} dropou {a['player']} ({val if val is not None else '?'} FP) {after}")
        if would:
            msg = (f"🔁 *{a['team']} dropou {a['player']}* ({p['nba_team']}, {val} FP)\n"
                   f"Waiver de 1 dia: faça o claim.")
            if val > worst["value"]:
                msg += f"\nDrop sugerido: *{worst['name']}* ({worst['value']} FP)"
            alerts.append((a["date"], "drop", msg + f"\n_[{after.strip('→ ')}]_"))
    print()

    trades = [a for a in acts if a["action"] == "TRADED"]
    if trades:
        print("## Trocas entre managers da liga\n")
        for a in trades:
            print(f"- {fmt_ms(a['date'])} | {a['team']} recebeu {a['player']}")
        print()

    print("## Adds mais disputados (FA/waiver)\n")
    adds = [a for a in acts if a["action"] in ("FA ADDED", "WAIVER ADDED")]
    adds.sort(key=lambda a: -(by_name.get(a["player"], {}).get("value", 0)))
    for a in adds[:15]:
        v = by_name.get(a["player"], {}).get("value", "?")
        print(f"- {fmt_ms(a['date'])} | {a['team']} pegou {a['player']} ({v} FP, {a['action']})")
    print()

    # ---- injuries: reconstructed from current statuses --------------------
    injured = [p for p in players.values() if p["status"] != "ACTIVE"]
    old = {pid: dict(p) for pid, p in players.items()}
    for p in injured:
        old[p["id"]]["status"] = "ACTIVE"
    inj_alerts = bot.injury_alerts(old, players)
    alerted_ids = {k.split(":")[1] for k, _ in inj_alerts}

    relevant = [p for p in injured if (p["fantasy_team"] or "").lower() == me or p["value"] >= config.STAR_MIN_FP]
    news = {}
    if relevant:
        try:
            info = league.player_info(playerId=[int(p["id"]) for p in relevant], include_news=True)
            info = info if isinstance(info, list) else [info]
            for pl in info:
                news[str(pl.playerId)] = list(pl.news or [])
        except Exception as e:  # news is a nice-to-have
            print(f"(news indisponível: {e!r})\n")
    # Recompute with timelines from the news (same as the live bot does).
    for pid, items in news.items():
        if not players[pid].get("return_date"):
            players[pid]["timeline"] = bot.timeline_from_news(
                [n.get("headline", "") for n in items[:3]] + [n.get("story", "") for n in items[:3]])
    inj_alerts = bot.injury_alerts(old, players)
    alerted_ids = {k.split(":")[1] for k, _ in inj_alerts}

    print(f"## Lesões/status hoje: {len(injured)} jogadores não-ativos entre os {len(players)} acompanhados\n")
    for p in sorted(injured, key=lambda p: -p["value"]):
        if p["value"] < 15 and (p["fantasy_team"] or "").lower() != me:
            continue
        tag = "🔔 ALERTA" if p["id"] in alerted_ids else "  silencioso"
        owner = p["fantasy_team"] or "livre"
        print(f"- {tag} | {p['name']} ({p['nba_team']}, {p['value']} FP, {owner}) — {bot.label(p['status'])}")
        if p.get("return_date") or p.get("timeline"):
            print("    " + bot.return_line(p).replace("\n", " | "))
        for n in [n for n in news.get(p["id"], []) if n.get("published", "") >= (now - timedelta(days=DAYS)).isoformat()[:10]][:2]:
            print(f"    · {n.get('published', '')[:10]} {n.get('headline', '')}")
    print()

    # ---- messages, as they'd look ------------------------------------------
    print(f"## Mensagens que o bot teria mandado ({len(alerts) + len(inj_alerts)})\n")
    print("(Lesões: data real desconhecida — reconstruídas do status atual.)\n")
    for _, msg in inj_alerts:
        print("---\n" + msg + "\n")
    for d, _, msg in alerts:
        print(f"--- [{fmt_ms(d)}]\n" + msg + "\n")

    if os.environ.get("BACKFILL_SEND") == "true":
        msgs = [m for _, m in inj_alerts] + [m for _, _, m in alerts]
        bot.send_alert(f"🔁 *REPLAY dos últimos {DAYS} dias* — {len(msgs)} mensagens simuladas (status de lesão reconstruído do status atual). Não são alertas novos.")
        for i, m in enumerate(msgs, 1):
            bot.send_alert(f"[replay {i}/{len(msgs)}]\n{m}")
            import time; time.sleep(1)
        bot.send_alert("✅ Fim do replay.")

    per_day = Counter(fmt_ms(d)[:9] for d, _, _ in alerts)
    print("## Volume de alertas de drop por dia\n")
    for day, n in sorted(per_day.items()):
        print(f"- {day}: {n}")


if __name__ == "__main__":
    main()
