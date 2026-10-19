"""Week planning for an H2H points league with daily lineups.

Pure functions (no ESPN calls), so they can be tested offline:
  - who plays which day, given the NBA schedule and injuries
  - lineup problems for today (starter not playing, bench player playing)
  - the streaming plan: which FA to rotate through a weak roster spot
  - projected end of the matchup
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import config

ET = timezone(timedelta(hours=-4))  # good enough for alert timing / day labels
WEEKDAYS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
STARTER_SLOTS = 10
BENCH_SLOTS = {"BE", "IR", ""}


# ---------------------------------------------------------------------------
# Who plays when
# ---------------------------------------------------------------------------

def period_dates(sched: dict, periods: list[int]) -> dict[int, date]:
    """Calendar day (ET) of each scoring period, read from any game that day."""
    out = {}
    for team_games in sched.values():
        for per, ms in team_games.items():
            if per in periods and per not in out:
                out[per] = datetime.fromtimestamp(ms / 1000, ET).date()
    return out


def week_days(sched: dict, weeks: list[int]) -> list[int]:
    """Scoring periods (days) belonging to these NBA fantasy weeks.

    ESPN's matchup settings list *weeks*, not days. Week 1 runs from opening
    night to the first Sunday; every later week is Monday-Sunday.
    """
    all_pers = sorted({per for team in sched.values() for per in team})
    if not all_pers:
        return []
    dates = period_dates(sched, all_pers)
    first = dates[all_pers[0]]
    week1_end = first + timedelta(days=(6 - first.weekday()))
    out = []
    for per in all_pers:
        d = dates[per]
        wk = 1 if d <= week1_end else 2 + (d - week1_end - timedelta(days=1)).days // 7
        if wk in weeks:
            out.append(per)
    return out


def day_label(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d}"


def available_on(p: dict, day: date) -> bool:
    """Healthy enough to play that day, judging by status and expected return."""
    if p.get("slot") == "IR":
        return False
    if p["status"] in config.OUT_STATUSES:
        if p.get("return_date"):
            return day >= date.fromisoformat(p["return_date"])
        return False
    return True


def plays(p: dict, per: int, sched: dict, pdates: dict, after_ms: int | None = None) -> bool:
    """Has a game in that period, is available, and (optionally) tips after a time."""
    ms = sched.get(p["nba_team"], {}).get(per)
    if ms is None or per not in pdates:
        return False
    if after_ms is not None and ms <= after_ms:
        return False
    return available_on(p, pdates[per])


# ---------------------------------------------------------------------------
# Lineup check for today (daily lineups, lock at each game's tip, no protection)
# ---------------------------------------------------------------------------

def lineup_issues(roster: list[dict], today: int, sched: dict, pdates: dict, now_ms: int) -> list[str]:
    """Swaps that gain points today: idle starters vs bench players with a game."""
    starters = [p for p in roster if p.get("slot") not in BENCH_SLOTS]
    bench = [p for p in roster if p.get("slot") == "BE"]

    def not_locked(p):
        ms = sched.get(p["nba_team"], {}).get(today)
        return ms is None or ms > now_ms

    idle = []
    for p in sorted(starters, key=lambda p: p["value"]):
        if not not_locked(p):
            continue
        if not sched.get(p["nba_team"], {}).get(today):
            idle.append((p, "sem jogo hoje"))
        elif not available_on(p, pdates[today]):
            idle.append((p, "OUT hoje"))
    options = sorted(
        [p for p in bench if plays(p, today, sched, pdates, after_ms=now_ms)],
        key=lambda p: p["value"], reverse=True,
    )
    lines = []
    for (p, why), sub in zip(idle, options):
        tip = datetime.fromtimestamp(sched[sub["nba_team"]][today] / 1000, ET)
        lines.append(f"• *{p['name']}* ({why}) → coloque *{sub['name']}* ({sub['value']} FP, joga {tip:%H:%M})")
    rest = idle[len(options):]
    out_today = [p for p, why in rest if why == "OUT hoje"]
    for p in out_today:
        lines.append(f"• *{p['name']}* está OUT e no lineup — nenhum reserva joga hoje")
    if lines or out_today:
        empty = len(rest)
        if empty:
            lines.append(f"{empty} vaga(s) de titular sem jogo hoje → espaço para um FA (veja o plano)")
    return lines


# ---------------------------------------------------------------------------
# Streaming plan
# ---------------------------------------------------------------------------

def _playing_count(roster: list[dict], periods: list[int], sched: dict, pdates: dict, exclude: set) -> dict[int, int]:
    return {
        per: sum(1 for p in roster if p["id"] not in exclude and plays(p, per, sched, pdates))
        for per in periods
    }


def best_rotation(holder0: dict, candidates: list[dict], periods: list[int], sched: dict,
                  pdates: dict, others_playing: dict[int, int], adds_left: int,
                  now_ms: int, add_cost: float) -> tuple[float, float, list[tuple[int, dict]]]:
    """DP over days: who should hold this roster spot each day.

    Returns (plan_points, baseline_points, swaps) where swaps = [(period, player_added)].
    A day only counts if fewer than 10 other players are playing (otherwise the
    spot wouldn't make the starting lineup anyway).
    """
    holders = [holder0] + candidates
    today = periods[0] if periods else None

    def pts(h: dict, per: int) -> float:
        if others_playing.get(per, 0) >= STARTER_SLOTS:
            return 0.0
        after = now_ms if (per == today and h is not holder0) else None
        return h["value"] if plays(h, per, sched, pdates, after_ms=after) else 0.0

    NEG = float("-inf")
    # dp[(holder_idx, adds)] = (score, path)
    dp = {(0, 0): (0.0, [])}
    for per in periods:
        nxt: dict = {}
        for (hi, a), (score, path) in dp.items():
            # keep the current holder
            key = (hi, a)
            val = score + pts(holders[hi], per)
            if val > nxt.get(key, (NEG,))[0]:
                nxt[key] = (val, path)
            # swap at the start of this day
            if a < adds_left:
                for ci in range(1, len(holders)):
                    if ci == hi:
                        continue
                    key = (ci, a + 1)
                    val = score - add_cost + pts(holders[ci], per)
                    if val > nxt.get(key, (NEG,))[0]:
                        nxt[key] = (val, path + [(per, ci)])
        dp = nxt
    (score, path) = max(dp.values(), key=lambda v: v[0])
    swaps = [(per, holders[ci]) for per, ci in path]
    plan_pts = score + add_cost * len(swaps)
    base = sum(pts(holder0, per) for per in periods)
    return plan_pts, base, swaps


def streaming_plan(roster: list[dict], fas: list[dict], periods: list[int], sched: dict,
                   pdates: dict, adds_left: int, now_ms: int) -> dict:
    """Plan for the weakest roster spots, one at a time, while adds last."""
    active = [p for p in roster if p.get("slot") != "IR"]
    week_pts = {p["id"]: p["value"] * sum(plays(p, per, sched, pdates) for per in periods) for p in active}
    droppable = sorted(
        [p for p in active if p["value"] < config.KEEP_FP],
        key=lambda p: week_pts[p["id"]],
    )
    worst = droppable[0] if droppable else None

    slots, used, taken = [], 0, set()
    replaced: set = set()
    extra_playing = {per: 0 for per in periods}
    for w in droppable[: config.MAX_STREAM_SLOTS]:
        if used >= adds_left:
            break
        others = _playing_count(active, periods, sched, pdates, exclude=replaced | {w["id"]})
        others = {per: others[per] + extra_playing[per] for per in periods}
        cands = [c for c in fas if c["id"] not in taken and c["value"] >= config.MIN_STREAMER_FP]
        plan_pts, base, swaps = best_rotation(
            w, cands, periods, sched, pdates, others, adds_left - used, now_ms, config.ADD_COST_FP)
        gain = plan_pts - base
        if not swaps or gain < config.MIN_STREAM_GAIN_FP:
            continue
        steps = []
        holder = w
        for i, (per, c) in enumerate(swaps):
            until = swaps[i + 1][0] if i + 1 < len(swaps) else None
            games = [q for q in periods if q >= per and (until is None or q < until)
                     and plays(c, q, sched, pdates, after_ms=now_ms if q == periods[0] else None)]
            steps.append({"period": per, "drop": holder, "add": c, "games": games})
            holder = c
            taken.add(c["id"])
        # the spot's new holders now count as playing for the next spot's math
        for st in steps:
            for q in st["games"]:
                extra_playing[q] += 1
        replaced.add(w["id"])
        used += len(swaps)
        slots.append({"worst": w, "base": round(base, 1), "plan": round(plan_pts, 1),
                      "gain": round(gain, 1), "steps": steps})
    return {"worst": worst, "worst_games": sum(plays(worst, per, sched, pdates) for per in periods) if worst else 0,
            "slots": slots, "adds_used": used}


def render_plan(plan: dict, pdates: dict, adds_left: int, title: str) -> str:
    w = plan["worst"]
    lines = [title]
    if w:
        lines.append(f"Seu pior jogador da semana: *{w['name']}* — {plan['worst_games']} jogos × {w['value']} = ~{plan['worst_games'] * w['value']:.0f} FP")
    if not plan["slots"]:
        lines.append("Nenhuma estratégia de FA ganha o suficiente esta semana. Segure o elenco.")
        return "\n".join(lines)
    total = sum(s["gain"] for s in plan["slots"])
    lines.append(f"Com esta estratégia: *+{total:.0f} FP* (usa {plan['adds_used']} de {adds_left} adds restantes)")
    for s in plan["slots"]:
        if len(plan["slots"]) > 1:
            lines.append(f"\n_Vaga do {s['worst']['name']}: +{s['gain']:.0f} FP_")
        for st in s["steps"]:
            days = ", ".join(day_label(pdates[q]) for q in st["games"]) or "—"
            lines.append(f"• *{day_label(pdates[st['period']])}*: dropar {st['drop']['name']} → pegar *{st['add']['name']}* "
                         f"({st['add']['nba_team']}, {st['add']['value']} FP; joga {days})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Matchup projection
# ---------------------------------------------------------------------------

def projected_remaining(roster: list[dict], periods: list[int], sched: dict, pdates: dict,
                        now_ms: int) -> tuple[float, int]:
    """Expected points still to come (top 10 players each day) and games left."""
    total, games = 0.0, 0
    for i, per in enumerate(periods):
        after = now_ms if i == 0 else None
        vals = sorted([p["value"] for p in roster if p.get("slot") != "IR"
                       and plays(p, per, sched, pdates, after_ms=after)], reverse=True)[:STARTER_SLOTS]
        total += sum(vals)
        games += len(vals)
    return round(total, 1), games
