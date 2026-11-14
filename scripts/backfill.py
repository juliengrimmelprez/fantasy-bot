"""Replay the last N days and print the alerts the bot would have sent.

Nothing is sent to Telegram/WhatsApp; the report goes to stdout only.
Run in GitHub Actions via the "Backfill report" workflow.

Limits: ESPN keeps no history of injury statuses or NBA team changes, so
injuries are reconstructed from *current* statuses plus recent player news,
and NBA trades can't be replayed. League transactions are exact.
"""

from __future__ import annotations

import os
import time
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


def send_samples(snap, players, now):
    """One example of each new alert type. Event-driven ones are simulated with real players."""
    import extras
    st = {"players": players}
    ctx = bot.build_context(snap, st, now)
    msgs = []
    T = "🧪 *TESTE — {}*\n"

    msgs.append(T.format("diagnóstico diário (real)") + bot.diag_message(ctx, ctx["remaining"]))
    for key, m in bot.weekly_extras(ctx):
        kind = {"recap": "resumo semanal (real, rodada anterior)", "race": "corrida pelos playoffs (real)",
                "trades": "ideias de troca (real)"}.get(key.split(":")[0], key)
        msgs.append(T.format(kind) + m)

    # 1) returning FA: best healthy free agent, pretending he was OUT
    fas = sorted([p for p in players.values() if p["fantasy_team"] is None and p["status"] == "ACTIVE"],
                 key=lambda p: -p["value"])
    if fas:
        old = {fas[0]["id"]: {**fas[0], "status": "OUT"}}
        r = extras.returning_fa_alerts(old, {fas[0]["id"]: fas[0]}, bot.worst_player(players)["value"])
        if r:
            msgs.append(T.format("FA voltando de lesão (simulado)") + r[0][1])

    # 2) cooling: your best non-IR player with a made-up slump
    mine = sorted([p for p in bot.my_roster(players) if p.get("slot") != "IR"], key=lambda p: -p["value"])
    if mine:
        p0 = mine[0]
        base = p0.get("season_avg") or p0["value"]
        mpg = p0.get("mpg") or 32
        fake = [{"date": now - timedelta(hours=h), "fp": round(base * f), "min": round(mpg - d)}
                for h, f, d in ((10, 0.55, 8), (34, 0.6, 7), (58, 0.62, 6), (82, 1.0, 0))]
        best = fas[0] if fas else None
        c = extras.cold_alerts({p0["id"]: fake}, players, now, best)
        if c:
            msgs.append(T.format("jogador seu esfriando (simulado, números inventados)") + c[0][1])

    # 7) league trade between two other teams, lopsided
    others = {}
    for p in players.values():
        t = (p["fantasy_team"] or "").strip()
        if t and t.lower() != config.MY_TEAM_NAME.lower():
            others.setdefault(t, []).append(p)
    teams = sorted(others)[:2]
    if len(teams) == 2:
        a = max(others[teams[0]], key=lambda p: p["value"])
        b = min([p for p in others[teams[1]] if p["value"] > 0], key=lambda p: p["value"])
        act = [{"date": 9e15, "team": teams[1], "action": "TRADED", "player": a["name"]},
               {"date": 9e15, "team": teams[0], "action": "TRADED", "player": b["name"]}]
        lt = extras.league_trade_alerts(act, players, since_ms=0)
        if lt:
            msgs.append(T.format("troca entre outros managers (simulada)") + lt[0][1])

    # 6) last day push: pretend today is the last day of a close matchup
    lctx = {**ctx, "periods": [p for p in ctx["periods"] if p <= ctx["today"]], "adds_left": max(ctx["adds_left"], 2)}
    ld = extras.last_day_push(lctx, diff=-25, total=1350)
    if ld:
        msgs.append(T.format("último dia da rodada (simulado: como se hoje fosse domingo, perdendo por 25)") + ld)

    bot.send_alert(f"🧪 *Testes dos alertas* — {len(msgs)} mensagens a seguir. Simulados estão marcados; nada disso é um alerta real.")
    for m in msgs:
        print("---\n" + m + "\n")
        bot.send_alert(m)
        time.sleep(1)
    bot.send_alert("🧪 Fim dos testes.")


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

    # ---- week planning preview (ignores time-of-day windows) --------------
    print("## Prévia do planejamento da semana\n")
    try:
        state = {"players": players}
        ctx = bot.build_context(snap, state, now)
        print(f"(dia {ctx['today']}, rodada {ctx['matchup']}: períodos {ctx['periods']}, adversário {ctx['opponent']}, adds restantes {ctx['adds_left']})\n")
        plan = bot.planner.streaming_plan(ctx["roster"], ctx["fas"], ctx["remaining"], ctx["sched"], ctx["pdates"], ctx["adds_left"], ctx["now_ms"])
        print(bot.planner.render_plan(plan, ctx["pdates"], ctx["adds_left"], "🗓️ *Plano da rodada*"))
        diag = bot.diag_message(ctx, ctx["remaining"])
        planmsg = bot.plan_message(ctx, plan)
        print("\n" + diag + "\n\n" + planmsg)
        if os.environ.get("SEND_WEEK") == "true":
            bot.send_alert(diag)
            bot.send_alert(planmsg)
            print("(diagnóstico e plano enviados ao Telegram)")
        per_day = []
        for per in ctx["remaining"]:
            n = sum(bot.planner.plays(p, per, ctx["sched"], ctx["pdates"]) for p in ctx["roster"])
            per_day.append(f"{bot.planner.day_label(ctx['pdates'][per])}: {n}")
        print("Seus jogadores jogando por dia: " + ", ".join(per_day))
        issues = bot.planner.lineup_issues(ctx["roster"], ctx["today"], ctx["sched"], ctx["pdates"], ctx["now_ms"])
        print("\nChecagem de lineup hoje: " + ("\n" + "\n".join(issues) if issues else "ok"))
    except Exception as e:
        import traceback
        print("(prévia falhou)", repr(e)); traceback.print_exc(file=sys.stdout)
    print()

    print("## Prévia dos alertas semanais (não enviados)\n")
    try:
        st = {"players": players}
        ctx = bot.build_context(snap, st, now)
        for key, msg in bot.weekly_extras({**ctx, "matchup": ctx["matchup"]}):
            print(f"--- [{key}]\n{msg}\n")
        print("--- [diag]\n" + bot.diag_message(ctx, ctx["remaining"]) + "\n")
    except Exception as e:
        import traceback
        print("(prévia semanal falhou)", repr(e)); traceback.print_exc(file=sys.stdout)

    if os.environ.get("SEND_SAMPLES") == "true":
        try:
            send_samples(snap, players, now)
        except Exception as e:
            import traceback
            print("(amostras falharam)", repr(e)); traceback.print_exc(file=sys.stdout)

    if os.environ.get("SEND_FORECAST") == "true":
        try:
            st = {"players": players}
            ctx = bot.build_context(snap, st, now)
            msg = bot.forecast_message(snap, st, ctx, ahead=2)
            print("## Forecast\n\n" + msg + "\n")
            bot.send_alert(msg)
            print("(forecast enviado ao Telegram)")
        except Exception as e:
            import traceback
            print("(forecast falhou)", repr(e)); traceback.print_exc(file=sys.stdout)

    if os.environ.get("SEND_SEASON") == "true":
        try:
            outlook = bot.season_outlook(snap, {"players": players})
            print("## Temporada\n\n" + outlook + "\n")
            bot.send_alert(outlook)
            print("(temporada enviada ao Telegram)")
        except Exception as e:
            import traceback
            print("(temporada falhou)", repr(e)); traceback.print_exc(file=sys.stdout)

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
