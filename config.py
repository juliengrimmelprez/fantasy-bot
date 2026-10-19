"""Settings you can tune without touching the logic.

Fantasy points (FP) use your league's own scoring, because ESPN computes
the averages and projections with Boom Shakalaka's settings.
"""

LEAGUE_ID = 41791233
SEASON = 2027          # ESPN names the 2026-27 season "2027"
MY_TEAM_NAME = "King Coopa"
LEAGUE_URL = f"https://fantasy.espn.com/basketball/league?leagueId={LEAGUE_ID}"
ADD_PLAYERS_URL = f"https://fantasy.espn.com/basketball/players/add?leagueId={LEAGUE_ID}"

# --- Who counts as a "good player" whose absence matters -------------------
# A player whose value (FP per game) is at or above this triggers an injury
# alert even when he isn't on your team, as long as a free teammate benefits.
STAR_MIN_FP = 30.0

# --- Replacement suggestions ----------------------------------------------
# A free agent must be worth at least this to be suggested as a pickup.
BENEFICIARY_MIN_FP = 15.0
# How many free agents to pull from ESPN each run (sorted by % owned).
FREE_AGENT_POOL = 250
# How many replacement names to show per alert.
MAX_SUGGESTIONS = 2

# Only alert on other teams' injuries when the best free replacement is at
# least as good as your worst player minus this margin (0 = strictly useful).
ACTIONABLE_ONLY = True
ACTIONABLE_MARGIN_FP = 0.0

# Absences at least this long get the "replacement is worth it for weeks" flag.
LONG_ABSENCE_DAYS = 14

# --- Hot free agents ---------------------------------------------------------
# A "good game" is at least this many fantasy points.
HOT_GAME_FP = 30.0
# Consecutive good games needed for a 🔥 alert (2-3 = more confidence).
HOT_MIN_STREAK = 2
# A single game this big alerts too, flagged as a possible fluke.
HOT_SINGLE_GAME_FP = 45.0
# Only games that started at least this many hours ago count (finished).
HOT_GAME_DONE_HOURS = 3
# Ignore streaks whose last game is older than this.
HOT_FRESH_HOURS = 36
# Check per-game stats at most every N minutes (it's a heavier request).
HOT_CHECK_MINUTES = 15

# --- Drop alerts ------------------------------------------------------------
# Alert when a dropped player's value beats your worst rostered player
# (IR slot excluded) by at least this margin.
DROP_MARGIN_FP = 0.0

# --- NBA trades / team changes -------------------------------------------------
# Only alert on team changes for players at or above this value, or anyone
# on your roster.
TRADE_MIN_FP = 18.0

# --- Statuses that mean "won't play" -------------------------------------
OUT_STATUSES = {"OUT", "INJURY_RESERVE", "SUSPENSION"}

STATUS_LABELS = {
    "ACTIVE": "Ativo",
    "DAY_TO_DAY": "Day-to-day",
    "QUESTIONABLE": "Questionable",
    "PROBABLE": "Probable",
    "DOUBTFUL": "Doubtful",
    "GAME_TIME_DECISION": "Game-time decision",
    "OUT": "OUT",
    "INJURY_RESERVE": "OUT (longo prazo)",
    "SUSPENSION": "Suspenso",
}

# --- Error alerts ----------------------------------------------------------
# If ESPN fails (e.g. cookies expired), warn at most once per this many hours.
ERROR_ALERT_HOURS = 6
