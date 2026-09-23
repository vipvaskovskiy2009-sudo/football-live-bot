
import os
import json
import time
import threading
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


BOT_TOKEN = os.environ["BOT_TOKEN"]
PITCH_KEY = os.environ["FOOTBALL_API_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
PITCH_API = "https://api.pitchapi.dev/v1"

TALLINN = ZoneInfo("Europe/Tallinn")


# =========================================================
# HTTP
# =========================================================

def api_get(url, headers=None, timeout=20):
    req = urllib.request.Request(
        url,
        headers=headers or {},
        method="GET"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except Exception as e:
        print("HTTP ERROR:", url, e)
        raise


def pitch_get(path, params=None):
    url = PITCH_API + path

    if params:
        url += "?" + urllib.parse.urlencode(params)

    data = api_get(
        url,
        headers={
            "X-API-KEY": PITCH_KEY,
            "Accept": "application/json",
            "User-Agent": "FootballLiveBot/1.0"
        }
    )

    if "error" in data:
        error = data["error"]
        raise RuntimeError(
            f"PitchAPI error: {error.get('code')} - {error.get('message')}"
        )

    return data.get("data")


def telegram(method, payload=None):
    url = f"{TELEGRAM_API}/{method}"

    body = urllib.parse.urlencode(payload or {}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    return telegram("sendMessage", payload)


def answer_callback(callback_id):
    try:
        telegram(
            "answerCallbackQuery",
            {"callback_query_id": callback_id}
        )
    except Exception as e:
        print("CALLBACK ERROR:", e)


# =========================================================
# TELEGRAM MENU
# =========================================================

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🔴 LIVE"}],
        [{"text": "📅 МАТЧИ СЕГОДНЯ"}]
    ],
    "resize_keyboard": True
}


def start_text():
    return (
        "⚽ <b>FOOTBALL LIVE</b>\n\n"
        "LIVE-анализ футбольных матчей.\n\n"
        "Выбери раздел:"
    )


# =========================================================
# DATE / MATCHES
# =========================================================

def parse_time_utc(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except Exception:
        return None


def get_today_matches():
    now = datetime.now(TALLINN)
    date_str = now.strftime("%Y-%m-%d")

    data = pitch_get(
        f"/date/{date_str}",
        {"status": "all"}
    )

    return data.get("matches", []) if data else []


def is_live_match(match):
    status = str(match.get("status") or "").lower()

    finished_statuses = {
        "finished",
        "ft",
        "after_extra_time",
        "after_penalties",
        "cancelled",
        "postponed",
        "abandoned"
    }

    not_started_statuses = {
        "not_started",
        "scheduled",
        "upcoming"
    }

    if status in finished_statuses:
        return False

    if status in not_started_statuses:
        start = parse_time_utc(match.get("time_utc"))

        if not start:
            return False

        now_utc = datetime.now(timezone.utc)

        # Если начало уже прошло, считаем матч потенциально LIVE.
        # Это позволяет работать и с задержками обновления статуса.
        return start <= now_utc

    # Для неизвестного статуса дополнительно смотрим время.
    start = parse_time_utc(match.get("time_utc"))

    if start:
        return start <= datetime.now(timezone.utc)

    return False


def get_live_matches():
    matches = get_today_matches()

    live = [
        match for match in matches
        if is_live_match(match)
    ]

    return live


# =========================================================
# MATCH DATA
# =========================================================

def get_match(match_id):
    return pitch_get(f"/matches/{match_id}")


def get_match_shots(match_id):
    data = pitch_get(f"/matches/{match_id}/shots")

    if not data:
        return []

    shots = []

    for period in data.get("periods", []):
        shots.extend(period.get("shots", []))

    return shots


def get_match_stats(match_id):
    return pitch_get(f"/matches/{match_id}/stats")


def get_match_events(match_id):
    data = pitch_get(f"/matches/{match_id}/events")

    if not data:
        return []

    return data.get("events", [])


def get_match_momentum(match_id):
    data = pitch_get(f"/matches/{match_id}/momentum")

    if not data:
        return []

    return data.get("points", [])


# =========================================================
# STATISTICS
# =========================================================

def calculate_shot_stats(shots, home_id, away_id):
    result = {
        "home_shots": 0,
        "away_shots": 0,
        "home_on_target": 0,
        "away_on_target": 0,
        "home_xg": 0.0,
        "away_xg": 0.0,
        "home_xgot": 0.0,
        "away_xgot": 0.0,
        "home_box_shots": 0,
        "away_box_shots": 0
    }

    for shot in shots:
        team_id = shot.get("team_id")

        xg = shot.get("expected_goals")
        xgot = shot.get("expected_goals_on_target")

        if not isinstance(xg, (int, float)):
            xg = 0.0

        if not isinstance(xgot, (int, float)):
            xgot = 0.0

        on_target = bool(shot.get("is_on_target"))
        inside_box = bool(shot.get("is_inside_box"))

        if team_id == home_id:
            result["home_shots"] += 1
            result["home_xg"] += xg
            result["home_xgot"] += xgot

            if on_target:
                result["home_on_target"] += 1

            if inside_box:
                result["home_box_shots"] += 1

        elif team_id == away_id:
            result["away_shots"] += 1
            result["away_xg"] += xg
            result["away_xgot"] += xgot

            if on_target:
                result["away_on_target"] += 1

            if inside_box:
                result["away_box_shots"] += 1

    return result


def extract_stats(stats_data):
    result = {}

    if not stats_data:
        return result

    periods = stats_data.get("periods", [])

    # Используем только полный матч.
    period = None

    for p in periods:
        if str(p.get("period", "")).lower() == "all":
            period = p
            break

    if period is None and periods:
        period = periods[-1]

    if not period:
        return result

    for group in period.get("groups", []):
        for item in group.get("items", []):
            key = item.get("key")

            if not key:
                continue

            home = item.get("home")
            away = item.get("away")

            result[key] = {
                "home": home,
                "away": away,
                "title": item.get("title")
            }

    return result


def to_number(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    if not text:
        return None

    # "63%" -> 63
    if "%" in text:
        text = text.replace("%", "").strip()

    # "4 / 10" -> 4
    if " " in text:
        text = text.split(" ")[0]

    try:
        return float(text)
    except Exception:
        return None


def stat_value(stats, key, side):
    item = stats.get(key)

    if not item:
        return None

    return to_number(item.get(side))


# =========================================================
# ANALYSIS
# =========================================================

def analyze_match(match_id):
    match = get_match(match_id)

    home = match.get("home_team", {})
    away = match.get("away_team", {})

    home_id = home.get("id")
    away_id = away.get("id")

    home_name = home.get("name", "HOME")
    away_name = away.get("name", "AWAY")

    score_home = match.get("score_home")
    score_away = match.get("score_away")

    if score_home is None:
        score_home = 0

    if score_away is None:
        score_away = 0

    shots = get_match_shots(match_id)
    stats_data = get_match_stats(match_id)
    events = get_match_events(match_id)
    momentum = get_match_momentum(match_id)

    shot_stats = calculate_shot_stats(
        shots,
        home_id,
        away_id
    )

    stats = extract_stats(stats_data)

    # -----------------------------------------------------
    # Basic statistics
    # -----------------------------------------------------

    possession_home = stat_value(
        stats, "possession", "home"
    )

    possession_away = stat_value(
        stats, "possession", "away"
    )

    dangerous_home = stat_value(
        stats, "dangerous_attacks", "home"
    )

    dangerous_away = stat_value(
        stats, "dangerous_attacks", "away"
    )

    attacks_home = stat_value(
        stats, "attacks", "home"
    )

    attacks_away = stat_value(
        stats, "attacks", "away"
    )

    corners_home = stat_value(
        stats, "corners", "home"
    )

    corners_away = stat_value(
        stats, "corners", "away"
    )

    # Some feeds use slightly different names.
    if possession_home is None:
        possession_home = stat_value(
            stats, "ball_possession", "home"
        )

    if possession_away is None:
        possession_away = stat_value(
            stats, "ball_possession", "away"
        )

    # -----------------------------------------------------
    # Momentum
    # -----------------------------------------------------

    momentum_value = None

    if momentum:
        last = momentum[-1]
        momentum_value = last.get("value")

    # -----------------------------------------------------
    # Model
    # -----------------------------------------------------

    total_xg = (
        shot_stats["home_xg"] +
        shot_stats["away_xg"]
    )

    total_xgot = (
        shot_stats["home_xgot"] +
        shot_stats["away_xgot"]
    )

    total_on_target = (
        shot_stats["home_on_target"] +
        shot_stats["away_on_target"]
    )

    total_shots = (
        shot_stats["home_shots"] +
        shot_stats["away_shots"]
    )

    score_total = score_home + score_away

    remaining_xg = max(0.0, total_xg - score_total)

    signal = "ПРОПУСК"
    reason = "Недостаточно сильного LIVE-сигнала."

    # Технический фильтр.
    # Это не прогноз гарантированного результата.
    strong_attack = (
        total_xg >= 2.2 and
        total_on_target >= 5 and
        total_shots >= 12
    )

    strong_quality = total_xgot >= 1.8

    balanced_flow = True

    if momentum_value is not None:
        balanced_flow = abs(float(momentum_value)) <= 75

    if strong_attack and strong_quality and balanced_flow:
        signal = "СИГНАЛ"
        reason = (
            "LIVE-поток имеет достаточный объём моментов "
            "и качество ударов."
        )

    # -----------------------------------------------------
    # Text
    # -----------------------------------------------------

    text = (
        f"⚽ <b>{home_name} — {away_name}</b>\n\n"
        f"⏱ Статус: <b>{match.get('status', 'LIVE')}</b>\n"
        f"🔢 Счёт: <b>{int(score_home)}:{int(score_away)}</b>\n\n"

        f"🟢 <b>КАЧЕСТВО МОМЕНТОВ</b>\n"
        f"xG: {shot_stats['home_xg']:.2f} — "
        f"{shot_stats['away_xg']:.2f}\n"
        f"xGOT: {shot_stats['home_xgot']:.2f} — "
        f"{shot_stats['away_xgot']:.2f}\n"
        f"Удары в створ: "
        f"{shot_stats['home_on_target']} — "
        f"{shot_stats['away_on_target']}\n"
        f"Удары из штрафной: "
        f"{shot_stats['home_box_shots']} — "
        f"{shot_stats['away_box_shots']}\n\n"

        f"🟢 <b>ТЕМП</b>\n"
        f"Удары: "
        f"{shot_stats['home_shots']} — "
        f"{shot_stats['away_shots']}\n"
    )

    if dangerous_home is not None and dangerous_away is not None:
        text += (
            f"Опасные атаки: "
            f"{dangerous_home:.0f} — "
            f"{dangerous_away:.0f}\n"
        )

    if attacks_home is not None and attacks_away is not None:
        text += (
            f"Атаки: "
            f"{attacks_home:.0f} — "
            f"{attacks_away:.0f}\n"
        )

    if possession_home is not None and possession_away is not None:
        text += (
            f"Владение: "
            f"{possession_home:.0f}% — "
            f"{possession_away:.0f}%\n"
        )

    if corners_home is not None and corners_away is not None:
        text += (
            f"Угловые: "
            f"{corners_home:.0f} — "
            f"{corners_away:.0f}\n"
        )

    text += (
        f"\n📊 Всего ударов: <b>{total_shots}</b>\n"
        f"🎯 Удары в створ: <b>{total_on_target}</b>\n"
        f"📈 Сумма xG: <b>{total_xg:.2f}</b>\n"
        f"📈 Сумма xGOT: <b>{total_xgot:.2f}</b>\n"
    )

    if momentum_value is not None:
        text += (
            f"🌊 Momentum: <b>{float(momentum_value):.1f}</b>\n"
        )

    text += (
        f"\n<b>{signal}</b>\n"
        f"{reason}\n\n"
        f"⚠️ Модельный сигнал, не гарантия результата."
    )

    return text


# =========================================================
# MATCH BUTTONS
# =========================================================

def live_keyboard(matches):
    rows = []

    for match in matches[:30]:
        match_id = match.get("id")

        home = match.get("home_team", {}).get(
            "name", "HOME"
        )

        away = match.get("away_team", {}).get(
            "name", "AWAY"
        )

        score_home = match.get("score_home")
        score_away = match.get("score_away")

        if score_home is None:
            score_home = 0

        if score_away is None:
            score_away = 0

        label = (
            f"⚽ {home} "
            f"{score_home}:{score_away} "
            f"{away}"
        )

        rows.append([
            {
                "text": label[:60],
                "callback_data": f"match:{match_id}"
            }
        ])

    return {
        "inline_keyboard": rows
    }


def today_text(matches):
    if not matches:
        return "📅 <b>МАТЧИ СЕГОДНЯ</b>\n\nМатчей не найдено."

    text = "📅 <b>МАТЧИ СЕГОДНЯ</b>\n\n"

    for match in matches[:30]:
        home = match.get("home_team", {}).get(
            "name", "HOME"
        )

        away = match.get("away_team", {}).get(
            "name", "AWAY"
        )

        status = match.get("status", "")

        text += (
            f"⚽ {home} — {away}\n"
            f"Статус: {status}\n\n"
        )

    return text


# =========================================================
# UPDATE HANDLING
# =========================================================

def process_update(update):
    try:
        # Normal message
        message = update.get("message")

        if message:
            chat_id = message["chat"]["id"]
            text = message.get("text", "")

            if text == "/start":
                send_message(
                    chat_id,
                    start_text(),
                    MAIN_KEYBOARD
                )
                return

            if text == "🔴 LIVE":
                try:
                    matches = get_live_matches()

                    if not matches:
                        send_message(
                            chat_id,
                            "🔴 <b>LIVE</b>\n\n"
                            "Сейчас активных матчей не найдено.",
                            MAIN_KEYBOARD
                        )
                    else:
                        send_message(
                            chat_id,
                            f"🔴 <b>LIVE</b>\n\n"
                            f"Найдено матчей: "
                            f"<b>{len(matches)}</b>\n\n"
                            f"Выбери матч:",
                            live_keyboard(matches)
                        )

                except Exception as e:
                    print("LIVE ERROR:", e)

                    send_message(
                        chat_id,
                        "⚠️ Не удалось получить LIVE-матчи.\n"
                        "Проверь логи Render.",
                        MAIN_KEYBOARD
                    )

                return

            if text == "📅 МАТЧИ СЕГОДНЯ":
                try:
                    matches = get_today_matches()

                    send_message(
                        chat_id,
                        today_text(matches),
                        MAIN_KEYBOARD
                    )

                except Exception as e:
                    print("TODAY ERROR:", e)

                    send_message(
                        chat_id,
                        "⚠️ Не удалось получить матчи.",
                        MAIN_KEYBOARD
                    )

                return

        # Inline button
        callback = update.get("callback_query")

        if callback:
            callback_id = callback.get("id")
            data = callback.get("data", "")
            chat_id = callback["message"]["chat"]["id"]

            answer_callback(callback_id)

            if data.startswith("match:"):
                match_id = data.split(":", 1)[1]

                try:
                    result = analyze_match(match_id)

                    send_message(
                        chat_id,
                        result,
                        MAIN_KEYBOARD
                    )

                except Exception as e:
                    print("MATCH ERROR:", e)

                    send_message(
                        chat_id,
                        "⚠️ Ошибка анализа матча.\n"
                        "Проверь логи Render.",
                        MAIN_KEYBOARD
                    )

    except Exception as e:
        print("UPDATE ERROR:", e)


# =========================================================
# TELEGRAM POLLING
# =========================================================

def bot_loop():
    offset = 0

    print("FOOTBALL LIVE BOT STARTED")

    while True:
        try:
            result = telegram(
                "getUpdates",
                {
                    "timeout": 30,
                    "offset": offset,
                    "allowed_updates": json.dumps(
                        ["message", "callback_query"]
                    )
                }
            )

            updates = result.get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                process_update(update)

        except Exception as e:
            print("POLLING ERROR:", e)
            time.sleep(5)


# =========================================================
# HEALTH SERVER FOR RENDER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()
        self.wfile.write(
            b"Football Live Bot is running."
        )

    def log_message(self, format, *args):
        return


def health_server():
    port = int(os.environ.get("PORT", "10000"))

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    print(f"Health server listening on {port}")

    server.serve_forever()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    threading.Thread(
        target=health_server,
        daemon=True
    ).start()

    bot_loop()
