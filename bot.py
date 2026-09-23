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

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Поддерживаем оба варианта имени переменной.
# Можно оставить существующий FOOTBALL_API_KEY в Render.
PITCH_API_KEY = (
    os.environ.get("PITCH_API_KEY")
    or os.environ.get("FOOTBALL_API_KEY")
)

if not PITCH_API_KEY:
    raise RuntimeError("PITCH_API_KEY / FOOTBALL_API_KEY not found")


TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

PITCH_API = "https://api.pitchapi.dev/v1"

PITCH_HEADERS = {
    "X-API-KEY": PITCH_API_KEY
}


# =========================================================
# СТРАТЕГИЯ LIVE
# =========================================================

CHECK_EVERY_MINUTES = 2

MIN_MINUTE = 5
MAX_MINUTE = 85

# Минимальная вероятность сигнала
MIN_PROBABILITY = 0.60

# Минимальный перевес одной команды
MIN_ADVANTAGE = 0.15

# Минимальный live-xG суммарно
MIN_XG = 0.55

# Минимум ударов в створ для сильного сигнала
MIN_ON_TARGET = 2

# Минимальный темп ударов
MIN_SHOTS = 6

# Максимальное количество сигналов на один матч
MAX_SIGNALS_PER_MATCH = 1


# =========================================================
# СОСТОЯНИЕ
# =========================================================

state = {
    "signals": 0,
    "scans": 0,
    "bankroll": 1000.0
}

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

        raw = response.read()

        return json.loads(raw)


def pitch(endpoint, params=None):

    url = PITCH_API + endpoint

    if params:

        url += "?" + urllib.parse.urlencode(params)

    try:

        result = get_json(
            url,
            PITCH_HEADERS
        )

        return result.get("data")

    except Exception as e:

        print(
            "PITCH API ERROR:",
            endpoint,
            type(e).__name__,
            str(e)
        )

        return None


# =========================================================
# TELEGRAM
# =========================================================

def telegram(method, data=None):

    url = TELEGRAM_API + method

    if data:

        body = urllib.parse.urlencode(
            data
        ).encode()

        request = urllib.request.Request(
            url,
            data=body
        )

    else:

        request = urllib.request.Request(url)

    with urllib.request.urlopen(
        request,
        timeout=25
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
        "text": text
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
            type(e).__name__,
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
            e
        )


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
                    "text": "🔄 Обновить анализ",
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
                    "text": "🏠 Главное меню",
                    "callback_data": "home"
                }
            ]

        ]
    }


# =========================================================
# МАТЧИ
# =========================================================

def get_today_matches():

    today = datetime.now(
        ZoneInfo("Europe/Tallinn")
    ).strftime("%Y-%m-%d")

    data = pitch(
        f"/date/{today}",
        {
            "status": "all"
        }
    )

    if not data:

        return []

    return data.get(
        "matches",
        []
    )


def match_is_live(match):

    status = str(
        match.get(
            "status",
            ""
        )
    ).lower()

    if status in (
        "finished",
        "not_started",
        "cancelled",
        "postponed"
    ):

        return False

    return True


def get_live_matches():

    matches = get_today_matches()

    live = []

    now = datetime.now(
        timezone.utc
    )

    for match in matches:

        if not match_is_live(match):
            continue

        time_utc = match.get(
            "time_utc"
        )

        if not time_utc:
            continue

        try:

            kickoff = datetime.fromisoformat(
                time_utc.replace(
                    "Z",
                    "+00:00"
                )
            )

        except Exception:

            continue

        minutes = (
            now - kickoff
        ).total_seconds() / 60

        # Матч считаем LIVE примерно
        # в диапазоне 0–130 минут от начала.
        if 0 <= minutes <= 130:

            match["_minute_estimate"] = int(
                max(
                    0,
                    minutes
                )
            )

            live.append(match)

    return live


# =========================================================
# MATCH DATA
# =========================================================

def get_match(match_id):

    return pitch(
        f"/matches/{match_id}"
    )


def get_match_stats(match_id):

    return pitch(
        f"/matches/{match_id}/stats"
    )


def get_match_shots(match_id):

    return pitch(
        f"/matches/{match_id}/shots"
    )


def get_match_events(match_id):

    return pitch(
        f"/matches/{match_id}/events"
    )


def get_match_momentum(match_id):

    return pitch(
        f"/matches/{match_id}/momentum"
    )


# =========================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================

def safe_float(value):

    try:

        if value is None:
            return 0.0

        return float(value)

    except Exception:

        return 0.0


def get_score(match):

    home = safe_float(
        match.get(
            "score_home"
        )
    )

    away = safe_float(
        match.get(
            "score_away"
        )
    )

    return int(home), int(away)


def get_minute(match):

    if "_minute_estimate" in match:

        return int(
            match["_minute_estimate"]
        )

    return 0


# =========================================================
# STATS PARSER
# =========================================================

def parse_stats(stats_data):

    result = {
        "home": {},
        "away": {}
    }

    if not stats_data:
        return result

    periods = stats_data.get(
        "periods",
        []
    )

    all_period = None

    for period in periods:

        if period.get(
            "period"
        ) == "All":

            all_period = period
            break

    if not all_period:

        return result

    groups = all_period.get(
        "groups",
        []
    )

    for group in groups:

        for item in group.get(
            "items",
            []
        ):

            key = item.get(
                "key"
            )

            if not key:
                continue

            result["home"][key] = (
                safe_float(
                    item.get(
                        "home"
                    )
                )
            )

            result["away"][key] = (
                safe_float(
                    item.get(
                        "away"
                    )
                )

    return result


# =========================================================
# SHOTS / xG / xGOT
# =========================================================

def calculate_shot_stats(
    shots_data,
    home_id,
    away_id
):

    data = {

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

        return data

    periods = shots_data.get(
        "periods",
        []
    )

    for period in periods:

        for shot in period.get(
            "shots",
            []
        ):

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
                    "is_on_target"
                )
            )

            if team_id == home_id:

                data["home_shots"] += 1

                data["home_xg"] += xg

                data["home_xgot"] += xgot

                if on_target:
                    data["home_on_target"] += 1

            elif team_id == away_id:

                data["away_shots"] += 1

                data["away_xg"] += xg

                data["away_xgot"] += xgot

                if on_target:
                    data["away_on_target"] += 1

    return data


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

        return home_red, away_red

    events = events_data.get(
        "events",
        []
    )

    for event in events:

        event_type = str(
            event.get(
                "type",
                ""
            )
        ).lower()

        detail = str(
            event.get(
                "detail",
                ""
            )
        ).lower()

        is_red = (
            "red" in event_type
            or "red" in detail
        )

        if not is_red:
            continue

        team_id = (
            event.get(
                "team_id"
            )
            or event.get(
                "team",
                {}
            ).get("id")
        )

        if team_id == home_id:

            home_red += 1

        elif team_id == away_id:

            away_red += 1

    return home_red, away_red


# =========================================================
# MOMENTUM
# =========================================================

def get_recent_momentum(
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

            recent.append(value)

    if not recent:

        return 0.0

    return sum(recent) / len(recent)


# =========================================================
# LIVE INTENSITY
# =========================================================

def calculate_live_intensity(
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

    # -----------------------------------------------------
    # Базовая скорость xG
    # -----------------------------------------------------

    home_rate = home_xg / elapsed
    away_rate = away_xg / elapsed

    # -----------------------------------------------------
    # Прогноз xG до конца
    # -----------------------------------------------------

    lambda_home = (
        home_rate
        * remaining
    )

    lambda_away = (
        away_rate
        * remaining
    )

    # -----------------------------------------------------
    # Если xG пока очень маленький,
    # не создаём искусственный сильный сигнал.
    # -----------------------------------------------------

    if home_xg < 0.20:

        lambda_home *= 0.65

    if away_xg < 0.20:

        lambda_away *= 0.65

    # -----------------------------------------------------
    # Счёт
    # -----------------------------------------------------

    if score_diff < 0:

        lambda_home *= 1.15
        lambda_away *= 0.90

    elif score_diff > 0:

        lambda_home *= 0.90
        lambda_away *= 1.15

    # -----------------------------------------------------
    # Красная карточка
    # -----------------------------------------------------

    lambda_home *= (
        0.75 ** red_home
    )

    lambda_away *= (
        0.75 ** red_away
    )

    return (
        max(
            0.01,
            lambda_home
        ),
        max(
            0.01,
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

    return {
        "HOME_NEXT":
            home_probability,

        "AWAY_NEXT":
            away_probability
    }


# =========================================================
# АНАЛИЗ
# =========================================================

def analyze_match(
    match_id
):

    match = get_match(
        match_id
    )

    if not match:

        return {
            "decision": "ПРОПУСК",
            "text": "⚪ ПРОПУСК\n\nМатч не найден."
        }

    home_id = (
        match.get(
            "home_team",
            {}
        ).get("id")
    )

    away_id = (
        match.get(
            "away_team",
            {}
        ).get("id")
    )

    home = (
        match.get(
            "home_team",
            {}
        ).get(
            "name",
            "Хозяева"
        )
    )

    away = (
        match.get(
            "away_team",
            {}
        ).get(
            "name",
            "Гости"
        )
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

    # -----------------------------------------------------
    # LIVE МИНУТА
    # -----------------------------------------------------

    today_matches = get_live_matches()

    minute = 0

    for m in today_matches:

        if m.get("id") == match_id:

            minute = get_minute(m)

            break

    # -----------------------------------------------------
    # ВНЕ ДИАПАЗОНА
    # -----------------------------------------------------

    if minute < MIN_MINUTE:

        return {
            "decision": "ПРОПУСК",

            "text":
                f"⚽ {home} — {away}\n\n"
                f"⏱ {minute}′\n"
                f"📊 Счёт: "
                f"{score_home}:{score_away}\n\n"
                "⚪ ПРОПУСК\n\n"
                "Слишком рано для LIVE-сигнала."
        }

    if minute > MAX_MINUTE:

        return {
            "decision": "ПРОПУСК",

            "text":
                f"⚽ {home} — {away}\n\n"
                f"⏱ {minute}′\n"
                f"📊 Счёт: "
                f"{score_home}:{score_away}\n\n"
                "⚪ ПРОПУСК\n\n"
                "Слишком поздно для входа."
        }

    # -----------------------------------------------------
    # ДАННЫЕ
    # -----------------------------------------------------

    shots_data = get_match_shots(
        match_id
    )

    events_data = get_match_events(
        match_id
    )

    momentum_data = get_match_momentum(
        match_id
    )

    stats_data = get_match_stats(
        match_id
    )

    shot_stats = calculate_shot_stats(
        shots_data,
        home_id,
        away_id
    )

    stats = parse_stats(
        stats_data
    )

    red_home, red_away = count_red_cards(
        events_data,
        home_id,
        away_id
    )

    momentum = get_recent_momentum(
        momentum_data,
        minute
    )

    # -----------------------------------------------------
    # ОСНОВНЫЕ ПОКАЗАТЕЛИ
    # -----------------------------------------------------

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

    total_xgot = (
        home_xgot
        + away_xgot
    )

    # -----------------------------------------------------
    # INTENSITY
    # -----------------------------------------------------

    score_diff = (
        score_home
        - score_away
    )

    lambda_home, lambda_away = (
        calculate_live_intensity(
            home_xg,
            away_xg,
            minute,
            score_diff,
            red_home,
            red_away
        )
    )

    probs = next_goal_probability(
        lambda_home,
        lambda_away
    )

    if not probs:

        return {
            "decision": "ПРОПУСК",

            "text":
                f"⚽ {home} — {away}\n\n"
                "⚪ ПРОПУСК\n\n"
                "Недостаточно данных."
        }

    home_prob = probs[
        "HOME_NEXT"
    ]

    away_prob = probs[
        "AWAY_NEXT"
    ]

    # -----------------------------------------------------
    # КТО ИМЕЕТ ПРЕИМУЩЕСТВО
    # -----------------------------------------------------

    if home_prob >= away_prob:

        next_team = home
        probability = home_prob
        opponent_probability = away_prob
        direction = "HOME"

    else:

        next_team = away
        probability = away_prob
        opponent_probability = home_prob
        direction = "AWAY"

    advantage = (
        probability
        - opponent_probability
    )

    # -----------------------------------------------------
    # ФИЛЬТРЫ
    # -----------------------------------------------------

    filters = 0
    reasons = []

    if total_xg >= MIN_XG:

        filters += 1

    else:

        reasons.append(
            f"xG {total_xg:.2f}"
        )

    if total_target >= MIN_ON_TARGET:

        filters += 1

    else:

        reasons.append(
            f"в створ {total_target}"
        )

    if total_shots >= MIN_SHOTS:

        filters += 1

    else:

        reasons.append(
            f"удары {total_shots}"
        )

    if probability >= MIN_PROBABILITY:

        filters += 1

    else:

        reasons.append(
            f"вероятность "
            f"{probability * 100:.1f}%"
        )

    if advantage >= MIN_ADVANTAGE:

        filters += 1

    else:

        reasons.append(
            f"перевес "
            f"{advantage * 100:.1f}%"
        )

    # -----------------------------------------------------
    # MOMENTUM
    # -----------------------------------------------------

    momentum_support = False

    if direction == "HOME" and momentum > 0.10:

        momentum_support = True

    if direction == "AWAY" and momentum < -0.10:

        momentum_support = True

    # -----------------------------------------------------
    # ФИНАЛЬНОЕ РЕШЕНИЕ
    # -----------------------------------------------------

    signal = (
        filters >= 4
        and momentum_support
        and probability >= MIN_PROBABILITY
        and advantage >= MIN_ADVANTAGE
    )

    if signal:

        decision = "🟢 СИГНАЛ"

        state["signals"] += 1

    else:

        decision = "⚪ ПРОПУСК"

    state["scans"] += 1

    # -----------------------------------------------------
    # ТЕКСТ
    # -----------------------------------------------------

    momentum_text = (
        f"{momentum:+.2f}"
    )

    if signal:

        signal_text = (

            f"🟢 <b>LIVE SIGNAL</b>\n\n"

            f"⚽ <b>{home} — {away}</b>\n"
            f"⏱ {minute}′\n"
            f"📊 Счёт: "
            f"{score_home}:{score_away}\n\n"

            f"🎯 Следующий гол: "
            f"<b>{next_team}</b>\n\n"

            f"📈 Наша вероятность: "
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
            f"{momentum_text}\n"

            f"🟥 Красные: "
            f"{red_home} — "
            f"{red_away}\n\n"

            f"🔥 Фильтры: "
            f"{filters}/5\n\n"

            f"⚠️ Это статистический сигнал, "
            f"не гарантия результата."
        )

    else:

        reason_text = ", ".join(
            reasons[:3]
        )

        signal_text = (

            f"⚽ <b>{home} — {away}</b>\n\n"

            f"⏱ Минута: {minute}′\n"

            f"📊 Счёт: "
            f"{score_home}:{score_away}\n\n"

            f"📈 xG: "
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
            f"{momentum_text}\n\n"

            f"🔥 Фильтры: "
            f"{filters}/5\n\n"

            f"⚪ <b>ПРОПУСК</b>\n"

            f"{reason_text}"
        )

    return {
        "decision": decision,
        "text": signal_text
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
            "_minute_estimate",
            "?"
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
# АНАЛИЗ МАТЧА
# =========================================================

def handle_match(
    chat_id,
    match_id
):

    try:

        result = analyze_match(
            match_id
        )

        send_message(
            chat_id,
            result["text"],
            match_keyboard(
                match_id
            )
        )

    except Exception as e:

        print(
            "MATCH ERROR:",
            type(e).__name__,
            str(e)
        )

        send_message(
            chat_id,
            "⚠️ Ошибка получения LIVE-данных.\n\n"
            "Попробуй обновить анализ через 30–60 секунд."
        )


# =========================================================
# TELEGRAM UPDATE
# =========================================================

def process_update(update):

    if "message" in update:

        message = update[
            "message"
        ]

        chat_id = message[
            "chat"
        ][
            "id"
        ]

        text = message.get(
            "text",
            ""
        )

        if text == "/start":

            send_message(
                chat_id,

                "⚽ <b>FOOTBALL LIVE 2.0</b>\n\n"
                "LIVE-анализ следующего гола.\n\n"
                "Выбери раздел:",

                main_keyboard()
            )

    if "callback_query" in update:

        query = update[
            "callback_query"
        ]

        callback_id = query[
            "id"
        ]

        chat_id = query[
            "message"
        ][
            "chat"
        ][
            "id"
        ]

        data = query.get(
            "data",
            ""
        )

        answer_callback(
            callback_id
        )

        # -------------------------------------------------
        # HOME
        # -------------------------------------------------

        if data == "home":

            send_message(
                chat_id,

                "⚽ <b>FOOTBALL LIVE 2.0</b>\n\n"
                "Выбери раздел:",

                main_keyboard()
            )

        # -------------------------------------------------
        # LIVE
        # -------------------------------------------------

        elif data == "live":

            try:

                matches = get_live_matches()

                if not matches:

                    send_message(
                        chat_id,

                        "🔴 <b>LIVE</b>\n\n"
                        "Сейчас активных матчей "
                        "не найдено.",

                        main_keyboard()
                    )

                else:

                    send_message(
                        chat_id,

                        f"🔴 <b>LIVE</b>\n\n"
                        f"Найдено матчей: "
                        f"{len(matches)}\n\n"
                        "Выбери матч:",

                        live_keyboard(
                            matches
                        )
                    )

            except Exception as e:

                print(
                    "LIVE ERROR:",
                    type(e).__name__,
                    str(e)
                )

                send_message(
                    chat_id,
                    "⚠️ Не удалось получить LIVE-матчи."
                )

        # -------------------------------------------------
        # TODAY
        # -------------------------------------------------

        elif data == "today":

            try:

                matches = get_today_matches()

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

                    status = match.get(
                        "status",
                        "?"
                    )

                    buttons.append(

                        [
                            {
                                "text":
                                    f"{home} — "
                                    f"{away} "
                                    f"[{status}]",

                                "callback_data":
                                    f"match:{match_id}"
                            }
                        ]
                    )

                if not buttons:

                    send_message(
                        chat_id,
                        "📅 Сегодня матчей не найдено."
                    )

                else:

                    send_message(
                        chat_id,

                        f"📅 <b>МАТЧИ СЕГОДНЯ</b>\n\n"
                        f"Найдено: "
                        f"{len(matches)}\n\n"
                        "Выбери матч:",

                        {
                            "inline_keyboard":
                                buttons
                        }
                    )

            except Exception as e:

                print(
                    "TODAY ERROR:",
                    type(e).__name__,
                    str(e)
                )

                send_message(
                    chat_id,
                    "⚠️ Не удалось получить матчи."
                )

        # -------------------------------------------------
        # STATUS
        # -------------------------------------------------

        elif data == "status":

            send_message(
                chat_id,

                f"📊 <b>СТАТУС БОТА</b>\n\n"
                f"🔎 Анализов: "
                f"{state['scans']}\n"
                f"🟢 Сигналов: "
                f"{state['signals']}\n"
                f"💰 Тестовый банк: "
                f"{state['bankroll']:.2f}",

                main_keyboard()
            )

        # -------------------------------------------------
        # MATCH
        # -------------------------------------------------

        elif data.startswith(
            "match:"
        ):

            match_id = data.split(
                ":",
                1
            )[1]

            handle_match(
                chat_id,
                match_id
            )


# =========================================================
# MAIN LOOP
# =========================================================

def main():

    offset = 0

    print(
        "FOOTBALL LIVE 2.0 STARTED"
    )

    while True:

        try:

            result = telegram(
                "getUpdates",
                {
                    "timeout": 25,
                    "offset": offset
                }
            )

            for update in result.get(
                "result",
                []
            ):

                offset = (
                    update[
                        "update_id"
                    ] + 1
                )

                try:

                    process_update(
                        update
                    )

                except Exception as e:

                    print(
                        "UPDATE ERROR:",
                        type(e).__name__,
                        str(e)
                    )

        except Exception as e:

            print(
                "MAIN ERROR:",
                type(e).__name__,
                str(e)
            )

            time.sleep(5)


# =========================================================
# RENDER HEALTH SERVER
# =========================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"Football Live 2.0 is running"
        )

    def log_message(
        self,
        format,
        *args
    ):

        return


def start_web_server():

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    server = HTTPServer(
        (
            "0.0.0.0",
            port
        ),
        HealthHandler
    )

    server.serve_forever()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    threading.Thread(
        target=start_web_server,
        daemon=True
    ).start()

    main()
