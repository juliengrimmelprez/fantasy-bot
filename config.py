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

# Only alert on other teams' injuries when the best free replacement, *with
# the extra minutes he should get*, beats your worst player minus this margin.
ACTIONABLE_ONLY = True
ACTIONABLE_MARGIN_FP = 0.0

# --- Who absorbs the minutes of an absent player ------------------------------
# Share of the freed minutes by position distance (PG=1 ... C=5), so a PG out
# mostly helps guards, but teams also slide players over (SF->SG, PF->SF, new
# PF in), so farther positions still get a little.
POS_WEIGHTS = [1.0, 0.6, 0.25, 0.1, 0.05]   # distance 0, 1, 2, 3, 4
MAX_MPG = 36                                # nobody goes above this
DEFAULT_MPG = 20                            # when ESPN has no minutes yet
DEFAULT_OUT_MPG = 30                        # minutes freed by a starter

# Absences at least this long get the "replacement is worth it for weeks" flag.
LONG_ABSENCE_DAYS = 14

# --- Hot free agents ---------------------------------------------------------
# A "good game" is at least this many fantasy points.
HOT_GAME_FP = 30.0
# Individual 🔥 alert: moving average of the last HOT_AVG_GAMES games reaches
# HOT_GAME_FP (needs at least HOT_MIN_GAMES games). Fires when it crosses.
HOT_AVG_GAMES = 3
HOT_MIN_GAMES = 2
# Single big games don't get their own alert; they go in the hourly digest.
HOT_SINGLE_GAME_FP = None
# Hourly digest: every free agent who finished a game with HOT_GAME_FP or more.
DIGEST_EVERY_MINUTES = 60
# Only games that started at least this many hours ago count (finished).
HOT_GAME_DONE_HOURS = 3
# Ignore streaks whose last game is older than this.
HOT_FRESH_HOURS = 36
# Check per-game stats at most every N minutes (it's a heavier request).
HOT_CHECK_MINUTES = 15

# --- Week planning (streaming, lineup, matchup) ---------------------------------
MATCHUP_ADD_LIMIT = 10      # league rule: 10 acquisitions per matchup
KEEP_FP = 30.0              # never plan to drop players averaging this or more
MAX_STREAM_SLOTS = 2        # how many weak roster spots to rotate at most
ADD_COST_FP = 3.0           # an add must earn at least this (adds are scarce)
MIN_STREAM_GAIN_FP = 8.0    # don't suggest a plan that gains less than this
STREAM_CANDIDATES = 40      # best healthy FAs considered for the plan
MIN_STREAMER_FP = 18.0      # don't burn an add on anyone below this
PLAN_HOUR_ET = 10           # weekly plan / daily step reminder after this hour
LINEUP_CHECK_MIN_BEFORE = (90, 20)   # window before the day's first tip-off
WAIVER_HOURS = 26           # recently dropped players are on waivers this long

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
