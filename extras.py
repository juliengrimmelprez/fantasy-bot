"""Extra alerts: returning free agents, cooling players, trade ideas, playoff
race, last-day push, trades between other managers, weekly recap.

Pure functions on plain dicts, so they can be tested offline.
"""

from __future__ import annotations

from datetime import date, datetime

import config
import planner

ME = config.MY_TEAM_NAME.lower()


def _mine(p: dict) -> bool:
    return (p.get("fantasy_team") or "").strip().lower() == ME


def _is_out(status: str) -> bool:
    return status in config.OUT_STATUSES


# ---------------------------------------------------------------------------
# 1) Free agent back from injury
# ---------------------------------------------------------------------------

def returning_fa_alerts(old: dict, new: dict, worst_value: float | None) -> list[tuple[str, str]]:
    """A free agent who was OUT is cleared to play and is better than your worst player."""
    out = []
    bar = max(config.RETURN_MIN_FP, worst_value or 0)
    for pid, p in new.items():
        prev = old.get(pid)
        if not prev or p["fantasy_team"] is not None or p["nba_team"] == "FA":
            continue  # unowned, and signed with an NBA team
        if not _is_out(prev["status"]) or _is_out(p["status"]):
            continue
        ref = max(p.get("season_avg") or 0, p["value"])
        if ref < bar:
            continue
        msg = (f"🩹 *Jogador livre voltou de lesão: {p['name']}* ({p['nba_team']}, {'/'.join(p['positions'])})\n"
               f"Status: {prev['status'].replace('_', ' ').title()} → *{p['status'].replace('_', ' ').title()}*\n"
               f"Média esperada: {p['value']} FP (temporada {p.get('season_avg') or 0:g})")
        if worst_value is not None:
            msg += f"\nSeu pior jogador hoje: {worst_value} FP"
        msg += "\nPrimeiros jogos costumam ter minutos limitados." + f"\n{config.ADD_PLAYERS_URL}"
        out.append((f"back:{pid}:{date.today():%Y%m%d}", msg))
    return out


# ---------------------------------------------------------------------------
# 2) Your player cooling off
# ---------------------------------------------------------------------------

def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def cold_alerts(games_by_pid: dict, players: dict, now: datetime, best_fa: dict | None) -> list[tuple[str, str]]:
    """Your player's last-3 average fell well below his norm (fires when it crosses)."""
    out = []
    n = config.COLD_AVG_GAMES
    for pid, games in games_by_pid.items():
        p = players.get(pid)
        if not p or not _mine(p) or p.get("slot") == "IR":
            continue
        done = [g for g in games if (now - g["date"]).total_seconds() >= config.HOT_GAME_DONE_HOURS * 3600]
        if len(done) < n + 1:
            continue
        base = p.get("season_avg") or p["value"]
        if not base or base < config.COLD_MIN_BASE_FP:
            continue
        avg_now = _avg([g["fp"] for g in done[:n]])
        avg_before = _avg([g["fp"] for g in done[1:n + 1]])
        bar = base * config.COLD_RATIO
        min_now = _avg([g["min"] for g in done[:n]])
        min_drop = (p.get("mpg") or min_now) - min_now
        crossed = avg_now < bar <= avg_before
        if not crossed:
            continue
        recent = ", ".join(f"{g['fp']:g} ({g['min']} min)" for g in done[:n])
        msg = (f"🥶 *{p['name']} esfriando* ({p['nba_team']})\n"
               f"Média dos últimos {n}: *{avg_now:.1f} FP* vs {base:g} na temporada\n"
               f"Jogos: {recent}")
        if min_drop >= config.COLD_MIN_DROP:
            msg += f"\n⚠️ Minutos caindo: {min_now:.0f} vs {p.get('mpg') or 0:.0f} de média — pode ter perdido espaço"
        if best_fa and best_fa["value"] > avg_now:
            msg += f"\nMelhor FA disponível: *{best_fa['name']}* ({best_fa['nba_team']}, {best_fa['value']} FP)"
        msg += "\nUm jogo ruim não decide: avalie se é fase ou mudança de papel."
        out.append((f"cold:{pid}:{done[0]['date']:%Y%m%d}", msg))
    return out


# ---------------------------------------------------------------------------
# 4) Trade ideas (sell high / buy low)
# ---------------------------------------------------------------------------

def trade_ideas(players: dict, today: date) -> str | None:
    if today > date.fromisoformat(config.TRADE_DEADLINE):
        return None
    sell, buy = [], []
    for p in players.values():
        sa, l15 = p.get("season_avg") or 0, p.get("last15") or 0
        if not sa or not l15:
            continue
        if _mine(p) and l15 >= sa * config.SELL_HIGH_RATIO and l15 - sa >= config.SELL_HIGH_MIN_DIFF:
            sell.append((l15 - sa, p))
        elif (p["fantasy_team"] and not _mine(p) and sa >= config.BUY_LOW_MIN_FP
              and l15 <= sa * config.BUY_LOW_RATIO and not _is_out(p["status"])):
            buy.append((sa - l15, p))
    # stars returning soon on other teams
    for p in players.values():
        if (p["fantasy_team"] and not _mine(p) and _is_out(p["status"]) and p.get("return_date")
                and (p.get("season_avg") or p["value"]) >= config.BUY_LOW_MIN_FP):
            days = (date.fromisoformat(p["return_date"]) - today).days
            if 0 <= days <= 21:
                buy.append((50 - days, {**p, "_returning": days}))
    if not sell and not buy:
        return None
    lines = [f"🤝 *Ideias de troca* (prazo da liga: {date.fromisoformat(config.TRADE_DEADLINE):%d/%m})"]
    if sell:
        lines.append("\n*Vender em alta* (seus jogadores acima do normal):")
        for _, p in sorted(sell, key=lambda x: x[0], reverse=True)[:4]:
            lines.append(f"• {p['name']} — últimos 15d {p['last15']:g} vs temporada {p['season_avg']:g}")
    if buy:
        lines.append("\n*Comprar em baixa* (de outros times):")
        for _, p in sorted(buy, key=lambda x: x[0], reverse=True)[:5]:
            if "_returning" in p:
                why = f"lesionado, volta em ~{p['_returning']} dias; dono pode vender barato"
            else:
                why = f"últimos 15d {p['last15']:g} vs temporada {p['season_avg']:g}"
            lines.append(f"• {p['name']} ({p['fantasy_team'].strip()}) — {why}")
    lines.append("_Sinais estatísticos, não certezas: confira o contexto (lesão, papel no time)._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5) Playoff race
# ---------------------------------------------------------------------------

def playoff_race(teams: list[dict], team_sched: dict, current_matchup: int, n_regular: int) -> str | None:
    """Standings, gap to the last playoff spot, and remaining schedule strength."""
    if not teams:
        return None
    me = next((t for t in teams if t["name"].strip().lower() == ME), None)
    if not me:
        return None
    ranked = sorted(teams, key=lambda t: (t["standing"] or 99))
    played = max(1, current_matchup - 1)
    ppw = {t["name"]: t["points_for"] / played for t in teams}
    league_ppw = sum(ppw.values()) / len(ppw)
    cut = ranked[config.PLAYOFF_TEAMS - 1] if len(ranked) >= config.PLAYOFF_TEAMS else ranked[-1]
    first_out = ranked[config.PLAYOFF_TEAMS] if len(ranked) > config.PLAYOFF_TEAMS else None
    opps = [o for i, o in enumerate(team_sched.get(me["name"], []), 1) if current_matchup <= i <= n_regular and o]
    sos = sum(ppw.get(o, league_ppw) for o in opps) / len(opps) if opps else None

    lines = [f"🏁 *Corrida pelos playoffs* (top {config.PLAYOFF_TEAMS} de {len(teams)})",
             f"Você: *{me['standing']}º* — {me['wins']}-{me['losses']}" + (f"-{me['ties']}" if me["ties"] else "")
             + f" · {me['points_for']:.0f} pts ({ppw[me['name']]:.0f}/rodada)"]
    if me["standing"] <= config.PLAYOFF_TEAMS and first_out:
        lines.append(f"Folga sobre o 1º fora ({first_out['name'].strip()}, {first_out['wins']}-{first_out['losses']}): "
                     f"{me['wins'] - first_out['wins']:+d} vitórias")
    else:
        lines.append(f"Distância para o {config.PLAYOFF_TEAMS}º ({cut['name'].strip()}, {cut['wins']}-{cut['losses']}): "
                     f"{cut['wins'] - me['wins']} vitórias")
    if sos:
        tag = "mais difícil" if sos > league_ppw * 1.02 else ("mais fácil" if sos < league_ppw * 0.98 else "na média")
        lines.append(f"Agenda restante ({len(opps)} rodadas): adversários fazem {sos:.0f}/rodada vs {league_ppw:.0f} da liga → *{tag}*")
    lines.append("")
    for t in ranked:
        mark = "👉 " if t is me else ""
        line = f"{mark}{t['standing']}. {t['name'].strip()} {t['wins']}-{t['losses']} · {t['points_for']:.0f}"
        lines.append(line)
        if t["standing"] == config.PLAYOFF_TEAMS:
            lines.append("— linha dos playoffs —")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6) Last day of the matchup
# ---------------------------------------------------------------------------

def last_day_push(ctx: dict, diff: float, total: float) -> str | None:
    """Close matchup on its last day with adds left: who to stream today."""
    today, sched, pdates, now_ms = ctx["today"], ctx["sched"], ctx["pdates"], ctx["now_ms"]
    if not ctx["periods"] or today != ctx["periods"][-1] or ctx["adds_left"] <= 0:
        return None
    if abs(diff) > config.LAST_DAY_CLOSE_RATIO * max(total, 1):
        return None
    playing = sum(planner.plays(p, today, sched, pdates, after_ms=now_ms) for p in ctx["roster"])
    free_spots = planner.STARTER_SLOTS - playing
    if free_spots <= 0:
        return None
    fas = [p for p in ctx["fas"] if planner.plays(p, today, sched, pdates, after_ms=now_ms)][:5]
    if not fas:
        return None
    droppable = sorted([p for p in ctx["roster"] if p.get("slot") != "IR" and p["value"] < config.KEEP_FP
                        and not planner.plays(p, today, sched, pdates, after_ms=now_ms)], key=lambda p: p["value"])
    lines = [f"⏳ *Último dia da rodada* — projeção {diff:+.0f}, jogo apertado",
             f"Você tem *{free_spots}* vaga(s) de titular sem jogo hoje e {ctx['adds_left']} adds.",
             "Melhores FAs que jogam hoje:"]
    for p in fas:
        tip = datetime.fromtimestamp(sched[p["nba_team"]][today] / 1000, planner.ET)
        lines.append(f"• *{p['name']}* ({p['nba_team']}, {p['value']} FP, {tip:%H:%M})")
    if droppable:
        lines.append("Pode dropar (sem jogo hoje, abaixo de 30 FP): " + ", ".join(p["name"] for p in droppable[:3]))
    lines.append(config.ADD_PLAYERS_URL)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7) Trades between managers
# ---------------------------------------------------------------------------

def league_trade_alerts(activity: list[dict], players: dict, since_ms: int) -> list[tuple[str, str]]:
    by_name = {p["name"]: p for p in players.values()}
    groups: dict[int, dict[str, list[str]]] = {}
    for a in activity:
        if a["action"] != "TRADED" or a["date"] <= since_ms:
            continue
        groups.setdefault(a["date"], {}).setdefault(a["team"].strip(), []).append(a["player"])
    out = []
    for when, sides in groups.items():
        if any(t.lower() == ME for t in sides):
            continue  # your own trade: you already know
        parts, totals = [], []
        for team, names in sides.items():
            vals = [by_name.get(n, {}).get("value", 0) for n in names]
            totals.append(sum(vals))
            parts.append(f"*{team}* recebe: " + ", ".join(f"{n} ({v:g})" for n, v in zip(names, vals))
                         + f" = {sum(vals):.0f} FP/jogo")
        msg = "🔀 *Troca na liga*\n" + "\n".join(parts)
        if len(totals) == 2 and min(totals) > 0 and max(totals) / min(totals) >= config.LOPSIDED_RATIO:
            msg += (f"\n⚖️ Desequilibrada ({max(totals):.0f} × {min(totals):.0f}). "
                    "Veto precisa de 6 votos, dentro de 1 dia de revisão.")
        msg += "\n_Valor = soma das médias esperadas; não considera elenco e necessidades de cada time._"
        out.append((f"ltrade:{when}", msg))
    return out


# ---------------------------------------------------------------------------
# 8) Weekly recap
# ---------------------------------------------------------------------------

def recap_message(matchup: int, opponent: str, my_score: float, opp_score: float,
                  lines_mine: list[tuple[str, float]], expected: dict[str, float]) -> str:
    res = "✅ Vitória" if my_score > opp_score else ("❌ Derrota" if my_score < opp_score else "➖ Empate")
    out = [f"📒 *Resumo da Rodada {matchup}* vs {opponent}",
           f"{res}: *{my_score:.0f} × {opp_score:.0f}* ({my_score - opp_score:+.0f})"]
    if lines_mine:
        ranked = sorted(lines_mine, key=lambda x: x[1], reverse=True)
        out.append("\n*Pontos por jogador:*")
        for name, pts in ranked:
            exp = expected.get(name)
            delta = f" ({pts - exp:+.0f} vs esperado)" if exp is not None else ""
            out.append(f"• {name}: {pts:.0f}{delta}")
        deltas = [(n, pts - expected[n]) for n, pts in lines_mine if n in expected]
        if deltas:
            best = max(deltas, key=lambda x: x[1])
            worst = min(deltas, key=lambda x: x[1])
            out.append(f"\nAcima do esperado: *{best[0]}* ({best[1]:+.0f}) · Abaixo: *{worst[0]}* ({worst[1]:+.0f})")
    return "\n".join(out)
