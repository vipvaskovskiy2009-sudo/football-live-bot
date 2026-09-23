import os
import json
import time
import math
import threading
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


# =========================================================
# НАСТРОЙКИ
# =========================================================

TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_TOKEN")
    or os.getenv("BOT_TOKEN")
)

PITCH_API_KEY = (
    os.getenv("PITCH_API_KEY")
    or os.getenv("FOOTBALL_API_KEY")
)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN / BOT_TOKEN not found")

if not PITCH_API_KEY:
    raise RuntimeError("PITCH_API_KEY / FOOTBALL_API_KEY not found")


TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/"
)

PITCH_API = "https://api.pitchapi.dev/v1"

PITCH_HEADERS = {
    "X-API-KEY": PITCH_API_KEY
}


# =========================================================
# LIVE СТРАТЕГИЯ
# =========================================================

CHECK_EVERY_SECONDS = 120

MIN_MINUTE = 5
MAX_MINUTE = 85

MIN_XG = 0.55
MIN_SHOTS = 6
MIN_ON_TARGET = 2

MIN_PROBABILITY = 0.60
MIN_ADVANTAGE = 0.15

# Сигнал только при наличии минимум 4 сильных факторов
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

sent_signals = {}


# =========================================================
# HTTP
# =========================================================

def get_json(url, headers=None):

    request = urllib.request.Request(
        url,
        headers=headers or {}
    )

    with urllib.request.urlopen(
        request,
        timeout=25
    ) as response:

        return json.loads(
            response.read()
        )


def pitch_api(endpoint, params=None):

    url = PITCH_API + endpoint

    if params:

        url += "?" + urllib.parse.urlencode(
            params
        )

    try:

        result = get_json(
            url,
            PITCH_HEADERS
        )

        return result.get(
            "data"
        )

    except Exception as e:

        print(
            "PITCH API ERROR:",
            endpoint,
            str(e)
        )

        return None


# =========================================================
# TELEGRAM
# =========================================================

def telegram(method, data=None):

    url = TELEGRAM_API + method

    if data is not None:

        body = urllib.parse.urlencode(
            data
        ).encode()

        request = urllib.request.Request(
            url,
            data=body
        )

    else:

        request = urllib.request.Request(
            url
        )

    with urllib.request.urlopen(
        request,
        timeout=30
    ) as response:

        return json.loads(
            response.read()
        )


def send_message(
    chat_id,
    text,
    keyboard=None
):

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

        telegram(
            "sendMessage",
            data
        )

    except Exception as e:

        print(
            "TELEGRAM ERROR:",
            str(e)
        )


def answer_callback(callback_id):

    try:

        telegram(
            "answerCallbackQuery",
            {
                "callback_query_id":
                    callback_id
            }
        )

    except Exception as e:

        print(
            "CALLBACK ERROR:",
            str(e)
        )


# =========================================================
# КЛАВИАТУРА
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
                    "callback_data":
                        f"match:{match_id}"
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
# МАТЧИ СЕГОДНЯ
# =========================================================

def get_today_matches():

    today = datetime.now(
        ZoneInfo("Europe/Tallinn")
    ).strftime("%Y-%m-%d")

    return_data = pitch_api(
        f"/date/{today}",
        {
            "status": "all"
        }
    )

    if not return_data:

        return []

    return return_data.get(
        "matches",
        []
    )


# =========================================================
# ВЫЧИСЛЕНИЕ МИНУТЫ
# =========================================================

def get_match_minute(match):

    time_utc = match.get(
        "time_utc"
    )

    if not time_utc:

        return 0

    try:

        kickoff = datetime.fromisoformat(
            time_utc.replace(
                "Z",
                "+00:00"
            )
        )

    except Exception:

        return 0

    now = datetime.now(
        timezone.utc
    )

    minutes = (
        now - kickoff
    ).total_seconds() / 60

    return int(
        max(
            0,
            minutes
        )
    )


# =========================================================
# LIVE МАТЧИ
# =========================================================

def get_live_matches():

    matches = get_today_matches()

    live = []

    for match in matches:

        status = str(
            match.get(
                "status",
                ""
            )
        ).lower()

        minute = get_match_minute(
            match
        )

        # PitchAPI status + временной фильтр.
        # Оставляем несколько вариантов статуса,
        # чтобы не зависеть от написания статуса.

        live_status = (
            "live" in status
            or "progress" in status
            or status == "in_play"
            or status == "paused"
            or status == "halftime"
        )

        # Дополнительная проверка по времени.
        time_live = (
            0 <= minute <= 130
            and status not in (
                "finished",
                "not_started",
                "cancelled",
                "postponed"
            )
        )

        if live_status or time_live:

            match["_minute"] = minute

            live.append(
                match
            )

    return live


# =========================================================
# MATCH
# =========================================================

def get_match(match_id):

    return pitch_api(
        f"/matches/{match_id}"
    )


def get_shots(match_id):

    return pitch_api(
        f"/matches/{match_id}/shots"
    )


def get_events(match_id):

    return pitch_api(
        f"/matches/{match_id}/events"
    )


def get_momentum(match_id):

    return pitch_api(
        f"/matches/{match_id}/momentum"
    )


# =========================================================
# SAFE FLOAT
# =========================================================

def safe_float(value):

    try:

        if value is None:
            return 0.0

        if isinstance(
            value,
            (int, float)
        ):

            return float(value)

        text = str(value)

        # Например:
        # "329 (82%)" -> 329

        text = text.split(
            "("
        )[0].strip()

        return float(text)

    except Exception:

        return 0.0


# =========================================================
# УДАРЫ + xG + xGOT
# =========================================================

def calculate_shots(
    shots_data,
    home_id,
    away_id
):

    result = {

        "home_shots": 0,
        "away_shots": 0,

        "home_on_target": 0,
        "away_on_target": 0,

        "home_xg": 0.0,
        "away_xg": 0.0,

        "home_xgot": 0.0,
        "away_xgot": 0.0
    }

    if not shots_data:

        return result

    periods = shots_data.get(
        "periods",
        []
    )

    for period in periods:

        shots = period.get(
            "shots",
            []
        )

        for shot in shots:

            team_id = shot.get(
                "team_id"
            )

            xg = safe_float(
                shot.get(
                    "expected_goals"
                )
            )

            xgot = safe_float(
                shot.get(
                    "expected_goals_on_target"
                )
            )

            on_target = bool(
                shot.get(
                    "is_on_target",
                    False
                )
            )

            if team_id == home_id:

                result[
                    "home_shots"
                ] += 1

                result[
                    "home_xg"
                ] += xg

                result[
                    "home_xgot"
                ] += xgot

                if on_target:

                    result[
                        "home_on_target"
                    ] += 1

            elif team_id == away_id:

                result[
                    "away_shots"
                ] += 1

                result[
                    "away_xg"
                ] += xg

                result[
                    "away_xgot"
                ] += xgot

                if on_target:

                    result[
                        "away_on_target"
                    ] += 1

    return result


# =========================================================
# КРАСНЫЕ КАРТОЧКИ
# =========================================================

def count_red_cards(
    events_data,
    home_id,
    away_id
):

    home_red = 0
    away_red = 0

    if not events_data:

        return (
            home_red,
            away_red
        )

    events = events_data.get(
        "events",
        []
    )

    for event in events:

        event_type = str(
            event.get(
                "event_type",
                ""
            )
        ).lower()

        team_id = event.get(
            "team_id"
        )

        if event_type != "redcard":

            continue

        if team_id == home_id:

            home_red += 1

        elif team_id == away_id:

            away_red += 1

    return (
        home_red,
        away_red
    )


# =========================================================
# MOMENTUM
# =========================================================

def calculate_momentum(
    momentum_data,
    minute
):

    if not momentum_data:

        return 0.0

    points = momentum_data.get(
        "points",
        []
    )

    if not points:

        return 0.0

    recent = []

    for point in points:

        p_minute = safe_float(
            point.get(
                "minute"
            )
        )

        value = safe_float(
            point.get(
                "value"
            )
        )

        if (
            minute - 10
            <= p_minute
            <= minute
        ):

            recent.append(
                value
            )

    if not recent:

        return 0.0

    return (
        sum(recent)
        / len(recent)
    )


# =========================================================
# LIVE INTENSITY
# =========================================================

def calculate_intensity(
    home_xg,
    away_xg,
    minute,
    score_diff,
    red_home,
    red_away
):

    elapsed = max(
        5,
        min(
            minute,
            90
        )
    )

    remaining = max(
        1,
        90 - elapsed
    )

    # xG в минуту
    home_rate = (
        home_xg / elapsed
    )

    away_rate = (
        away_xg / elapsed
    )

    # Ожидаемый xG до конца
    lambda_home = (
        home_rate
        * remaining
    )

    lambda_away = (
        away_rate
        * remaining
    )

    # =====================================================
    # СЧЁТ
    # =====================================================

    if score_diff < 0:

        # Проигрывающая команда
        # становится агрессивнее.

        lambda_home *= 1.15
        lambda_away *= 0.90

    elif score_diff > 0:

        lambda_home *= 0.90
        lambda_away *= 1.15

    # =====================================================
    # КРАСНЫЕ
    # =====================================================

    lambda_home *= (
        0.75 ** red_home
    )

    lambda_away *= (
        0.75 ** red_away
    )

    # =====================================================
    # ЕСЛИ xG ОЧЕНЬ МАЛЕНЬКИЙ
    # НЕ СОЗДАЁМ ФИКТИВНУЮ АТАКУ
    # =====================================================

    if home_xg < 0.20:

        lambda_home *= 0.65

    if away_xg < 0.20:

        lambda_away *= 0.65

    return (
        max(
            0.001,
            lambda_home
        ),
        max(
            0.001,
            lambda_away
        )
    )


# =========================================================
# ВЕРОЯТНОСТЬ СЛЕДУЮЩЕГО ГОЛА
# =========================================================

def next_goal_probability(
    lambda_home,
    lambda_away
):

    total = (
        lambda_home
        + lambda_away
    )

    if total <= 0:

        return None

    home_probability = (
        lambda_home
        / total
    )

    away_probability = (
        lambda_away
        / total
    )

    return (
        home_probability,
        away_probability
    )


# =========================================================
# АНАЛИЗ МАТЧА
# =========================================================

def analyze_match(
    match_id
):

    match = get_match(
        match_id
    )

    if not match:

        return {
            "signal": False,
            "text":
                "⚪ <b>ПРОПУСК</b>\n\n"
                "Не удалось получить матч."
        }

    home_team = match.get(
        "home_team",
        {}
    )

    away_team = match.get(
        "away_team",
        {}
    )

    home_id = home_team.get(
        "id"
    )

    away_id = away_team.get(
        "id"
    )

    home = home_team.get(
        "name",
        "Хозяева"
    )

    away = away_team.get(
        "name",
        "Гости"
    )

    score_home = int(
        safe_float(
            match.get(
                "score_home"
            )
        )
    )

    score_away = int(
        safe_float(
            match.get(
                "score_away"
            )
        )
    )

    minute = get_match_minute(
        match
    )

    # =====================================================
    # ВРЕМЕННОЙ ФИЛЬТР
    # =====================================================

    if minute < MIN_MINUTE:

        return {
            "signal": False,

            "text":
                f"⚽ <b>{home} — {away}</b>\n\n"
                f"⏱ {minute}′\n"
                f"📊 {score_home}:{score_away}\n\n"
                "⚪ <b>ПРОПУСК</b>\n\n"
                "Первые 5 минут."
        }

    if minute > MAX_MINUTE:

        return {
            "signal": False,

            "text":
                f"⚽ <b>{home} — {away}</b>\n\n"
                f"⏱ {minute}′\n"
                f"📊 {score_home}:{score_away}\n\n"
                "⚪ <b>ПРОПУСК</b>\n\n"
                "Поздняя стадия матча."
        }

    # =====================================================
    # ДАННЫЕ
    # =====================================================

    shots_data = get_shots(
        match_id
    )

    events_data = get_events(
        match_id
    )

    momentum_data = get_momentum(
        match_id
    )

    shot_stats = calculate_shots(
        shots_data,
        home_id,
        away_id
    )

    red_home, red_away = (
        count_red_cards(
            events_data,
            home_id,
            away_id
        )
    )

    momentum = calculate_momentum(
        momentum_data,
        minute
    )

    # =====================================================
    # СТАТИСТИКА
    # =====================================================

    home_shots = shot_stats[
        "home_shots"
    ]

    away_shots = shot_stats[
        "away_shots"
    ]

    home_target = shot_stats[
        "home_on_target"
    ]

    away_target = shot_stats[
        "away_on_target"
    ]

    home_xg = shot_stats[
        "home_xg"
    ]

    away_xg = shot_stats[
        "away_xg"
    ]

    home_xgot = shot_stats[
        "home_xgot"
    ]

    away_xgot = shot_stats[
        "away_xgot"
    ]

    total_shots = (
        home_shots
        + away_shots
    )

    total_target = (
        home_target
        + away_target
    )

    total_xg = (
        home_xg
        + away_xg
    )

    # =====================================================
    # ИНТЕНСИВНОСТЬ
    # =====================================================

    score_diff = (
        score_home
        - score_away
    )

    lambda_home, lambda_away = (
        calculate_intensity(
            home_xg,
            away_xg,
            minute,
            score_diff,
            red_home,
            red_away
        )
    )

    probabilities = (
        next_goal_probability(
            lambda_home,
            lambda_away
        )
    )

    if not probabilities:

        return {
            "signal": False,

            "text":
                f"⚽ <b>{home} — {away}</b>\n\n"
                "⚪ <b>ПРОПУСК</b>\n\n"
                "Недостаточно данных."
        }

    home_probability = probabilities[0]
    away_probability = probabilities[1]

    # =====================================================
    # НАПРАВЛЕНИЕ
    # =====================================================

    if home_probability >= away_probability:

        next_team = home
        probability = home_probability
        opponent_probability = away_probability
        direction = "HOME"

    else:

        next_team = away
        probability = away_probability
        opponent_probability = home_probability
        direction = "AWAY"

    advantage = (
        probability
        - opponent_probability
    )

    # =====================================================
    # ФИЛЬТРЫ
    # =====================================================

    filters = []

    # 1. xG
    if total_xg >= MIN_XG:

        filters.append(
            "xG"
        )

    # 2. Удары
    if total_shots >= MIN_SHOTS:

        filters.append(
            "Удары"
        )

    # 3. Удары в створ
    if total_target >= MIN_ON_TARGET:

        filters.append(
            "В створ"
        )

    # 4. Вероятность
    if probability >= MIN_PROBABILITY:

        filters.append(
            "Вероятность"
        )

    # 5. Перевес
    if advantage >= MIN_ADVANTAGE:

        filters.append(
            "Перевес"
        )

    # =====================================================
    # MOMENTUM
    # =====================================================

    momentum_support = False

    if direction == "HOME":

        if momentum >= 5:

            momentum_support = True

    else:

        if momentum <= -5:

            momentum_support = True

    # =====================================================
    # ФИНАЛ
    # =====================================================

    signal = (
        len(filters) >= MIN_FILTERS
        and probability >= MIN_PROBABILITY
        and advantage >= MIN_ADVANTAGE
        and momentum_support
    )

    # =====================================================
    # ТЕКСТ СИГНАЛА
    # =====================================================

    if signal:

        state["signals"] += 1

        text = (

            f"🟢 <b>LIVE SIGNAL</b>\n\n"

            f"⚽ <b>{home} — {away}</b>\n"

            f"⏱ {minute}′\n"

            f"📊 Счёт: "
            f"{score_home}:{score_away}\n\n"

            f"⚽ <b>Следующий гол — "
            f"{next_team}</b>\n\n"

            f"📈 Вероятность: "
            f"<b>{probability * 100:.1f}%</b>\n"

            f"⚖️ Перевес: "
            f"<b>+{advantage * 100:.1f}%</b>\n\n"

            f"📊 xG: "
            f"{home_xg:.2f} — "
            f"{away_xg:.2f}\n"

            f"🎯 xGOT: "
            f"{home_xgot:.2f} — "
            f"{away_xgot:.2f}\n"

            f"🥅 Удары: "
            f"{home_shots} — "
            f"{away_shots}\n"

            f"🎯 В створ: "
            f"{home_target} — "
            f"{away_target}\n\n"

            f"📈 Momentum: "
            f"{momentum:+.1f}\n"

            f"🟥 Красные: "
            f"{red_home} — "
            f"{red_away}\n\n"

            f"🔥 Фильтры: "
            f"<b>{len(filters)}/5</b>\n\n"

            "⚠️ Статистический сигнал, "
            "не гарантия результата."
        )

    else:

        text = (

            f"⚽ <b>{home} — {away}</b>\n\n"

            f"⏱ {minute}′\n"

            f"📊 Счёт: "
            f"{score_home}:{score_away}\n\n"

            f"📊 xG: "
            f"{home_xg:.2f} — "
            f"{away_xg:.2f}\n"

            f"🎯 xGOT: "
            f"{home_xgot:.2f} — "
            f"{away_xgot:.2f}\n"

            f"🥅 Удары: "
            f"{home_shots} — "
            f"{away_shots}\n"

            f"🎯 В створ: "
            f"{home_target} — "
            f"{away_target}\n\n"

            f"🎯 Следующий гол: "
            f"{next_team}\n"

            f"📈 Вероятность: "
            f"{probability * 100:.1f}%\n"

            f"⚖️ Перевес: "
            f"{advantage * 100:.1f}%\n"

            f"📈 Momentum: "
            f"{momentum:+.1f}\n\n"

            f"🔥 Фильтры: "
            f"{len(filters)}/5\n\n"

            "⚪ <b>ПРОПУСК</b>"
        )

    state["scans"] += 1

    return {
        "signal": signal,
        "text": text
    }


# =========================================================
# СПИСОК LIVE
# =========================================================

def live_keyboard(matches):

    buttons = []

    for match in matches[:30]:

        match_id = match.get(
            "id"
        )

        home = match.get(
            "home_team",
            {}
        ).get(
            "name",
            "?"
        )

        away = match.get(
            "away_team",
            {}
        ).get(
            "name",
            "?"
        )

        home_score = int(
            safe_float(
                match.get(
                    "score_home"
                )
            )
        )

        away_score = int(
            safe_float(
                match.get(
                    "score_away"
                )
            )
        )

        minute = match.get(
            "_minute",
            0
        )

        buttons.append(

            [
                {
                    "text":
                        f"🔴 {minute}′ "
                        f"{home} "
                        f"{home_score}:"
                        f"{away_score} "
                        f"{away}",

                    "callback_data":
                        f"match:{match_id}"
                }
            ]
        )

    if not buttons:

        buttons.append(

            [
                {
                    "text": "🔄 Обновить",
                    "callback_data": "live"
                }
            ]
        )

    return {
        "inline_keyboard":
            buttons
    }


# =========================================================
# ОБРАБОТКА TELEGRAM
# =========================================================

def process_update(update):

    # =====================================================
    # MESSAGE
    # =====================================================

    if "message" in update:

        message = update[
            "message"
        ]

        chat_id = message[
            "chat"
        ][
            "id"
        ]

        subscribers.add(
            chat_id
        )

        text = message.get(
            "text",
            ""
        ).strip()

        if text == "/start":

            send_message(

                chat_id,

                "⚽ <b>FOOTBALL LIVE</b>\n\n"
                "Live-анализ следующего гола.\n\n"
                "Бот автоматически проверяет "
                "live-матчи и ищет сильные сигналы.\n\n"
                "Выбери раздел:",

                main_keyboard()
            )

            return

        if text == "/status":

            send_message(

                chat_id,

                f"📊 <b>СТАТУС</b>\n\n"
                f"🔎 Анализов: "
                f"{state['scans']}\n"
                f"🟢 Сигналов: "
                f"{state['signals']}\n"
                f"💰 Тестовый банк: "
                f"{state['bankroll']:.2f}"
            )

            return

        if text.startswith(
            "/bank "
        ):

            try:

                amount = float(
                    text.split(
                        " ",
                        1
                    )[1]
                )

                if amount <= 0:

                    raise ValueError

                state[
                    "bankroll"
                ] = amount

                send_message
