import os
import math
import asyncio
import logging
from datetime import datetime, timezone
from aiohttp import web, ClientSession
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Основная лига.
# Для экономии лимита The Odds API оставляем одну лигу.
SPORT = os.getenv("SPORT", "soccer_epl")

REGIONS = "eu"
MARKETS = "h2h"

# Стратегия
CHECK_EVERY_MINUTES = 15
KELLY_FRACTION = 0.15
VALUE_THRESHOLD = 0.04

MIN_MINUTE = 10
MAX_MINUTE = 80

BANKROLL_START = 100.0

PORT = int(os.getenv("PORT", "10000"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN / BOT_TOKEN not found")

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
    "bets_placed": 0,
    "total_staked": 0.0,
    "wins": 0,
    "losses": 0,
    "signals_sent": 0,
    "scans": 0,
    "last_scan": "—",
    "api_remaining": "?",
}

subscribers = set()


# =========================================================
# THE ODDS API
# =========================================================

async def fetch_odds(session):
    url = (
        f"{ODDS_API_BASE}/sports/{SPORT}/odds"
        f"?apiKey={ODDS_API_KEY}"
        f"&regions={REGIONS}"
        f"&markets={MARKETS}"
        f"&oddsFormat=decimal"
        f"&dateFormat=iso"
    )

    try:
        async with session.get(url, timeout=30) as response:

            remaining = response.headers.get(
                "x-requests-remaining",
                "?"
            )

            state["api_remaining"] = remaining

            if response.status != 200:
                text = await response.text()

                logging.warning(
                    f"OddsAPI {response.status}: {text}"
                )

                return []

            data = await response.json()

            logging.info(
                f"OddsAPI: {len(data)} matches | "
                f"remaining: {remaining}"
            )

            return data

    except Exception as e:
        logging.error(
            f"OddsAPI error: {e}"
        )
        return []


def parse_match_odds(match):

    home_odds = []
    draw_odds = []
    away_odds = []

    for bookmaker in match.get(
        "bookmakers",
        []
    ):

        for market in bookmaker.get(
            "markets",
            []
        ):

            if market.get("key") != "h2h":
                continue

            for outcome in market.get(
                "outcomes",
                []
            ):

                name = outcome.get("name")
                price = outcome.get("price")

                if name is None or price is None:
                    continue

                try:
                    price = float(price)
                except Exception:
                    continue

                if name == match["home_team"]:
                    home_odds.append(price)

                elif name == match["away_team"]:
                    away_odds.append(price)

                elif name.lower() == "draw":
                    draw_odds.append(price)

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

def parse_minute(value):

    if value is None:
        return 0

    if isinstance(value, int):
        return value

    text = str(value).strip()

    digits = ""

    for char in text:
        if char.isdigit():
            digits += char
        else:
            break

    try:
        return int(digits)
    except Exception:
        return 0


def get_red_cards(match):

    red_home = 0
    red_away = 0

    events = getattr(
        match,
        "events",
        []
    ) or []

    home = (
        getattr(
            match,
            "home_team_name",
            ""
        ) or ""
    ).lower()

    away = (
        getattr(
            match,
            "away_team_name",
            ""
        ) or ""
    ).lower()

    for event in events:

        event_type = str(
            getattr(
                event,
                "type",
                ""
            ) or ""
        ).lower()

        description = str(
            getattr(
                event,
                "description",
                ""
            ) or ""
        ).lower()

        combined = (
            event_type + " " + description
        )

        if "red" not in combined:
            continue

        # Исключаем просто текст про возможную карточку
        # и считаем событие красной карточкой.
        event_text = str(event).lower()

        if home and home in event_text:
            red_home += 1

        elif away and away in event_text:
            red_away += 1

        else:
            # Если сторону определить нельзя,
            # не приписываем карточку случайно.
            continue

    return red_home, red_away


async def fetch_flashscore_live():

    result = {}

    try:

        from flashscore import FlashscoreApi

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

            minute = parse_minute(
                getattr(
                    match,
                    "minute",
                    0
                )
            )

            home_score = getattr(
                match,
                "home_team_score",
                0
            ) or 0

            away_score = getattr(
                match,
                "away_team_score",
                0
            ) or 0

            red_home, red_away = get_red_cards(
                match
            )

            key = (
                f"{home}-{away}"
                .lower()
            )

            result[key] = {
                "minute": minute,
                "score": (
                    int(home_score),
                    int(away_score)
                ),
                "red_home": red_home,
                "red_away": red_away,
            }

    except ImportError:

        logging.error(
            "fs-football-fork is not installed"
        )

    except Exception as e:

        logging.error(
            f"Flashscore error: {e}"
        )

    logging.info(
        f"Flashscore LIVE: {len(result)}"
    )

    return result


def match_flashscore(
    odds_match,
    flash_data
):

    home = (
        odds_match["home_team"]
        .lower()
    )

    away = (
        odds_match["away_team"]
        .lower()
    )

    # Сначала точное совпадение
    exact_key = f"{home}-{away}"

    if exact_key in flash_data:
        return flash_data[exact_key]

    # Затем более мягкое совпадение
    home_words = home.split()
    away_words = away.split()

    if not home_words or not away_words:
        return None

    home_first = home_words[0]
    away_first = away_words[0]

    for key, value in flash_data.items():

        if (
            home_first in key
            and away_first in key
        ):
            return value

    return None


# =========================================================
# POISSON
# =========================================================

def poisson_prob(lam, k):

    if lam <= 0:
        return (
            1.0
            if k == 0
            else 0.0
        )

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
    red_away
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

    lam_h = max(
        0.05,
        base_home
        * time_left
        * 1.10
    )

    lam_a = max(
        0.05,
        base_away
        * time_left
        * 0.90
    )

    return lam_h, lam_a


def match_probabilities(
    lam_h,
    lam_a,
    max_goals=6
):

    p_home = 0.0
    p_draw = 0.0
    p_away = 0.0

    for i in range(max_goals + 1):

        for j in range(max_goals + 1):

            p = (
                poisson_prob(lam_h, i)
                * poisson_prob(lam_a, j)
            )

            if i > j:
                p_home += p

            elif i == j:
                p_draw += p

            else:
                p_away += p

    total = (
        p_home
        + p_draw
        + p_away
    )

    if total == 0:
        return None

    return {
        "HOME": p_home / total,
        "DRAW": p_draw / total,
        "AWAY": p_away / total,
    }


# =========================================================
# KELLY
# =========================================================

def kelly_stake(
    probability,
    odds,
    bankroll
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
        * max(0.0, kelly)
        * KELLY_FRACTION
    )

    return round(
        stake,
        2
    )


# =========================================================
# TELEGRAM
# =========================================================

async def send_to_all(
    application,
    text
):

    if not subscribers:
        logging.warning(
            "Нет подписчиков Telegram"
        )
        return

    dead = []

    for chat_id in list(subscribers):

        try:

            await application.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML"
            )

        except Exception as e:

            logging.error(
                f"Telegram error {chat_id}: {e}"
            )

            dead.append(chat_id)

    for chat_id in dead:
        subscribers.discard(chat_id)


# =========================================================
# ANALYSIS
# =========================================================

async def analyze_and_send(
    application,
    odds_match,
    flash
):

    home = odds_match["home_team"]
    away = odds_match["away_team"]

    parsed = parse_match_odds(
        odds_match
    )

    if not all([
        parsed["HOME"],
        parsed["DRAW"],
        parsed["AWAY"]
    ]):
        return

    if not flash:
        return

    minute = flash["minute"]

    h_goals, a_goals = flash["score"]

    # ФИЛЬТР МИНУТЫ
    if (
        minute < MIN_MINUTE
        or minute > MAX_MINUTE
    ):
        return

    score_diff = (
        h_goals - a_goals
    )

    # Базовая интенсивность
    base_home = 1.4
    base_away = 1.1

    lam_h, lam_a = (
        calculate_live_intensity(
            base_home,
            base_away,
            minute,
            score_diff,
            flash["red_home"],
            flash["red_away"]
        )
    )

    probs = match_probabilities(
        lam_h,
        lam_a
    )

    if not probs:
        return

    odds_map = {
        "HOME": parsed["HOME"],
        "DRAW": parsed["DRAW"],
        "AWAY": parsed["AWAY"]
    }

    label_map = {
        "HOME": "П1",
        "DRAW": "X",
        "AWAY": "П2"
    }

    # Проверяем HOME / DRAW / AWAY
    for outcome, probability in probs.items():

        odd = odds_map[outcome]

        if not odd or odd <= 1.05:
            continue

        # Вероятность букмекера
        implied = 1 / odd

        # VALUE
        value = (
            probability
            - implied
        )

        # Минимум +4%
        if value <= VALUE_THRESHOLD:
            continue

        # Kelly 15%
        stake = kelly_stake(
            probability,
            odd,
            state["bankroll"]
        )

        if stake < 1:
            continue

        label = label_map[outcome]

        msg = (
            "⚽ <b>LIVE VALUE</b>\n\n"
            f"<b>{home} — {away}</b>\n"
            f"Счёт: <b>{h_goals}:{a_goals}</b> | "
            f"{minute}'\n\n"
            f"🎯 Ставка: <b>{label}</b>\n"
            f"💰 Коэф.: <b>{odd:.2f}</b>\n\n"
            f"🤖 Наша вероятность: "
            f"<b>{probability * 100:.1f}%</b>\n"
            f"📉 Вероятность по коэффициенту: "
            f"{implied * 100:.1f}%\n"
            f"📈 VALUE: "
            f"<b>+{value * 100:.1f}%</b>\n\n"
            f"💵 Kelly 15%: "
            f"<b>${stake:.2f}</b>\n\n"
            f"🟥 Красные: "
            f"{flash['red_home']} — "
            f"{flash['red_away']}\n\n"
            "⚠️ Сигнал рассчитан моделью; "
            "ставка не является гарантией результата."
        )

        await send_to_all(
            application,
            msg
        )

        state["signals_sent"] += 1
        state["bets_placed"] += 1
        state["total_staked"] += stake

        logging.info(
            f"SIGNAL | {home} vs {away} | "
            f"{outcome} @ {odd:.2f} | "
            f"value={value:.4f}"
        )

        # Только один сигнал на матч за проход анализа
        return


# =========================================================
# MAIN SCANNER
# =========================================================

async def scan_once(
    application
):

    state["scans"] += 1

    state["last_scan"] = (
        datetime.now(timezone.utc)
        .strftime("%H:%M:%S UTC")
    )

    logging.info(
        f"========== SCAN #{state['scans']} =========="
    )

    async with ClientSession() as session:

        odds_list = await fetch_odds(
            session
        )

    if not odds_list:

        logging.info(
            "Odds API: матчей нет"
        )

        return

    flash = await asyncio.to_thread(
        fetch_flashscore_live
    )

    if not flash:

        logging.info(
            "Flashscore: LIVE матчей нет"
        )

        return

    for match in odds_list:

        flash_match = match_flashscore(
            match,
            flash
        )

        if not flash_match:
            continue

        await analyze_and_send(
            application,
            match,
            flash_match
        )

        await asyncio.sleep(0.5)


async def scanner_loop(
    application
):

    logging.info(
        "LIVE scanner started"
    )

    while True:

        try:

            await scan_once(
                application
            )

        except Exception as e:

            logging.exception(
                f"Scanner error: {e}"
            )

        await asyncio.sleep(
            CHECK_EVERY_MINUTES * 60
        )


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    subscribers.add(chat_id)

    await update.message.reply_text(
        "⚽ <b>LIVE VALUE BOT</b>\n\n"
        "Ты добавлен в список получателей сигналов.\n\n"
        "Стратегия:\n"
        "• минута 10–80\n"
        "• Poisson\n"
        "• счёт LIVE\n"
        "• красные карточки\n"
        "• Value ≥ 4%\n"
        "• Kelly 15%\n\n"
        "/status — статистика\n"
        "/win — записать WIN\n"
        "/loss — записать LOSS\n"
        "/bank 100 — установить банк",
        parse_mode="HTML"
    )


async def cmd_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    total = (
        state["wins"]
        + state["losses"]
    )

    winrate = (
        state["wins"] / total * 100
        if total
        else 0
    )

    await update.message.reply_text(
        "📊 <b>СТАТУС</b>\n\n"
        f"💰 Банк: ${state['bankroll']:.2f}\n"
        f"📡 Сканирований: {state['scans']}\n"
        f"🟢 Сигналов: {state['signals_sent']}\n"
        f"💵 Сумма ставок: ${state['total_staked']:.2f}\n"
        f"✅ WIN: {state['wins']}\n"
        f"❌ LOSS: {state['losses']}\n"
        f"📈 Winrate: {winrate:.1f}%\n"
        f"👥 Подписчиков: {len(subscribers)}\n"
        f"🔢 API осталось: {state['api_remaining']}\n"
        f"🕐 Последний скан: {state['last_scan']}",
        parse_mode="HTML"
    )


async def cmd_win(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    state["wins"] += 1

    await update.message.reply_text(
        "✅ WIN записан."
    )


async def cmd_loss(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    state["losses"] += 1

    await update.message.reply_text(
        "❌ LOSS записан."
    )


async def cmd_bank(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        amount = float(
            context.args[0]
        )

        if amount <= 0:
            raise ValueError

        state["bankroll"] = amount

        await update.message.reply_text(
            f"💰 Банк установлен: "
            f"${amount:.2f}"
        )

    except (IndexError, ValueError):

        await update.message.reply_text(
            "Использование:\n"
            "/bank 100"
        )


# =========================================================
# RENDER HEALTH
# =========================================================

async def health(request):

    return web.Response(
        text=(
            "Football Live Value Bot "
            "is running."
        )
    )


async def start_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logging.info(
        f"Health server on port {PORT}"
    )


# =========================================================
# START
# =========================================================

async def post_init(
    application: Application
):

    await start_health_server()

    asyncio.create_task(
        scanner_loop(application)
    )

    logging.info(
        "Scanner task created."
    )


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
            cmd_start
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            cmd_status
        )
    )

    application.add_handler(
        CommandHandler(
            "win",
            cmd_win
        )
    )

    application.add_handler(
        CommandHandler(
            "loss",
            cmd_loss
        )
    )

    application.add_handler(
        CommandHandler(
            "bank",
            cmd_bank
        )
    )

    logging.info(
        "Football Live Value Bot starting..."
    )

    application.run_polling()


if __name__ == "__main__":
    main()
