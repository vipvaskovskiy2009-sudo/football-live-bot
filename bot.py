import os
import math
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

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
REGIONS = os.getenv("ODDS_REGIONS", "eu")

PORT = int(os.getenv("PORT", "10000"))

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


# =========================================================
# LOGGING
# =========================================================

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
    "wins": 0,
    "losses": 0,
    "scans": 0,
}

subscribers = set()
sent_signals = set()


# =========================================================
# TELEGRAM KEYBOARD
# =========================================================

KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔴 LIVE"],
        ["📅 МАТЧИ СЕГОДНЯ"],
        ["📊 СТАТУС"],
    ],
    resize_keyboard=True,
)


# =========================================================
# THE ODDS API
# =========================================================

async def api_get(endpoint, params=None):

    query = dict(params or {})
    query["apiKey"] = ODDS_API_KEY

    try:

        timeout = aiohttp.ClientTimeout(
            total=20
        )

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.get(
                BASE_URL + endpoint,
                params=query,
            ) as response:

                if response.status != 200:

                    body = await response.text()

                    logging.error(
                        "Odds API %s: %s",
                        response.status,
                        body[:500],
                    )

                    return None

                data = await response.json()

                logging.info(
                    "Odds API OK | remaining=%s",
                    response.headers.get(
                        "x-requests-remaining",
                        "?",
                    ),
                )

                return data

    except Exception:

        logging.exception(
            "Odds API request failed"
        )

        return None


async def fetch_scores():

    return await api_get(
        f"/sports/{SPORT}/scores",
        {
            "dateFormat": "iso",
        },
    )


async def fetch_odds():

    return await api_get(
        f"/sports/{SPORT}/odds",
        {
            "regions": REGIONS,
            "markets": "h2h",
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
            value.replace(
                "Z",
                "+00:00",
            )
        )

    except Exception:

        return None


def minute_of(match):

    start = parse_time(
        match.get(
            "commence_time",
            "",
        )
    )

    if not start:
        return 0

    seconds = (
        datetime.now(timezone.utc)
        - start
    ).total_seconds()

    return max(
        0,
        int(seconds / 60),
    )


def is_live(match):

    start = parse_time(
        match.get(
            "commence_time",
            "",
        )
    )

    if not start:
        return False

    if match.get("completed") is True:
        return False

    now = datetime.now(
        timezone.utc
    )

    minute = minute_of(match)

    return (
        start <= now
        and 1 <= minute <= 120
    )


# =========================================================
# SCORE
# =========================================================

def get_score(match):

    home = match.get("home_team")
    away = match.get("away_team")

    home_score = 0
    away_score = 0

    for item in match.get(
        "scores"
    ) or []:

        name = item.get("name")

        try:

            value = int(
                item.get("score")
            )

        except (
            TypeError,
            ValueError,
        ):

            continue

        if name == home:

            home_score = value

        elif name == away:

            away_score = value

    return (
        home_score,
        away_score,
    )


def match_title(match):

    return (
        f"{match.get('home_team', '?')}"
        f" — "
        f"{match.get('away_team', '?')}"
    )


# =========================================================
# ODDS
# =========================================================

def average_odds(match):

    home = match.get(
        "home_team"
    )

    away = match.get(
        "away_team"
    )

    values = {
        "HOME": [],
        "DRAW": [],
        "AWAY": [],
    }

    for bookmaker in (
        match.get("bookmakers")
        or []
    ):

        for market in (
            bookmaker.get("markets")
            or []
        ):

            if market.get("key") != "h2h":
                continue

            for outcome in (
                market.get("outcomes")
                or []
            ):

                name = outcome.get(
                    "name"
                )

                try:

                    price = float(
                        outcome.get(
                            "price"
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):

                    continue

                if name == home:

                    values["HOME"].append(
                        price
                    )

                elif name == away:

                    values["AWAY"].append(
                        price
                    )

                elif (
                    str(name).lower()
                    == "draw"
                ):

                    values["DRAW"].append(
                        price
                    )

    result = {}

    for key, items in values.items():

        if items:

            result[key] = (
                sum(items)
                / len(items)
            )

        else:

            result[key] = None

    return result


def remove_margin(odds):

    for key in (
        "HOME",
        "DRAW",
        "AWAY",
    ):

        if (
            odds.get(key) is None
            or odds[key] <= 1
        ):

            return None

    raw = {
        key: 1 / odds[key]
        for key in (
            "HOME",
            "DRAW",
            "AWAY",
        )
    }

    total = sum(
        raw.values()
    )

    if total <= 0:
        return None

    return {
        key: value / total
        for key, value in raw.items()
    }


# =========================================================
# POISSON
# =========================================================

def poisson_probability(
    lam,
    goals,
):

    if lam <= 0:

        if goals == 0:
            return 1.0

        return 0.0

    return (
        math.exp(-lam)
        * lam ** goals
        / math.factorial(goals)
    )


def model_probabilities(
    home_lambda,
    away_lambda,
):

    result = {
        "HOME": 0.0,
        "DRAW": 0.0,
        "AWAY": 0.0,
    }

    for home_goals in range(8):

        for away_goals in range(8):

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

                result["HOME"] += (
                    probability
                )

            elif home_goals < away_goals:

                result["AWAY"] += (
                    probability
                )

            else:

                result["DRAW"] += (
                    probability
                )

    total = sum(
        result.values()
    )

    if total <= 0:
        return None

    return {
        key: value / total
        for key, value in result.items()
    }


# =========================================================
# ANALYSIS
# =========================================================

def analyse(
    match,
    odds_match,
):

    odds = average_odds(
        odds_match
    )

    market = remove_margin(
        odds
    )

    if not market:
        return None

    minute = minute_of(
        match
    )

    if not (
        MIN_MINUTE
        <= minute
        <= MAX_MINUTE
    ):

        return None

    home_score, away_score = (
        get_score(match)
    )

    remaining = max(
        0.08,
        (90 - minute) / 90,
    )

    home_share = (
        market["HOME"]
        /
        (
            market["HOME"]
            + market["AWAY"]
        )
    )

    away_share = (
        1
        - home_share
    )

    home_lambda = (
        2.70
        * home_share
        * remaining
    )

    away_lambda = (
        2.70
        * away_share
        * remaining
    )

    # LIVE score adjustment

    if home_score < away_score:

        home_lambda *= 1.18
        away_lambda *= 0.92

    elif home_score > away_score:

        home_lambda *= 0.92
        away_lambda *= 1.18

    home_lambda = max(
        0.03,
        home_lambda,
    )

    away_lambda = max(
        0.03,
        away_lambda,
    )

    probabilities = (
        model_probabilities(
            home_lambda,
            away_lambda,
        )
    )

    if not probabilities:
        return None

    choices = []

    for side in (
        "HOME",
        "DRAW",
        "AWAY",
    ):

        probability = (
            probabilities[side]
        )

        odd = odds[side]

        if not odd:
            continue

        value = (
            probability
            * odd
            - 1
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

    value, side, probability, odd = (
        max(
            choices,
            key=lambda item: item[0],
        )
    )

    b = odd - 1

    if b > 0:

        raw_kelly = max(
            0,
            (
                b * probability
                - (1 - probability)
            )
            / b,
        )

    else:

        raw_kelly = 0

    stake = round(
        min(
            state["bankroll"]
            * 0.05,
            state["bankroll"]
            * raw_kelly
            * KELLY_FRACTION,
        ),
        2,
    )

    return {
        "minute": minute,
        "home_score": home_score,
        "away_score": away_score,
        "side": side,
        "probability": probability,
        "odds": odd,
        "value": value,
        "signal": (
            value
            >= VALUE_THRESHOLD
        ),
        "stake": stake,
    }


def side_name(
    match,
    side,
):

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
# LIVE DATA
# =========================================================

async def live_bundle():

    scores = await fetch_scores()
    odds = await fetch_odds()

    if not isinstance(
        scores,
        list,
    ):

        return []

    if not isinstance(
        odds,
        list,
    ):

        return []

    odds_by_id = {
        item.get("id"): item
        for item in odds
        if item.get("id")
    }

    result = []

    for match in scores:

        match_id = match.get(
            "id"
        )

        if not match_id:
            continue

        if not is_live(
            match
        ):

            continue

        if match_id not in odds_by_id:
            continue

        result.append(
            (
                match,
                odds_by_id[
                    match_id
                ],
            )
        )

    return result


# =========================================================
# LIVE MENU
# =========================================================

async def show_live(
    update,
):

    bundle = await live_bundle()

    if not bundle:

        await update.message.reply_text(
            "🔴 LIVE\n\n"
            "Сейчас LIVE-матчей "
            "с доступными "
            "коэффициентами не найдено.",
            reply_markup=KEYBOARD,
        )

        return

    buttons = []

    for match, _ in bundle[:20]:

        home_score, away_score = (
            get_score(match)
        )

        minute = minute_of(
            match
        )

        text = (
            f"{match.get('home_team')}"
            f" {home_score}:{away_score} "
            f"{match.get('away_team')}"
            f" ({minute}')"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    text,
                    callback_data=(
                        f"match:"
                        f"{match.get('id')}"
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

    bundle = await live_bundle()

    pair = next(
        (
            (
                match,
                odds,
            )
            for match, odds in bundle
            if match.get("id")
            == match_id
        ),
        None,
    )

    if not pair:

        await query.edit_message_text(
            "Матч уже не доступен "
            "в LIVE или коэффициенты "
            "временно отсутствуют."
        )

        return

    match, odds_match = pair

    result = analyse(
        match,
        odds_match,
    )

    home_score, away_score = (
        get_score(match)
    )

    minute = minute_of(
        match
    )

    text = (
        f"⚽ {match_title(match)}\n\n"
        f"⏱ ~{minute}'\n"
        f"📊 Счёт: "
        f"{home_score}:{away_score}\n\n"
    )

    if not result:

        text += (
            "⚪ ПРОПУСК\n\n"
            "Недостаточно данных "
            "для расчёта."
        )

    else:

        side = side_name(
            match,
            result["side"],
        )

        if result["signal"]:

            text += (
                "🟢 VALUE-СИГНАЛ\n\n"
            )

        else:

            text += (
                "⚪ ПРОПУСК\n\n"
            )

        text += (
            f"🎯 {side}\n"
            f"📈 Вероятность: "
            f"{result['probability'] * 100:.1f}%\n"
            f"💰 Коэффициент: "
            f"{result['odds']:.2f}\n"
            f"💎 Value: "
            f"{result['value'] * 100:.1f}%\n"
        )

        if result["signal"]:

            text += (
                f"💵 Расчётная ставка: "
                f"{result['stake']:.2f} €\n\n"
                "⚠️ Статистический сигнал, "
                "не автоматическая ставка."
            )

        else:

            text += (
                f"\nПорог Value: "
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
                            f"match:{match_id}"
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

async def show_today(
    update,
):

    scores = await fetch_scores()

    if not isinstance(
        scores,
        list,
    ):

        await update.message.reply_text(
            "Не удалось получить "
            "данные The Odds API.",
            reply_markup=KEYBOARD,
        )

        return

    today = datetime.now(
        timezone.utc
    ).date()

    lines = [
        "📅 МАТЧИ СЕГОДНЯ",
        "",
    ]

    for match in scores:

        start = parse_time(
            match.get(
                "commence_time",
                "",
            )
        )

        if not start:
            continue

        if start.date() != today:
            continue

        home_score, away_score = (
            get_score(match)
        )

        if is_live(match):

            status = (
                f" 🔴 "
                f"{home_score}:"
                f"{away_score}"
            )

        else:

            status = ""

        lines.append(
            f"{start.strftime('%H:%M')} "
            f"{match_title(match)}"
            f"{status}"
        )

        if len(lines) >= 32:
            break

    if len(lines) == 2:

        lines.append(
            "Сегодня матчей не найдено."
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=KEYBOARD,
    )


# =========================================================
# STATUS
# =========================================================

async def show_status(
    update,
):

    text = (
        "📊 СТАТУС БОТА\n\n"
        f"Сканирований: "
        f"{state['scans']}\n"
        f"Сигналов: "
        f"{state['signals_sent']}\n"
        f"Побед: "
        f"{state['wins']}\n"
        f"Поражений: "
        f"{state['losses']}\n"
        f"Банк: "
        f"{state['bankroll']:.2f} €\n"
        f"Подписчиков: "
        f"{len(subscribers)}\n\n"
        f"SPORT: {SPORT}\n"
        f"Проверка: "
        f"каждые "
        f"{CHECK_EVERY_MINUTES} мин."
    )

    await update.message.reply_text(
        text,
        reply_markup=KEYBOARD,
    )


# =========================================================
# COMMANDS
# =========================================================

async def start(
    update,
    context,
):

    subscribers.add(
        update.effective_chat.id
    )

    await update.message.reply_text(
        "⚽ LIVE VALUE BOT\n\n"
        "Ты добавлен в список "
        "получателей сигналов.\n\n"
        "• LIVE 10–80 мин\n"
        "• Poisson-модель\n"
        "• LIVE счёт\n"
        "• рыночные коэффициенты\n"
        "• Value ≥ 4%\n"
        "• Kelly 15%",
        reply_markup=KEYBOARD,
    )


async def status_command(
    update,
    context,
):

    await show_status(
        update
    )


async def win_command(
    update,
    context,
):

    state["wins"] += 1

    await update.message.reply_text(
        "✅ Победа записана."
    )


async def loss_command(
    update,
    context,
):

    state["losses"] += 1

    await update.message.reply_text(
        "❌ Поражение записано."
    )


async def bank_command(
    update,
    context,
):

    if context.args:

        try:

            value = float(
                context.args[0]
            )

            if value >= 0:

                state[
                    "bankroll"
                ] = value

        except ValueError:

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

    text = update.message.text

    if text == "🔴 LIVE":

        await show_live(
            update
        )

    elif text == "📅 МАТЧИ СЕГОДНЯ":

        await show_today(
            update
        )

    elif text == "📊 СТАТУС":

        await show_status(
            update
        )


# =========================================================
# CALLBACKS
# =========================================================

async def callback_handler(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    if query.data.startswith(
        "match:"
    ):

        match_id = (
            query.data.split(
                ":",
                1,
            )[1]
        )

        await show_match(
            query,
            match_id,
        )

    elif query.data == "back_live":

        await query.edit_message_text(
            "Нажми 🔴 LIVE "
            "в основном меню."
        )


# =========================================================
# AUTOMATIC SCANNER
# =========================================================

async def scanner(
    application,
):

    await asyncio.sleep(
        10
    )

    while True:

        try:

            state["scans"] += 1

            bundle = (
                await live_bundle()
            )

            logging.info(
                "LIVE scan: %s matches",
                len(bundle),
            )

            for match, odds_match in bundle:

                result = analyse(
                    match,
                    odds_match,
                )

                if not result:
                    continue

                if not result["signal"]:
                    continue

                signal_id = (
                    f"{match.get('id')}:"
                    f"{result['side']}:"
                    f"{result['minute'] // 5}"
                )

                if signal_id in sent_signals:
                    continue

                sent_signals.add(
                    signal_id
                )

                state[
                    "signals_sent"
                ] += 1

                message = (
                    "🚨 LIVE VALUE\n\n"
                    f"⚽ {match_title(match)}\n"
                    f"⏱ ~{result['minute']}'\n"
                    f"📊 "
                    f"{result['home_score']}:"
                    f"{result['away_score']}\n\n"
                    f"🎯 "
                    f"{side_name(match, result['side'])}\n"
                    f"📈 "
                    f"{result['probability'] * 100:.1f}%\n"
                    f"💰 "
                    f"{result['odds']:.2f}\n"
                    f"💎 "
                    f"{result['value'] * 100:.1f}%\n"
                    f"💵 "
                    f"{result['stake']:.2f} €"
                )

                for chat_id in list(
                    subscribers
                ):

                    try:

                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=message,
                        )

                    except Exception:

                        logging.exception(
                            "Telegram send error"
                        )

        except Exception:

            logging.exception(
                "Scanner error"
            )

        await asyncio.sleep(
            CHECK_EVERY_MINUTES
            * 60
        )


# =========================================================
# RENDER HEALTH SERVER
# =========================================================

async def health(
    request,
):

    return web.Response(
        text=(
            "Football Live Bot "
            "is running."
        )
    )


async def post_init(
    application,
):

    web_app = web.Application()

    web_app.router.add_get(
        "/",
        health,
    )

    web_app.router.add_get(
        "/health",
        health,
    )

    runner = web.AppRunner(
        web_app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    application.bot_data[
        "health_runner"
    ] = runner

    logging.info(
        "Health server listening "
        "on 0.0.0.0:%s",
        PORT,
    )

    application.create_task(
        scanner(application)
    )


async def post_shutdown(
    application,
):

    runner = application.bot_data.get(
        "health_runner"
    )

    if runner:

        await runner.cleanup()


# =========================================================
# MAIN
# =========================================================

def main():

    application = (
        Application.builder()
        .token(
            TELEGRAM_TOKEN
        )
        .post_init(
            post_init
        )
        .post_shutdown(
            post_shutdown
        )
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
        "Football Live Bot starting"
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()


