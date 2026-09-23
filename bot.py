import os
import json
import time
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


# =========================================================
# НАСТРОЙКИ
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
PITCH_API_KEY = os.getenv("PITCH_API_KEY") or os.getenv("FOOTBALL_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN / BOT_TOKEN not found")

if not PITCH_API_KEY:
    raise RuntimeError("PITCH_API_KEY / FOOTBALL_API_KEY not found")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/"
PITCH_API = "https://api.pitchapi.dev/v1"

CHECK_EVERY_SECONDS = 120
MIN_MINUTE = 5
MAX_MINUTE = 85

MIN_XG = 0.55
MIN_SHOTS = 6
MIN_ON_TARGET = 2

MIN_PROBABILITY = 0.60
MIN_ADVANTAGE = 0.15
MIN_FILTERS = 4


# =========================================================
# СОСТОЯНИЕ
# =========================================================

state = {
    "scans": 0,
    "signals": 0,
    "bankroll": 1000.0
}

subscribers = set()
sent_signals = set()


# =========================================================
# HTTP
# =========================================================

def get_json(url, headers=None):
    request = urllib.request.Request(
        url,
        headers=headers or {}
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def pitch(endpoint, params=None):
    url = PITCH_API + endpoint

    if params:
        url += "?" + urllib.parse.urlencode(params)

    try:
        result = get_json(
            url,
            {
                "X-API-KEY": PITCH_API_KEY
            }
        )

        return result.get("data")

    except Exception as e:
        print("PITCH ERROR:", endpoint, str(e))
        return None


# =========================================================
# TELEGRAM
# =========================================================

def telegram(method, data=None):
    url = TELEGRAM_API + method

    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=body
        )
    else:
        request = urllib.request.Request(url)

    with urllib.request.urlopen(request, timeout=35) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if keyboard:
        data["reply_markup"] = json.dumps(
            keyboard,
            ensure_ascii=False
        )

    try:
        telegram("sendMessage", data)
    except Exception as e:
        print("TELEGRAM ERROR:", str(e))


def answer_callback(callback_id):
    try:
        telegram(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id
            }
        )
    except Exception as e:
        print("CALLBACK ERROR:", str(e))


# =========================================================
# КЛАВИАТУРЫ
# =========================================================

def main_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🔴 LIVE",
                    "callback_data": "live"
                }
            ],
            [
                {
                    "text": "📅 МАТЧИ СЕГОДНЯ",
                    "callback_data": "today"
                }
            ],
            [
                {
                    "text": "📊 СТАТУС",
                    "callback_data": "status"
                }
            ]
        ]
    }


def match_keyboard(match_id):
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🔄 АНАЛИЗ",
                    "callback_data": f"match:{match_id}"
                }
            ],
            [
                {
                    "text": "🔴 LIVE",
                    "callback_data": "live"
                }
            ],
            [
                {
                    "text": "🏠 МЕНЮ",
                    "callback_data": "home"
                }
            ]
        ]
    }


# =========================================================
# ВСПОМОГАТЕЛЬНЫЕ
# =========================================================

def num(value):
    try:
        if value is None:
            return 0.0

        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip()

        if "(" in text:
            text = text.split("(")[0].strip()

        return float(text)

    except Exception:
        return 0.0


def team_name(team):
    if isinstance(team, dict):
        return (
            team.get("name")
            or team.get("short_name")
            or team.get("title")
            or "Команда"
        )

    return str(team or "Команда")


def get_minute(match):
    value = match.get("minute")

    if value is not None:
        try:
            return int(num(value))
        except Exception:
            pass

    time_utc = (
        match.get("time_utc")
        or match.get("kickoff")
        or match.get("start_time")
    )

    if not time_utc:
        return 0

    try:
        kickoff = datetime.fromisoformat(
            str(time_utc).replace("Z", "+00:00")
        )

        now = datetime.now(timezone.utc)

        minutes = (
            now - kickoff
        ).total_seconds() / 60

        return max(0, int(minutes))

    except Exception:
        return 0


def match_id(match):
    return (
        match.get("id")
        or match.get("match_id")
    )


def get_score(match):
    home = (
        match.get("score_home")
        or match.get("home_score")
        or 0
    )

    away = (
        match.get("score_away")
        or match.get("away_score")
        or 0
    )

    return int(num(home)), int(num(away))


def get_teams(match):
    home_team = match.get("home_team", {})
    away_team = match.get("away_team", {})

    return (
        team_name(home_team),
        team_name(away_team)
    )


# =========================================================
# МАТЧИ
# =========================================================

def get_today_matches():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    data = pitch(
        f"/date/{today}",
        {
            "status": "all"
        }
    )

    if not data:
        return []

    if isinstance(data, list):
        return data

    return data.get("matches", [])


def is_live_status(status):
    status = str(status or "").lower()

    finished = (
        "finished",
        "not_started",
        "cancelled",
        "postponed",
        "scheduled",
        "fixture"
    )

    for item in finished:
        if status == item:
            return False

    return True


def get_live_matches():
    matches = get_today_matches()
    result = []

    for match in matches:
        status = match.get("status", "")

        if not is_live_status(status):
            continue

        minute = get_minute(match)

        if 0 <= minute <= 130:
            match["_minute"] = minute
            result.append(match)

    return result


# =========================================================
# PITCHAPI
# =========================================================

def get_match(match_id_value):
    return pitch(f"/matches/{match_id_value}")


def get_stats(match_id_value):
    return pitch(f"/matches/{match_id_value}/stats")


def get_shots(match_id_value):
    return pitch(f"/matches/{match_id_value}/shots")


def get_events(match_id_value):
    return pitch(f"/matches/{match_id_value}/events")


def get_momentum(match_id_value):
    return pitch(f"/matches/{match_id_value}/momentum")


# =========================================================
# СТАТИСТИКА
# =========================================================

def parse_stats(data):
    result = {
        "home_xg": 0.0,
        "away_xg": 0.0,
        "home_shots": 0,
        "away_shots": 0,
        "home_target": 0,
        "away_target": 0
    }

    if not data:
        return result

    periods = data.get("periods", [])

    for period in periods:
        period_name = str(
            period.get("period", "")
        ).lower()

        if period_name not in ("all", "match", "full"):
            continue

        for group in period.get("groups", []):
            for item in group.get("items", []):
                key = str(
                    item.get("key", "")
                ).lower()

                home = num(item.get("home"))
                away = num(item.get("away"))

                if key in ("expected_goals", "xg"):
                    result["home_xg"] = home
                    result["away_xg"] = away

                elif key in ("total_shots", "shots"):
                    result["home_shots"] = int(home)
                    result["away_shots"] = int(away)

                elif key in (
                   
