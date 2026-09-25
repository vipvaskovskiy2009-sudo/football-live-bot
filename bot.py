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

BASE_URL = "https://api.the-odds-api.com/v4"

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
    format="%(asctime)s | %(levelname)s | %(message)s",
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
sent_signals = set()

# =========================================================
# KEYBOARD
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
# HTTP
# =========================================================

async def api_get(endpoint, params=None):
    if params is None:
        params = {}

    params["apiKey"] = ODDS_API_KEY

    url = f"{BASE_URL}{endpoint}"

    try:
        timeout = aiohttp.ClientTimeout(total=20)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:

                text = await response.text()

                if response.status != 200:
                    logging.error(
                        "API ERROR %s: %s",
                        response.status,
                        text[:500],
                    )
                    return None

                try:
                    data = await response.json()
                except Exception:
                    logging.error("Invalid JSON from API")
                    return None

                remaining = response.headers.get(
                    "x-requests-remaining",
                    "?",
                )

                logging.info(
                    "API OK | remaining=%s",
                    remaining,
                )

                return data

    except Exception as e:
        logging.error("API connection error: %s", e)
        return None


async def fetch_scores():
    return await api_get(
        f"/sports/{SPORT}/scores",
        {
            "daysFrom": 1,
            "dateFormat": "iso",
        },
    )


async def fetch_odds():
    return await api_get(
        f"/sports/{SPORT}/odds",
        {
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
    )


# =========================================================
# TIME
# =========================================================

def parse_time(value):
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except Exception:
        return None


def estimated_minute(match):
    start = parse_time(
        match.get("commence_time", "")
    )

    if not start:
        return 0

    now = datetime.now(timezone.utc)

    minutes = int(
        (now - start).total_seconds() / 60
    )

    if minutes < 0:
        return 0

    return min(minutes, 120)


def is_live(match):
    if match.get("completed") is True:
        return False

    start = parse_time(
        match.get("commence_time", "")
    )

    if not start:
        return False

    now = datetime.now(timezone.utc)

    if start > now:
        return False

    minute = estimated_minute(match)

    return 1 <= minute <= 120


# =========================================================
# ODDS
# =========================================================

def get_average_odds(match):

    home = match.get("home_team")
    away = match.get("away_team")

    home_values = []
    draw_values = []
    away_values = []

    for bookmaker in match.get("bookmakers", []):

        for market in bookmaker.get("markets", []):

            if market.get("key") != "h2h":
                continue

            for outcome in market.get("outcomes", []):

                name = outcome.get("name")
                price = outcome.get("price")

                if not price:
                    continue

                try:
                    price = float(price)
                except Exception:
                    continue

                if name == home:
                    home_values.append(price)

                elif name == away:
                    away_values.append(price)

                elif str(name).lower() == "draw":
                    draw_values.append(price)

    def avg(values):
        if not values:
            return None
        return sum(values) / len(values)

    return {
        "HOME": avg(home_values),
        "DRAW": avg(draw_values),
        "AWAY": avg(away_values),
    }


def remove_margin(odds):

    if not odds["HOME"]:
        return None

    if not odds["DRAW"]:
        return None

    if not odds["AWAY"]:
        return None

    raw = {
        "HOME": 1 / odds["HOME"],
        "DRAW": 1 / odds["DRAW"],
        "AWAY": 1 / odds["AWAY"],
    }

    total = sum(raw.values())

    if total <= 0:
        return None

    return {
        key: value / total
        for key, value in raw.items()
    }


# =========================================================
# POISSON
# =========================================================

def poisson_probability(lam, goals):

    if lam <= 0:
        return 1.0 if goals == 0 else 0.0

    return (
        math.exp(-lam)
        * (lam ** goals)
        / math.factorial(goals)
    )


def poisson_match_probabilities(
    home_lambda,
    away_lambda,
):

    home = 0.0
    draw = 0.0
    away = 0.0

    for home_goals in range(0, 8):

        for away_goals in range(0, 8):

            probability = (
                poisson_probability(
                    home_lambda,
                    home_goals,
                )
                *
                poisson_probability(
                    away_lambda,
                    away_goals,
                )
            )

            if home_goals > away_goals:
                home += probability

            elif home_goals == away_goals:
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


# =========================================================
# LIVE MODEL
# =========================================================

def calculate_model(
    match,
    score_home,
    score_away,
):

    odds = get_average_odds(match)

    market = remove_margin(odds)

    if not market:
        return None

    minute = estimated_minute(match)

    if minute < MIN_MINUTE:
        return None

    if minute > MAX_MINUTE:
        return None

    remaining = max(
        0.08,
        (90 - minute) / 90,
    )

    total_goals_base = 2.70

    home_share = (
        market["HOME"]
        /
        (
            market["HOME"]
            + market["AWAY"]
        )
    )

    away_share = 1 - home_share

    home_lambda = (
        total_goals_base
        * home_share
        * remaining
    )

    away_lambda = (
        total_goals_base
        * away_share
        * remaining
    )

    # LIVE score adjustment
    difference = score_home - score_away

    if difference < 0:
        home_lambda *= 1.18
        away_lambda *= 0.92

    elif difference > 0:
        home_lambda *= 0.92
        away_lambda *= 1.18

    # If draw, keep normal intensity
    home_lambda = max(
        0.03,
        home_lambda,
    )

    away_lambda = max(
        0.03,
        away_lambda,
    )

    model = poisson_match_probabilities(
        home_lambda,
        away_lambda,
    )

    if not model:
        return None

    return {
        "minute": minute,
        "odds": odds,
        "market": market,
        "model": model,
        "home_lambda": home_lambda,
        "away_lambda": away_lambda,
    }


# =========================================================
# VALUE
# =========================================================

def calculate_value(
    probability,
    odds,
):

    if not odds or odds <= 1:
        return 0.0

    fair_odds = 1 / probability

    return (
        odds / fair_odds
    ) - 1


def calculate_kelly(
    probability,
    odds,
    bankroll,
):

    if odds <= 1:
        return 0.0

    b = odds - 1
    q = 1 - probability

    raw_kelly = (
        b * probability - q
    ) / b

    raw_kelly = max(
        0.0,
        raw_kelly,
    )

    stake = (
        bankroll
        * raw_kelly
        * KELLY_FRACTION
    )

    return round(
        min(stake, bankroll * 0.05),
        2,
    )


# =========================================================
# MATCH FORMAT
# =========================================================

def get_score(match):

    scores = match.get("scores")

    if not scores:
        return 0, 0

    home_score = 0
    away_score = 0

    try:
        for item in scores:

            name = item.get("name")
            value = item.get("score")

            if value is None:
                continue

            value = int(value)

            if name == match.get("home_team"):
                home_score = value

            elif name == match.get("away_team"):
                away_score = value

    except Exception:
        pass

    return home_score, away_score


def match_title(match):

    return (
        f"{match.get('home_team', '?')}"
        f" — "
        f"{match.get('away_team', '?')}"
    )


# =========================================================
# LIVE MATCHES
# =========================================================

async def get_live_matches():

    scores = await fetch_scores()

    if not scores:
        return []

    result = []

    for match in scores:

        if not is_live(match):
            continue

        minute = estimated_minute(match)

        if minute < 1 or minute > 120:
            continue

        result.append(match)

    return result


# =========================================================
# ANALYSIS TEXT
# =========================================================

def analyse_live_match(match):

    home_score, away_score = get_score(match)

    result = calculate_model(
        match,
        home_score,
        away_score,
    )

    if not result:
        return None

    model = result["model"]
    odds = result["odds"]

    choices = []

    for side in ["HOME", "DRAW", "AWAY"]:

        probability = model[side]
        odd = odds[side]

        if not odd:
            continue

        value = calculate_value(
            probability,
            odd,
        )

        choices.append(
            (
                value,
                side,
                probability,
                odd,
            )
        )

    if not choices:
        return None

    choices.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    best = choices[0]

    value, side, probability, odd = best

    if value < VALUE_THRESHOLD:
        return {
            "signal": False,
            "minute": result["minute"],
            "home_score": home_score,
            "away_score": away_score,
            "side": side,
            "value": value,
            "probability": probability,
            "odds": odd,
        }

    stake = calculate_kelly(
        probability,
        odd,
        state["bankroll"],
    )

    return {
        "signal": True,
        "minute": result["minute"],
        "home_score": home_score,
        "away_score": away_score,
        "side": side,
        "value": value,
        "probability": probability,
        "odds": odd,
        "stake": stake,
    }


def side_name(match, side):

    if side == "HOME":
        return match.get(
            "home_team",
            "HOME",
        )

    if side == "AWAY":
        return match.get(
            "away_team",
            "AWAY",
        )

    return "НИЧЬЯ"


# =========================================================
# LIVE BUTTON
# =========================================================

async def show_live(update):

    matches = await get_live_matches()

    if not matches:

        await update.message.reply_text(
            "🔴 LIVE\n\n"
            "Сейчас активных LIVE-матчей "
            "не найдено.",
            reply_markup=MAIN_KEYBOARD,
        )

        return

    buttons = []

    for match in matches[:20]:

        home_score, away_score = get_score(
            match
        )

        minute = estimated_minute(match)

        text = (
            f"{match.get('home_team')} "
            f"{home_score}:{away_score} "
            f"{match.get('away_team')} "
            f"({minute}')"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    text,
                    callback_data=(
                        "match:"
                        + match.get("id", "")
                    ),
                )
            ]
        )

    await update.message.reply_text(
        "🔴 LIVE — выбери матч:",
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )


# =========================================================
# MATCH DETAILS
# =========================================================

async def show_match(
    query,
    match_id,
):

    scores = await fetch_scores()

    if not scores:
        await query.edit_message_text(
            "Не удалось получить LIVE-данные."
        )
        return

    match = None

    for item in scores:

        if item.get("id") == match_id:
            match = item
            break

    if not match:

        await query.edit_message_text(
            "Матч уже исчез из LIVE."
        )

        return

    home_score, away_score = get_score(
        match
    )

    minute = estimated_minute(match)

    analysis = analyse_live_match(
        match
    )

    text = (
        f"⚽ {match_title(match)}\n\n"
        f"⏱ Минута: ~{minute}'\n"
        f"📊 Счёт: {home_score}:{away_score}\n\n"
    )

    if not analysis:

        text += (
            "⚪ Недостаточно данных "
            "для сигнала."
        )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Обновить",
                            callback_data=(
                                "match:"
                                + match_id
                            ),
                        )
                    ]
                ]
            ),
        )

        return

    side = side_name(
        match,
        analysis["side"],
    )

    probability = (
        analysis["probability"]
        * 100
    )

    value = (
        analysis["value"]
        * 100
    )

    odds = analysis["odds"]

    if analysis["signal"]:

        stake = analysis["stake"]

        text += (
            "🟢 VALUE-СИГНАЛ\n\n"
            f"🎯 Сторона: {side}\n"
            f"📈 Вероятность модели: "
            f"{probability:.1f}%\n"
            f"💰 Коэффициент: {odds:.2f}\n"
            f"💎 Value: {value:.1f}%\n"
            f"💵 Ставка: {stake:.2f} €\n\n"
            "⚠️ Это статистический сигнал, "
            "не автоматическая ставка."
        )

    else:

        text += (
            "⚪ ПРОПУСК\n\n"
            f"Лучший вариант: {side}\n"
            f"Вероятность модели: "
            f"{probability:.1f}%\n"
            f"Коэффициент: {odds:.2f}\n"
            f"Value: {value:.1f}%\n\n"
            f"Требуется минимум "
            f"{VALUE_THRESHOLD * 100:.0f}%."
        )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 Обновить",
                        callback_data=(
                            "match:"
                            + match_id
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔴 LIVE",
                        callback_data="back_live",
                    )
                ],
            ]
        ),
    )


# =========================================================
# TODAY
# =========================================================

async def show_today(update):

    scores = await fetch_scores()

    if not scores:

        await update.message.reply_text(
            "Не удалось получить матчи.",
            reply_markup=MAIN_KEYBOARD,
        )

        return

    now = datetime.now(
        timezone.utc
    ).date()

    matches = []

    for match in scores:

        start = parse_time(
            match.get(
                "commence_time",
                "",
            )
        )

        if not start:
            continue

        if start.date() == now:
            matches.append(match)

    if not matches:

        await update.message.reply_text(
            "📅 Сегодня матчей не найдено.",
            reply_markup=MAIN_KEYBOARD,
        )

        return

    lines = [
        "📅 МАТЧИ СЕГОДНЯ",
        "",
    ]

    for match in matches[:30]:

        start = parse_time(
            match.get(
                "commence_time",
                "",
            )
        )

        if start:
            time_text = start.strftime(
                "%H:%M"
            )
        else:
            time_text = "--:--"

        status = ""

        if is_live(match):
            h, a = get_score(match)
            status = f" 🔴 {h}:{a}"

        lines.append(
            f"{time_text} "
            f"{match.get('home_team')} — "
            f"{match.get('away_team')}"
            f"{status}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=MAIN_KEYBOARD,
    )


# =========================================================
# STATUS
# =========================================================

async def show_status(update):

    text = (
        "📊 СТАТУС БОТА\n\n"
        f"Сканирований: {state['scans']}\n"
        f"Сигналов: {state['signals_sent']}\n"
        f"Ставок: {state['bets_placed']}\n"
        f"Побед: {state['wins']}\n"
        f"Поражений: {state['losses']}\n"
        f"Банк: {state['bankroll']:.2f} €\n"
        f"Подписчиков: {len(subscribers)}\n\n"
        f"Лига: {SPORT}\n"
        f"Проверка: каждые "
        f"{CHECK_EVERY_MINUTES} мин."
    )

    await update.message.reply_text(
        text,
        reply_markup=MAIN_KEYBOARD,
    )


# =========================================================
# COMMANDS
# =========================================================

async def start(update, context):

    user_id = update.effective_chat.id

    subscribers.add(user_id)

    await update.message.reply_text(
        "⚽ LIVE VALUE BOT\n\n"
        "Ты добавлен в список получателей "
        "сигналов.\n\n"
        "Стратегия:\n"
        "• LIVE 10–80 мин\n"
        "• Poisson-модель\n"
        "• счёт LIVE\n"
        "• коэффициенты букмекеров\n"
        "• Value ≥ 4%\n"
        "• Kelly 15%\n\n"
        "Выбери действие:",
        reply_markup=MAIN_KEYBOARD,
    )


async def status_command(update, context):
    await show_status(update)


async def win_command(update, context):

    state["wins"] += 1

    await update.message.reply_text(
        "✅ Победа записана."
    )


async def loss_command(update, context):

    state["losses"] += 1

    await update.message.reply_text(
        "❌ Поражение записано."
    )


async def bank_command(update, context):

    if context.args:

        try:
            value = float(
                context.args[0]
            )

            if value >= 0:
                state["bankroll"] = value

        except Exception:
            pass

    await update.message.reply_text(
        f"💰 Банк: "
        f"{state['bankroll']:.2f} €"
    )


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(
    update,
    context,
):

    text = (
        update.message.text
        if update.message
        else ""
    )

    if text == "🔴 LIVE":
        await show_live(update)

    elif text == "📅 МАТЧИ СЕГОДНЯ":
        await show_today(update)

    elif text == "📊 СТАТУС":
        await show_status(update)


# =========================================================
# CALLBACKS
# =========================================================

async def callback_handler(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    data = query.data or ""

    if data.startswith("match:"):

        match_id = data.split(
            ":",
            1,
        )[1]

        await show_match(
            query,
            match_id,
        )

    elif data == "back_live":

        await query.edit_message_text(
            "Нажми кнопку 🔴 LIVE "
            "в основном меню."
        )


# =========================================================
# AUTO SCANNER
# =========================================================

async def scanner(
    application,
):

    await asyncio.sleep(10)

    while True:

        try:

            state["scans"] += 1

            matches = await get_live_matches()

            logging.info(
                "LIVE scan: %s matches",
                len(matches),
            )

            for match in matches:

                analysis = analyse_live_match(
                    match
                )

                if not analysis:
                    continue

                if not analysis["signal"]:
                    continue

                signal_id = (
                    f"{match.get('id')}:"
                    f"{analysis['side']}:"
                    f"{analysis['minute'] // 5}"
                )

                if signal_id in sent_signals:
                    continue

                sent_signals.add(
                    signal_id
                )

                state["signals_sent"] += 1

                side = side_name(
                    match,
                    analysis["side"],
                )

                text = (
                    "🚨 LIVE VALUE\n\n"
                    f"⚽ {match_title(match)}\n"
                    f"⏱ ~{analysis['minute']}'\n"
                    f"📊 "
                    f"{analysis['home_score']}:"
                    f"{analysis['away_score']}\n\n"
                    f"🎯 {side}\n"
                    f"📈 "
                    f"{analysis['probability'] * 100:.1f}%\n"
                    f"💰 "
                    f"{analysis['odds']:.2f}\n"
                    f"💎 "
                    f"{analysis['value'] * 100:.1f}%\n"
                    f"💵 "
                    f"{analysis['stake']:.2f} €"
                )

                for chat_id in list(
                    subscribers
                ):

                    try:

                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=text,
                        )

                    except Exception as e:

                        logging.error(
                            "Telegram send error: %s",
                            e,
                        )

        except Exception as e:

            logging.error(
                "Scanner error: %s",
                e,
            )

        await asyncio.sleep(
            CHECK_EVERY_MINUTES * 60
        )


# =========================================================
# POST INIT
# =========================================================

async def post_init(application):

    application.create_task(
        scanner(application)
    )


# =========================================================
# MAIN
# =========================================================

def main():

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "win",
            win_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "loss",
            loss_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "bank",
            bank_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler,
        )
    )

    logging.info(
        "Football Live Bot started"
    )

    application.run_polling()


if __name__ == "__main__":
    main()


