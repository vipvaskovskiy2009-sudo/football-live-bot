import os
import json
import time
import threading
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

BOT_TOKEN = os.environ["BOT_TOKEN"]
FOOTBALL_API_KEY = os.environ["FOOTBALL_API_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
FOOTBALL_API = "https://v3.football.api-sports.io/"

HEADERS = {
    "x-apisports-key": FOOTBALL_API_KEY
}


def telegram(method, data=None):
    url = TELEGRAM_API + method

    if data:
        body = urllib.parse.urlencode(data).encode()
        request = urllib.request.Request(url, data=body)
    else:
        request = urllib.request.Request(url)

    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def football(endpoint, params=None):
    url = FOOTBALL_API + endpoint

    if params:
        url += "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(
        url,
        headers=HEADERS
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def send_message(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": text
    }

    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)

    telegram("sendMessage", data)


def answer_callback(callback_id):
    try:
        telegram(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id
            }
        )
    except Exception:
        pass


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
            ]
        ]
    }


def get_live_matches():
    data = football(
        "fixtures",
        {
            "live": "all"
        }
    )

    return data.get("response", [])


def get_today_matches():
    today = datetime.utcnow().strftime("%Y-%m-%d")

    data = football(
        "fixtures",
        {
            "date": today,
            "timezone": "Europe/Tallinn"
        }
    )

    return data.get("response", [])


def match_name(match):
    home = match["teams"]["home"]["name"]
    away = match["teams"]["away"]["name"]
    return f"{home} — {away}"


def live_keyboard(matches):
    buttons = []

    for match in matches[:30]:
        fixture_id = match["fixture"]["id"]

        buttons.append(
            [
                {
                    "text": (
                        f'{match["fixture"]["status"].get("elapsed") or "?"}′ '
                        f'{match["teams"]["home"]["name"]} '
                        f'{match["goals"]["home"] or 0}:'
                        f'{match["goals"]["away"] or 0} '
                        f'{match["teams"]["away"]["name"]}'
                    ),
                    "callback_data": f"match:{fixture_id}"
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
        "inline_keyboard": buttons
    }


def today_keyboard(matches):
    buttons = []

    for match in matches[:30]:
        fixture_id = match["fixture"]["id"]

        home = match["teams"]["home"]["name"]
        away = match["teams"]["away"]["name"]

        status = match["fixture"]["status"]["short"]

        buttons.append(
            [
                {
                    "text": f"{home} — {away} [{status}]",
                    "callback_data": f"match:{fixture_id}"
                }
            ]
        )

    if not buttons:
        buttons.append(
            [
                {
                    "text": "🔄 Обновить",
                    "callback_data": "today"
                }
            ]
        )

    return {
        "inline_keyboard": buttons
    }


def get_match_stats(fixture_id):
    data = football(
        "fixtures/statistics",
        {
            "fixture": fixture_id
        }
    )

    return data.get("response", [])


def get_match(fixture_id):
    data = football(
        "fixtures",
        {
            "id": fixture_id
        }
    )

    response = data.get("response", [])

    if not response:
        return None

    return response[0]


def stat_value(stats, name):
    for item in stats:
        if item.get("type") == name:
            value = item.get("value")

            if value is None:
                return 0

            if isinstance(value, str):
                value = value.replace("%", "")

                try:
                    return float(value)
                except Exception:
                    return 0

            return value

    return 0


def analyze_match(match, stats):
    if len(stats) < 2:
        return (
            "⚪ ПРОПУСК\n\n"
            "Недостаточно LIVE-статистики для анализа."
        )

    home_stats = stats[0].get("statistics", [])
    away_stats = stats[1].get("statistics", [])

    home_shots = stat_value(home_stats, "Total Shots")
    away_shots = stat_value(away_stats, "Total Shots")

    home_target = stat_value(home_stats, "Shots on Goal")
    away_target = stat_value(away_stats, "Shots on Goal")

    home_corners = stat_value(home_stats, "Corner Kicks")
    away_corners = stat_value(away_stats, "Corner Kicks")

    home_possession = stat_value(home_stats, "Ball Possession")
    away_possession = stat_value(away_stats, "Ball Possession")

    total_shots = home_shots + away_shots
    total_target = home_target + away_target
    total_corners = home_corners + away_corners

    minute = match["fixture"]["status"].get("elapsed") or 0

    home = match["teams"]["home"]["name"]
    away = match["teams"]["away"]["name"]

    score_home = match["goals"]["home"] or 0
    score_away = match["goals"]["away"] or 0

    signals = 0

    if total_shots >= 14:
        signals += 1

    if total_target >= 5:
        signals += 1

    if total_corners >= 4:
        signals += 1

    if total_shots >= 18 and total_target >= 7:
        signals += 1

    if minute >= 25 and total_target >= 4:
        signals += 1

    if signals >= 4:
        decision = "🟢 СИГНАЛ"
    else:
        decision = "⚪ ПРОПУСК"

    return (
        f"⚽ {home} — {away}\n\n"
        f"⏱ Минута: {minute}′\n"
        f"📊 Счёт: {score_home}:{score_away}\n\n"
        f"Удары: {total_shots:.0f}\n"
        f"В створ: {total_target:.0f}\n"
        f"Угловые: {total_corners:.0f}\n"
        f"Владение: {home_possession:.0f}% — "
        f"{away_possession:.0f}%\n\n"
        f"🔎 Активных сигналов: {signals}/5\n\n"
        f"{decision}\n\n"
        "⚠️ Это статистический сигнал для тестирования, "
        "а не гарантия результата."
    )


def handle_match(chat_id, fixture_id):
    try:
        match = get_match(fixture_id)

        if not match:
            send_message(
                chat_id,
                "⚪ ПРОПУСК\n\nМатч не найден."
            )
            return

        stats = get_match_stats(fixture_id)

        result = analyze_match(
            match,
            stats
        )

        send_message(
            chat_id,
            result,
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "🔄 Обновить анализ",
                            "callback_data": f"match:{fixture_id}"
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
        )

    except Exception:
        send_message(
            chat_id,
            "⚪ ПРОПУСК\n\n"
            "Не удалось получить LIVE-данные."
        )


def process_update(update):
    if "message" in update:
        message = update["message"]

        chat_id = message["chat"]["id"]
        text = message.get("text", "")

        if text == "/start":
            send_message(
                chat_id,
                "⚽ FOOTBALL LIVE\n\n"
                "LIVE-анализ футбольных матчей.\n\n"
                "Выбери раздел:",
                main_keyboard()
            )

    if "callback_query" in update:
        query = update["callback_query"]

        callback_id = query["id"]
        chat_id = query["message"]["chat"]["id"]
        data = query.get("data", "")

        answer_callback(callback_id)

        if data == "home":
            send_message(
                chat_id,
                "⚽ FOOTBALL LIVE\n\n"
                "Выбери раздел:",
                main_keyboard()
            )

        elif data == "live":
            try:
                matches = get_live_matches()

                if not matches:
                    send_message(
                        chat_id,
                        "🔴 LIVE\n\n"
                        "Сейчас активных матчей не найдено.",
                        main_keyboard()
                    )
                else:
                    send_message(
                        chat_id,
                        f"🔴 LIVE\n\n"
                        f"Найдено матчей: {len(matches)}\n\n"
                        "Выбери матч:",
                        live_keyboard(matches)
                    )

            except Exception:
                send_message(
                    chat_id,
                    "⚠️ Не удалось получить LIVE-матчи.\n\n"
                    "Проверь API-ключ."
                )

        elif data == "today":
            try:
                matches = get_today_matches()

                if not matches:
                    send_message(
                        chat_id,
                        "📅 Сегодня матчей не найдено."
                    )
                else:
                    send_message(
                        chat_id,
                        f"📅 МАТЧИ СЕГОДНЯ\n\n"
                        f"Найдено: {len(matches)}\n\n"
                        "Выбери матч:",
                        today_keyboard(matches)
                    )

            except Exception:
                send_message(
                    chat_id,
                    "⚠️ Не удалось получить матчи."
                )

        elif data.startswith("match:"):
            fixture_id = data.split(":")[1]

            handle_match(
                chat_id,
                fixture_id
            )


def main():
    offset = 0

    while True:
        try:
            result = telegram(
                "getUpdates",
                {
                    "timeout": 25,
                    "offset": offset
                }
            )

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                try:
                    process_update(update)
                except Exception:
                    pass

        except Exception:
            time.sleep(5)


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"Football Live Bot is running"
        )

    def log_message(self, format, *args):
        return


def start_web_server():
    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    server.serve_forever()


if __name__ == "__main__":
    threading.Thread(
        target=start_web_server,
        daemon=True
    ).start()

    main()
