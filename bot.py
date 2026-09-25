"""
Live Football Value Betting Bot (multi-league)
Стек: The Odds API + Telegram + Render
"""

import os
import math
import asyncio
import logging
from datetime import datetime, timezone, timedelta

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

# Все лиги, которые проверяем. The Odds API не покрывает Лигу наций —
# если матчей нет в топ-лигах, бот ничего не найдёт.
SPORTS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_england_championship",
]

REGIONS = os.getenv("ODDS_REGIONS", "eu")
PORT = int(os.getenv("PORT", "10000"))

CHECK_EVERY_MINUTES = int(os.getenv("CHECK_EVERY_MINUTES", "30"))

MIN_MINUTE = 5
MAX_MINUTE = 85

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
    "api_calls": 0,
}

subscribers = set()
sent_signals = set()

# Кэш активных видов спорта (обновляем раз в 6 часов)
sports_cache = {"data": None, "updated": None}
SPORTS_CACHE_TTL_HOURS = 6


# =========================================================
# KEYBOARD
# =========================================================

KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔴 LIVE"],
        ["📅 МАТЧИ СЕГОДНЯ"],
        ["📊 СТАТУС", "🌍 ЛИГИ"],
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
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(BASE_URL + endpoint, params=query) as response:
                state["api_calls"] += 1
                if response.status != 200:
                    body = await response.text()
                    logging.error("Odds API %s: %s", response.status, body[:300])
                    return None
                data = await response.json()
                logging.info(
                    "Odds API OK | endpoint=%s | remaining=%s",
                    endpoint,
                    response.headers.get("x-requests-remaining", "?"),
                )
                return data
    except Exception:
        logging.exception("Odds API request failed")
        return None


async def fetch_active_sports():
    """Возвращает список активных soccer-лиг (с кэшем)."""
    now = datetime.now(timezone.utc)
    if (
        sports_cache["data"] is not None
        and sports_cache["updated"] is not None
        and (now - sports_cache["updated"]).total_seconds()
            < SPORTS_CACHE_TTL_HOURS * 3600
    ):
        return sports_cache["data"]

    data = await api_get("/sports/")
    if not isinstance(data, list):
        return sports_cache["data"] or []

    active = [
        s for s in data
        if s.get("active")
        and s.get("group", "").lower() == "soccer"
        and not s.get("has_outrights")
    ]
    sports_cache["data"] = active
    sports_cache["updated"] = now
    logging.info("Active soccer sports: %s", [s["key"] for s in active])
    return active


async def fetch_scores(sport):
    return await api_get(
        f"/sports/{sport}/scores",
        {"daysFrom": 1, "dateFormat": "iso"},
    )


async def fetch_odds(sport):
    return await api_get(
        f"/sports/{sport}/odds",
        {
            "regions": REGIONS,
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
    )


# =========================================================
# TIME HELPERS
# =========================================================

def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def minute_of(match):
    start = parse_time(match.get("commence_time", ""))
    if not start:
        return 0
    seconds = (datetime.now(timezone.utc) - start).total_seconds()
    return max(0, int(seconds / 60))


def is_live(match):
    start = parse_time(match.get("commence_time", ""))
    if not start:
        return False
    if match.get("completed") is True:
        return False
    now = datetime.now(timezone.utc)
    minute = minute_of(match)
    # Футбол: обычно 90 + добавленное + перерыв. До 130 минут — ок.
    return start <= now and 1 <= minute <= 130


def is_today(match):
    start = parse_time(match.get("commence_time", ""))
    if not start:
        return False
    return start.date() == datetime.now(timezone.utc).date()


# =========================================================
# SCORE
# =========================================================

def get_score(match):
    home = match.get("home_team")
    away = match.get("away_team")
    hs = 0
    aws = 0
    for item in match.get("scores") or []:
        name = item.get("name")
        try:
            value = int(item.get("score"))
        except (TypeError, ValueError):
            continue
        if name == home:
            hs = value
        elif name == away:
            aws = value
    return hs, aws


def match_title(match):
    return f"{match.get('home_team', '?')} — {match.get('away_team', '?')}"


# =========================================================
# ODDS
# =========================================================

def average_odds(match):
    home = match.get("home_team")
    away = match.get("away_team")
    values = {"HOME": [], "DRAW": [], "AWAY": []}
    for bookmaker in match.get("bookmakers") or []:
        for market in bookmaker.get("markets") or []:
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes") or []:
                name = outcome.get("name")
                try:
                    price = float(outcome.get("price"))
                except (TypeError, ValueError):
                    continue
                if name == home:
                    values["HOME"].append(price)
                elif name == away:
                    values["AWAY"].append(price)
                elif str(name).lower() == "draw":
                    values["DRAW"].append(price)
    result = {}
    for key, items in values.items():
        result[key] = (sum(items) / len(items)) if items else None
    return result


def remove_margin(odds):
    for key in ("HOME", "DRAW", "AWAY"):
        if odds.get(key) is None or odds[key] <= 1:
            return None
    raw = {key: 1 / odds[key] for key in ("HOME", "DRAW", "AWAY")}
    total = sum(raw.values())
    if total <= 0:
        return None
    return {key: value / total for key, value in raw.items()}


# =========================================================
# POISSON
# =========================================================

def poisson_probability(lam, goals):
    if lam <= 0:
        return 1.0 if goals == 0 else 0.0
    return math.exp(-lam) * lam ** goals / math.factorial(goals)


def model_probabilities(home_lambda, away_lambda):
    result = {"HOME": 0.0, "DRAW": 0.0, "AWAY": 0.0}
    for hg in range(8):
        for ag in range(8):
            p = poisson_probability(home_lambda, hg) * poisson_probability(away_lambda, ag)
            if hg > ag:
                result["HOME"] += p
            elif hg < ag:
                result["AWAY"] += p
            else:
                result["DRAW"] += p
    total = sum(result.values())
    if total <= 0:
        return None
    return {k: v / total for k, v in result.items()}


# =========================================================
# ANALYSIS
# =========================================================

def analyse(match, odds_match):
    odds = average_odds(odds_match)
    market = remove_margin(odds)
    if not market:
        return None

    minute = minute_of(match)
    if not (MIN_MINUTE <= minute <= MAX_MINUTE):
        return None

    home_score, away_score = get_score(match)
    remaining = max(0.08, (90 - minute) / 90)

    home_share = market["HOME"] / (market["HOME"] + market["AWAY"])
    away_share = 1 - home_share

    home_lambda = 2.70 * home_share * remaining
    away_lambda = 2.70 * away_share * remaining

    if home_score < away_score:
        home_lambda *= 1.18
        away_lambda *= 0.92
    elif home_score > away_score:
        home_lambda *= 0.92
        away_lambda *= 1.18

    home_lambda = max(0.03, home_lambda)
    away_lambda = max(0.03, away_lambda)

    probabilities = model_probabilities(home_lambda, away_lambda)
    if not probabilities:
        return None

    choices = []
    for side in ("HOME", "DRAW", "AWAY"):
        prob = probabilities[side]
        odd = odds[side]
        if not odd:
            continue
        value = prob * odd - 1
        choices.append((value, side, prob, odd))

    if not choices:
        return None

    value, side, probability, odd = max(choices, key=lambda x: x[0])

    b = odd - 1
    raw_kelly = max(0, (b * probability - (1 - probability)) / b) if b > 0 else 0
    stake = round(
        min(state["bankroll"] * 0.05, state["bankroll"] * raw_kelly * KELLY_FRACTION),
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
        "signal": value >= VALUE_THRESHOLD,
        "stake": stake,
    }


def side_name(match, side):
    if side == "HOME":
        return match.get("home_team", "HOME")
    if side == "AWAY":
        return match.get("away_team", "AWAY")
    return "НИЧЬЯ"


# =========================================================
# LIVE BUNDLE (multi-league)
# =========================================================

async def live_bundle():
    """
    Возвращает список (sport, match_with_scores, odds_match).
    Проходит по всем лигам из SPORTS.
    Сначала тянем odds (дешевле по логике: сразу видно есть ли матчи),
    потом только для лиг с live-матчами тянем scores.
    """
    result = []

    # Шаг 1: собираем odds по всем лигам
    odds_by_sport = {}
    for sport in SPORTS:
        data = await fetch_odds(sport)
        if isinstance(data, list) and data:
            odds_by_sport[sport] = data
            logging.info("%s: odds %d matches", sport, len(data))
        else:
            logging.info("%s: no odds", sport)
        await asyncio.sleep(0.4)  # не спамим API

    # Шаг 2: определяем, в каких лигах есть матчи, которые сейчас live
    # (по времени). Для них тянем scores.
    sports_with_live = set()
    for sport, matches in odds_by_sport.items():
        for m in matches:
            start = parse_time(m.get("commence_time", ""))
            if not start:
                continue
            now = datetime.now(timezone.utc)
            minute = (now - start).total_seconds() / 60
            if start <= now and 1 <= minute <= 130:
                sports_with_live.add(sport)
                break

    logging.info("Sports with potentially live matches: %s", sports_with_live)

    # Шаг 3: для каждой такой лиги тянем scores и джойним по id
    for sport in sports_with_live:
        scores = await fetch_scores(sport)
        await asyncio.sleep(0.4)
        if not isinstance(scores, list):
            continue

        scores_by_id = {s.get("id"): s for s in scores if s.get("id")}

        for odds_match in odds_by_sport.get(sport, []):
            mid = odds_match.get("id")
            if not mid:
                continue
            sm = scores_by_id.get(mid)
            if not sm:
                continue
            if not is_live(sm):
                continue
            result.append((sport, sm, odds_match))

    return result


# =========================================================
# MENUS
# =========================================================

async def show_live(update):
    bundle = await live_bundle()

    if not bundle:
        await update.message.reply_text(
            "🔴 LIVE\n\n"
            "Сейчас LIVE-матчей с доступными коэффициентами не найдено.\n\n"
            "Возможные причины:\n"
            "• сегодня нет матчей в топ-лигах (The Odds API не покрывает "
            "Лигу наций и товарищеские)\n"
            "• лимит запросов The Odds API исчерпан\n"
            "• матчи идут, но букмекеры не дают h2h на бесплатном тире",
            reply_markup=KEYBOARD,
        )
        return

    buttons = []
    for sport, sm, _ in bundle[:20]:
        hs, aws = get_score(sm)
        minute = minute_of(sm)
        text = (
            f"{sm.get('home_team')} {hs}:{aws} {sm.get('away_team')} ({minute}')"
        )
        buttons.append(
            [InlineKeyboardButton(text, callback_data=f"match:{sm.get('id')}")]
        )

    await update.message.reply_text(
        f"🔴 LIVE — найдено {len(bundle)} матчей:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_match(query, match_id):
    bundle = await live_bundle()
    pair = next(
        ((sport, sm, om) for sport, sm, om in bundle if sm.get("id") == match_id),
        None,
    )

    if not pair:
        await query.edit_message_text(
            "Матч уже не доступен в LIVE или коэффициенты временно отсутствуют."
        )
        return

    sport, sm, om = pair
    result = analyse(sm, om)
    hs, aws = get_score(sm)
    minute = minute_of(sm)

    text = (
        f"⚽ {match_title(sm)}\n"
        f"🏆 {sport}\n\n"
        f"⏱ ~{minute}'\n"
        f"📊 Счёт: {hs}:{aws}\n\n"
    )

    if not result:
        text += "⚪ ПРОПУСК\n\nНедостаточно данных для расчёта."
    else:
        side = side_name(sm, result["side"])
        if result["signal"]:
            text += "🟢 VALUE-СИГНАЛ\n\n"
        else:
            text += "⚪ ПРОПУСК\n\n"
        text += (
            f"🎯 {side}\n"
            f"📈 Вероятность: {result['probability'] * 100:.1f}%\n"
            f"💰 Коэффициент: {result['odds']:.2f}\n"
            f"💎 Value: {result['value'] * 100:.1f}%\n"
        )
        if result["signal"]:
            text += (
                f"💵 Расчётная ставка: {result['stake']:.2f} €\n\n"
                "⚠️ Статистический сигнал, не автоматическая ставка."
            )
        else:
            text += f"\nПорог Value: {VALUE_THRESHOLD * 100:.0f}%."

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Обновить", callback_data=f"match:{match_id}")],
                [InlineKeyboardButton("🔴 LIVE", callback_data="back_live")],
            ]
        ),
    )


async def show_today(update):
    all_matches = []
    for sport in SPORTS:
        data = await fetch_odds(sport)
        await asyncio.sleep(0.3)
        if not isinstance(data, list):
            continue
        for m in data:
            if is_today(m):
                all_matches.append((sport, m))

    if not all_matches:
        await update.message.reply_text(
            "📅 МАТЧИ СЕГОДНЯ\n\nСегодня матчей не найдено.",
            reply_markup=KEYBOARD,
        )
        return

    all_matches.sort(key=lambda x: x[1].get("commence_time", ""))

    lines = ["📅 МАТЧИ СЕГОДНЯ", ""]
    for sport, m in all_matches[:40]:
        start = parse_time(m.get("commence_time", ""))
        if not start:
            continue
        status = " 🔴 LIVE" if is_live(m) else ""
        short_sport = sport.replace("soccer_", "").replace("_", " ")[:14]
        lines.append(
            f"{start.strftime('%H:%M')} [{short_sport}] "
            f"{match_title(m)}{status}"
        )

    await update.message.reply_text("\n".join(lines), reply_markup=KEYBOARD)


async def show_status(update):
    text = (
        "📊 СТАТУС БОТА\n\n"
        f"Сканирований: {state['scans']}\n"
        f"Сигналов: {state['signals_sent']}\n"
        f"Побед: {state['wins']}\n"
        f"Поражений: {state['losses']}\n"
        f"Банк: {state['bankroll']:.2f} €\n"
        f"Подписчиков: {len(subscribers)}\n"
        f"API-вызовов: {state['api_calls']}\n\n"
        f"Лиг в списке: {len(SPORTS)}\n"
        f"Проверка: каждые {CHECK_EVERY_MINUTES} мин."
    )
    await update.message.reply_text(text, reply_markup=KEYBOARD)


async def show_sports(update):
    """Показывает активные лиги на ключе пользователя."""
    active = await fetch_active_sports()
    if not active:
        await update.message.reply_text(
            "🌍 ЛИГИ\n\nНе удалось получить список. Проверь API-ключ.",
            reply_markup=KEYBOARD,
        )
        return

    lines = ["🌍 АКТИВНЫЕ СОККЕР-ЛИГИ (The Odds API)", ""]
    for s in active:
        marker = "✅" if s["key"] in SPORTS else "▫️"
        lines.append(f"{marker} {s['key']} — {s.get('title', '')}")
    lines.append("")
    lines.append("✅ = в списке бота, ▫️ = не сканируется")

    await update.message.reply_text("\n".join(lines[:60]), reply_markup=KEYBOARD)


# =========================================================
# COMMANDS
# =========================================================

async def start(update, context):
    subscribers.add(update.effective_chat.id)
    await update.message.reply_text(
        "⚽ LIVE VALUE BOT (multi-league)\n\n"
        "Сканирую топ-лиги каждые "
        f"{CHECK_EVERY_MINUTES} мин.\n\n"
        "• LIVE 5–85 мин\n"
        "• Poisson-модель + рыночные коэффициенты\n"
        "• Value ≥ 4%\n"
        "• Kelly 15%\n\n"
        "Команды:\n"
        "/status /win /loss /bank /sports",
        reply_markup=KEYBOARD,
    )


async def status_command(update, context):
    await show_status(update)


async def win_command(update, context):
    state["wins"] += 1
    await update.message.reply_text("✅ Победа записана.")


async def loss_command(update, context):
    state["losses"] += 1
    await update.message.reply_text("❌ Поражение записано.")


async def bank_command(update, context):
    if context.args:
        try:
            value = float(context.args[0])
            if value >= 0:
                state["bankroll"] = value
        except ValueError:
            pass
    await update.message.reply_text(f"💰 Банк: {state['bankroll']:.2f} €")


async def sports_command(update, context):
    await show_sports(update)


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(update, context):
    text = update.message.text
    if text == "🔴 LIVE":
        await show_live(update)
    elif text == "📅 МАТЧИ СЕГОДНЯ":
        await show_today(update)
    elif text == "📊 СТАТУС":
        await show_status(update)
    elif text == "🌍 ЛИГИ":
        await show_sports(update)


# =========================================================
# CALLBACKS
# =========================================================

async def callback_handler(update, context):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("match:"):
        match_id = query.data.split(":", 1)[1]
        await show_match(query, match_id)
    elif query.data == "back_live":
        await query.edit_message_text("Нажми 🔴 LIVE в основном меню.")


# =========================================================
# SCANNER
# =========================================================

async def scanner(application):
    await asyncio.sleep(20)
    while True:
        try:
            state["scans"] += 1
            bundle = await live_bundle()
            logging.info("LIVE scan: %d matches", len(bundle))

            for sport, sm, om in bundle:
                result = analyse(sm, om)
                if not result or not result["signal"]:
                    continue

                signal_id = f"{sm.get('id')}:{result['side']}:{result['minute'] // 5}"
                if signal_id in sent_signals:
                    continue
                sent_signals.add(signal_id)

                state["signals_sent"] += 1
                message = (
                    "🚨 LIVE VALUE\n\n"
                    f"🏆 {sport}\n"
                    f"⚽ {match_title(sm)}\n"
                    f"⏱ ~{result['minute']}'\n"
                    f"📊 {result['home_score']}:{result['away_score']}\n\n"
                    f"🎯 {side_name(sm, result['side'])}\n"
                    f"📈 {result['probability'] * 100:.1f}%\n"
                    f"💰 {result['odds']:.2f}\n"
                    f"💎 {result['value'] * 100:.1f}%\n"
                    f"💵 {result['stake']:.2f} €"
                )

                for chat_id in list(subscribers):
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id, text=message
                        )
                    except Exception:
                        logging.exception("Telegram send error")
        except Exception:
            logging.exception("Scanner error")

        await asyncio.sleep(CHECK_EVERY_MINUTES * 60)


# =========================================================
# HEALTH SERVER (для Render Web Service)
# =========================================================

async def health(request):
    return web.Response(text="Football Live Bot is running.")


async def post_init(application):
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    application.bot_data["health_runner"] = runner
    logging.info("Health server on 0.0.0.0:%s", PORT)
    application.create_task(scanner(application))


async def post_shutdown(application):
    runner = application.bot_data.get("health_runner")
    if runner:
        await runner.cleanup()


# =========================================================
# MAIN
# =========================================================

def main():
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("win", win_command))
    application.add_handler(CommandHandler("loss", loss_command))
    application.add_handler(CommandHandler("bank", bank_command))
    application.add_handler(CommandHandler("sports", sports_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    logging.info("Football Live Bot starting")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()


