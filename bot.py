import os
import math
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Основная лига.
# Можно поменять через Render переменную SPORT.
SPORT = os.getenv("SPORT", "soccer_epl")

REGIONS = "eu"
MARKETS = "h2h"

CHECK_EVERY_MINUTES = 15

MIN_MINUTE = 10
MAX_MINUTE = 80

VALUE_THRESHOLD = 0.04
KELLY_FRACTION = 0.15

BANKROLL_START = 100.0

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not found")

if not ODDS_API_KEY:
    raise RuntimeError("ODDS_API_KEY not found")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================================================
# STATE
# =========================================================

state = {
    "bankroll": BANKROLL_START,
    "signals_sent": 0,
    "bets_placed": 0,
    "total_staked": 0.0,
    "wins": 0,
    "losses": 0,
    "scans": 0,
}

subscribers = set()

live_cache = {}

sent_signals = set()


# =========================================================
# TELEGRAM KEYBOARD
# =========================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔴 LIVE"],
        ["📅 МАТЧИ СЕГОДНЯ"],
        ["📊 СТАТУС"],
    ],
    resize_keyboard=True,
)


# =========================================================
# ODDS API
# =========================================================

async def odds_request(endpoint, params=None):
    if params is None:
        params = {}

    params["apiKey"] = ODDS_API_KEY

    url = f"{ODDS_API_BASE}{endpoint}"

    try:
        timeout = aiohttp.ClientTimeout(total=20)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:

                if response.status != 200:
                    text = await response.text()

                    logging.error(
                        "Odds API %s: %s",
                        response.status,
                        text[:500]
                    )

                    return None

                data = await response.json()

                remaining = response.headers.get(
                    "x-requests-remaining",
                    "?"
                )

                logging.info(
                    "Odds API OK | remaining=%s",
                    remaining
                )

                return data

    except Exception as e:
        logging.error("Odds API error: %s", e)
        return None


async def fetch_odds():
    return await odds_request(
        f"/sports/{SPORT}/odds",
        {
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
    )


# =========================================================
# MATCH TIME
# =========================================================

def parse_time(value):
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except Exception:
        return None


def is_live(match):
    start = parse_time(match.get("commence_time", ""))

    if not start:
        return False

    now = datetime.now(timezone.utc)

    # The Odds API considers games with commence_time
    # before now as in-play while odds are available.
    if start > now:
        return False

    # Не держим матч бесконечно.
    age_minutes = (now - start).total_seconds() / 60

    return 0 <= age_minutes <= 150


# =========================================================
# ODDS PARSER
# =========================================================

def parse_match_odds(match):

    home = match.get("home_team")
    away = match.get("away_team")

    home_odds = []
    draw_odds = []
    away_odds = []

    for bookmaker in match.get("bookmakers", []):

        for market in bookmaker.get("markets", []):

            if market.get("key") != "h2h":
                continue

            for outcome in market.get("outcomes", []):

                name = outcome.get("name")
                price = outcome.get("price")

                if not price:
                    continue

                if name == home:
                    home_odds.append(float(price))

                elif name == away:
                    away_odds.append(float(price))

                elif str(name).lower() == "draw":
                    draw_odds.append(float(price))

    def average(values):
        if not values:
            return None

        return sum(values) / len(values)

    return {
        "HOME": average(home_odds),
        "DRAW": average(draw_odds),
        "AWAY": average(away_odds),
    }


# =========================================================
# FLASHSCORE
# =========================================================

async def fetch_flashscore_live():

    result = {}

    try:

        from flashscore import FlashscoreApi

        def load():

            data = {}

            try:

                api = FlashscoreApi()
                matches = api.get_today_matches()

                for match in matches:

                    try:
                        match.load_content()
                    except Exception:
                        continue

                    home = getattr(
                        match,
                        "home_team_name",
                        None
                    )

                    away = getattr(
                        match,
                        "away_team_name",
                        None
                    )

                    if not home or not away:
                        continue

                    key = (
                        f"{home}-{away}"
                        .lower()
                    )

                    data[key] = {
                        "minute": (
                            getattr(
                                match,
                                "minute",
                                0
                            ) or 0
                        ),

                        "score": (
                            getattr(
                                match,
                                "home_team_score",
                                0
                            ) or 0,

                            getattr(
                                match,
                                "away_team_score",
                                0
                            ) or 0,
                        ),

                        "red_home": 0,
                        "red_away": 0,
                    }

            except Exception as e:
                logging.error(
                    "Flashscore load error: %s",
                    e
                )

            return data

        result = await asyncio.to_thread(load)

    except ImportError:

        logging.warning(
            "fs-football-fork is not installed"
        )

    except Exception as e:

        logging.error(
            "Flashscore error: %s",
            e
        )

    return result


def find_flashscore_match(match, flash_data):

    home = (
        match.get("home_team", "")
        .lower()
    )

    away = (
        match.get("away_team", "")
        .lower()
    )

    for key, value in flash_data.items():

        if home.split()[0] in key and away.split()[0] in key:
            return value

    return None


# =========================================================
# POISSON MODEL
# =========================================================

def poisson_prob(lam, k):

    if lam <= 0:
        return 1.0 if k == 0 else 0.0

    return (
        math.exp(-lam)
        * (lam ** k)
        / math.factorial(k)
    )


def calculate_live_intensity(
    base_home,
    base_away,
    minute,
    score_diff,
    red_home,
    red_away,
):

    time_left = max(
        0,
        90 - minute
    ) / 90.0

    if score_diff < 0:

        base_home *= 1.15
        base_away *= 0.90

    elif score_diff > 0:

        base_home *= 0.90
        base_away *= 1.15

    base_home *= (
        0.75 ** red_home
    )

    base_away *= (
        0.75 ** red_away
    )

    lam_home = max(
        0.05,
        base_home * time_left * 1.10
    )

    lam_away = max(
        0.05,
        base_away * time_left * 0.90
    )

    return lam_home, lam_away


def match_probabilities(
    lam_home,
    lam_away,
    max_goals=6,
):

    home = 0.0
    draw = 0.0
    away = 0.0

    for i in range(max_goals + 1):

        for j in range(max_goals + 1):

            probability = (
                poisson_prob(
                    lam_home,
                    i
                )
                *
                poisson_prob(
                    lam_away,
                    j
                )
            )

            if i > j:
                home += probability

            elif i == j:
                draw += probability

            else:
                away += probability

    total = home + draw + away

    if total <= 0:
        return None

    return {
        "HOME": home / total,
        "DRAW": draw / total,
        "AWAY": away / total,
    }


def kelly_stake(
    probability,
    odds,
    bankroll,
):

    b = odds - 1

    if b <= 0:
        return 0.0

    q = 1 - probability

    kelly = (
        b * probability - q
    ) / b

    stake = (
        bankroll
        * max(0, kelly)
        * KELLY_FRACTION
    )

    return round(stake, 2)


# =========================================================
# ANALYSIS
# =========================================================

def analyse_match(
    match,
    flash,
):

    parsed = parse_match_odds(match)

    if not parsed["HOME"]:
        return None

    if not parsed["DRAW"]:
        return None

    if not parsed["AWAY"]:
        return None

    # Если Flashscore нашёл LIVE данные
    if flash:

        minute = flash["minute"]

        home_goals, away_goals = (
            flash["score"]
        )

        if (
            minute < MIN_MINUTE
            or minute > MAX_MINUTE
        ):
            return None

        score_diff = (
            home_goals
            - away_goals
        )

        red_home = flash["red_home"]
        red_away = flash["red_away"]


