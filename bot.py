import os
import json
import time
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
PITCH_API_KEY = os.getenv("PITCH_API_KEY") or os.getenv("FOOTBALL_API_KEY")
PITCH_API = "https://api.pitchapi.dev/v1"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/"

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN / BOT_TOKEN not found")

if not PITCH_API_KEY:
    raise RuntimeError("PITCH_API_KEY / FOOTBALL_API_KEY not found")

CHECK_EVERY = 120
MIN_MINUTE = 5
MAX_MINUTE = 85
MIN_XG = 0.55
MIN_SHOTS = 6
MIN_TARGET = 2
MIN_PROB = 0.60
MIN_ADV = 0.15

subscribers = set()
sent_signals = set()
state = {"scans": 0, "signals": 0}


def get_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def pitch(path, params=None):
    url = PITCH_API + path

    if params:
        url += "?" + urllib.parse.urlencode(params)

    try:
        obj = get_json(
            url,
            {"X-API-KEY": PITCH_API_KEY}
        )
        return obj.get("data", obj)

    except Exception as e:
        print("PITCH ERROR", path, e)
        return None


def tg(method, data=None):
    url = TELEGRAM_API + method

    if data is None:
        req = urllib.request.Request(url)
    else:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=body)

    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read().decode("utf-8"))


def send(chat_id, text, keyboard=None):
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
        tg("sendMessage", data)
    except Exception as e:
        print("TELEGRAM ERROR", e)


def callback_answer(cid):
    try:
        tg(
            "answerCallbackQuery",
            {"callback_query_id": cid}
        )
    except Exception:
        pass


def num(x):
    try:
        if x is None:
            return 0.0

        if isinstance(x, (int, float)):
            return float(x)

        s = str(x).strip()

        if "(" in s:
            s = s.split("(")[0].strip()

        return float(s)

    except Exception:
        return 0.0


def name(team):
    if isinstance(team, dict):
        return str(
            team.get("name")
            or team.get("short_name")
            or "Team"
        )

    return str(team or "Team")


def teams(m):
    return (
        name(m.get("home_team")),
        name(m.get("away_team"))
    )


def mid(m):
    return m.get("id") or m.get("match_id")


def score(m):
    home = m.get(
        "score_home",
        m.get("home_score", 0)
    )

    away = m.get(
        "score_away",
        m.get("away_score", 0)
    )

    return int(num(home)), int(num(away))


def minute(m):
    if m.get("minute") is not None:
        return int(num(m["minute"]))

    raw = (
        m.get("time_utc")
        or m.get("kickoff")
        or m.get("start_time")
    )

    if not raw:
        return 0

    try:
        start = datetime.fromisoformat(
            str(raw).replace("Z", "+00:00")
        )

        seconds = (
            datetime.now(timezone.utc) - start
        ).total_seconds()

        return max(0, int(seconds / 60))

    except Exception:
        return 0


def today():
    date = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    data = pitch(
        "/date/" + date,
        {"status": "all"}
    )

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        return data.get("matches", [])

    return []


def live():
    result = []

    for match in today():
        status = str(
            match.get("status", "")
        ).lower()

        if status in (
            "finished",
            "not_started",
            "scheduled",
            "cancelled",
            "postponed",
            "fixture"
        ):
            continue

        mm = minute(match)

        if 0 <= mm <= 130:
            match["_minute"] = mm
            result.append(match)

    return result


def parse_stats(data):
    result = {
        "hxg": 0.0,
        "axg": 0.0,
        "hs": 0,
        "as": 0,
        "ht": 0,
        "at": 0
    }

    if not isinstance(data, dict):
        return result

    for period in data.get("periods", []):
        period_name = str(
            period.get("period", "")
        ).lower()

        if period_name not in (
            "all",
            "match",
            "full"
        ):
            continue

        for group in period.get("groups", []):
            for item in group.get("items", []):
                key = str(
                    item.get("key", "")
                ).lower()

                home = num(
                    item.get("home")
                )

                away = num(
                    item.get("away")
                )

                if key in (
                    "expected_goals",
                    "xg"
                ):
                    result["hxg"] = home
                    result["axg"] = away

                elif key in (
                    "total_shots",
                    "shots"
                ):
                    result["hs"] = int(home)
                    result["as"] = int(away)

                elif key in (
                    "shots_on_target",
                    "shots_on_goal",
                    "on_target"
                ):
                    result["ht"] = int(home)
                    result["at"] = int(away)

    return result


def parse_shots(data, home_id, away_id):
    result = {
        "hxg": 0.0,
        "axg": 0.0,
        "hxgot": 0.0,
        "axgot": 0.0,
        "hs": 0,
        "as": 0,
        "ht": 0,
        "at": 0
    }

    if not isinstance(data, dict):
        return result

    periods = data.get(
        "periods",
        []
    )

    if not periods:
        shots = data.get(
            "shots",
            []
        )

        if isinstance(shots, list):
            periods = [
                {"shots": shots}
            ]

    for period in periods:
        for shot in period.get(
            "shots",
            []
        ):
            team_id = (
                shot.get("team_id")
                or shot.get("teamId")
            )

            xg = num(
                shot.get(
                    "expected_goals",
                    shot.get("xg")
                )
            )

            xgot = num(
                shot.get(
                    "expected_goals_on_target",
                    shot.get("xgot")
                )
            )

            target = shot.get(
                "is_on_target",
                shot.get("on_target", False)
            )

            if team_id == home_id:
                result["hs"] += 1
                result["hxg"] += xg
                result["hxgot"] += xgot

                if bool(target):
                    result["ht"] += 1

            elif team_id == away_id:
                result["as"] += 1
                result["axg"] += xg
                result["axgot"] += xgot

                if bool(target):
                    result["at"] += 1

    return result


def parse_red(data, home_id, away_id):
    home = 0
    away = 0

    if not isinstance(data, dict):
        return home, away

    for event in data.get(
        "events",
        []
    ):
        event_type = str(
            event.get(
                "event_type",
                event.get("type", "")
            )
        ).lower()

        team_id = (
            event.get("team_id")
            or event.get("teamId")
        )

        if "red" not in event_type:
            continue

        if team_id == home_id:
            home += 1

        elif team_id == away_id:
            away += 1

    return home, away


def get_momentum(data, match_minute):
    if not isinstance(data, dict):
        return 0.0

    values = []

    for point in data.get(
        "points",
        []
    ):
        point_minute = num(
            point.get("minute")
        )

        value = num(
            point.get("value")
        )

        if (
            match_minute - 10
            <= point_minute
            <= match_minute
        ):
            values.append(value)

    if not values:
        return 0.0

    return sum(values) / len(values)


def probability(
    home_xg,
    away_xg,
    match_minute,
    score_home,
    score_away,
    momentum,
    red_home,
    red_away
):
    elapsed = max(
        5,
        min(match_minute, 90)
    )

    remaining = max(
        1,
        90 - elapsed
    )

    home_lambda = (
        home_xg / elapsed
    ) * remaining

    away_lambda = (
        away_xg / elapsed
    ) * remaining

    if score_home < score_away:
        home_lambda *= 1.15
        away_lambda *= 0.90

    elif score_away < score_home:
        home_lambda *= 0.90
        away_lambda *= 1.15

    if momentum > 0:
        home_lambda *= (
            1 + min(
                momentum / 100,
                0.25
            )
        )

        away_lambda *= (
            1 - min(
                momentum / 200,
                0.15
            )
        )

    elif momentum < 0:
        away_lambda *= (
            1 + min(
                abs(momentum) / 100,
                0.25
            )
        )

        home_lambda *= (
            1 - min(
                abs(momentum) / 200,
                0.15
            )
        )

    home_lambda *= 0.75 ** red_home
    away_lambda *= 0.75 ** red_away

    total = (
        home_lambda
        + away_lambda
    )

    if total <= 0:
        return 0.5, 0.5

    return (
        home_lambda / total,
        away_lambda / total
    )


def analyze(match_id):
    match = pitch(
        "/matches/" + str(match_id)
    )

    if not isinstance(match, dict):
        return False, (
            "⚪ <b>ПРОПУСК</b>\n\n"
            "Нет данных матча."
        )

    home_team = match.get(
        "home_team",
        {}
    )

    away_team = match.get(
        "away_team",
        {}
    )

    home_id = home_team.get("id")
    away_id = away_team.get("id")

    home = name(home_team)
    away = name(away_team)

    score_home, score_away = score(
        match
    )

    match_minute = minute(
        match
    )

    if match_minute < MIN_MINUTE:
        return False, (
            "⚪ <b>ПРОПУСК</b>\n\n"
            f"⚽ {home} — {away}\n"
            f"⏱ {match_minute}′\n"
            "Меньше 5 минут."
        )

    if match_minute > MAX_MINUTE:
        return False, (
            "⚪ <b>ПРОПУСК</b>\n\n"
            f"⚽ {home} — {away}\n"
            f"⏱ {match_minute}′\n"
            "Поздняя стадия."
        )

    stats = parse_stats(
        pitch(
            "/matches/"
            + str(match_id)
            + "/stats"
        )
    )

    shots = parse_shots(
        pitch(
            "/matches/"
            + str(match_id)
            + "/shots"
        ),
        home_id,
        away_id
    )

    red_home, red_away = parse_red(
        pitch(
            "/matches/"
            + str(match_id)
            + "/events"
        ),
        home_id,
        away_id
    )

    mom = get_momentum(
        pitch(
            "/matches/"
            + str(match_id)
            + "/momentum"
        ),
        match_minute
    )

    home_xg = max(
        stats["hxg"],
        shots["hxg"]
    )

    away_xg = max(
        stats["axg"],
        shots["axg"]
    )

    home_shots = max(
        stats["hs"],
        shots["hs"]
    )

    away_shots = max(
        stats["as"],
        shots["as"]
    )

    home_target = max(
        stats["ht"],
        shots["ht"]
    )

    away_target = max(
        stats["at"],
        shots["at"]
    )

    home_xgot = shots["hxgot"]
    away_xgot = shots["axgot"]

    total_xg = (
        home_xg
        + away_xg
    )

    total_shots = (
        home_shots
        + away_shots
    )

    total_target = (
        home_target
        + away_target
    )

    total_xgot = (
        home_xgot
        + away_xgot
    )

    home_probability, away_probability = probability(
        home_xg,
        away_xg,
        match_minute,
        score_home,
        score_away,
        mom,
        red_home,
        red_away
    )

    if home_probability >= away_probability:
        next_team = home
        prob = home_probability
        opponent_prob = away_probability
    else:
        next_team = away
        prob = away_probability
        opponent_prob = home_probability

    advantage = (
        prob
        - opponent_prob
    )

    filters = 0

    if total_xg >= MIN_XG:
        filters += 1

    if total_shots >= MIN_SHOTS:
        filters += 1

    if total_target >= MIN_TARGET:
        filters += 1

    if total_xgot >= 0.35:
        filters += 1

    if prob >= MIN_PROB:
        filters += 1

    if advantage >= MIN_ADV:
        filters += 1

    signal = (
        filters >= 4
        and prob >= MIN_PROB
        and advantage >= MIN_ADV
    )

    if signal:
        state["signals"] += 1

    label = (
        "🟢 <b>СИГНАЛ</b>"
        if signal
        else "⚪ <b>ПРОПУСК</b>"
    )

    text = (
        f"{label}\n\n"
        f"⚽ <b>{home} — {away}</b>\n"
        f"⏱ {match_minute}′\n"
        f"📊 Счёт: "
        f"{score_home}:{score_away}\n\n"
        f"🎯 Следующий гол: "
        f"<b>{next_team}</b>\n"
        f"📈 Вероятность: "
        f"<b>{prob * 100:.1f}%</b>\n"
        f"📊 Преимущество: "
        f"<b>{advantage * 100:.1f}%</b>\n\n"
        f"xG: {home_xg:.2f} — "
        f"{away_xg:.2f}\n"
        f"xGOT: {home_xgot:.2f} — "
        f"{away_xgot:.2f}\n"
        f"Удары: {home_shots} — "
        f"{away_shots}\n"
        f"В створ: {home_target} — "
        f"{away_target}\n"
        f"Momentum: {mom:.2f}\n"
        f"Красные: "
        f"{red_home} — {red_away}\n"
        f"Фильтры: {filters}/6"
    )

    return signal, text


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


def live_keyboard():
    matches = live()
    rows = []

    for match in matches[:20]:
        match_id_value = mid(match)

        if match_id_value is None:
            continue

        home, away = teams(match)
        score_home, score_away = score(match)

        rows.append([
            {
                "text": (
                    f"⚽ {home} "
                    f"{score_home}:{score_away} "
                    f"{away} "
                    f"({match.get('_minute', 0)}′)"
                ),
                "callback_data":
                    f"match:{match_id_value}"
            }
        ])

    rows.append([
        {
            "text": "🔄 ОБНОВИТЬ",
            "callback_data": "live"
        }
    ])

    rows.append([
        {
            "text": "🏠 МЕНЮ",
            "callback_data": "home"
        }
    ])

    return (
        f"🔴 <b>LIVE: {len(matches)}</b>\n\n"
        "Выбери матч:",
        {
            "inline_keyboard": rows
        }
    )


def today_text():
    matches = today()

    lines = [
        f"📅 <b>МАТЧИ СЕГОДНЯ: "
        f"{len(matches)}</b>",
        ""
    ]

    for match in matches[:30]:
        home, away = teams(match)
        score_home, score_away = score(match)

        lines.append(
            f"⚽ {home} — {away} "
            f"{score_home}:{score_away}"
        )

    return "\n".join(lines)


def process_update(update):
    message = update.get("message")

    if message:
        chat_id = message.get(
            "chat",
            {}
        ).get("id")

        text = str(
            message.get(
                "text",
                ""
            )
        ).strip()

        if chat_id is not None:
            subscribers.add(chat_id)

        if (
            chat_id is not None
            and text.startswith("/start")
        ):
            send(
                chat_id,
                (
                    "⚽ <b>FOOTBALL LIVE</b>\n\n"
                    "Выбери действие:"
                ),
                main_keyboard()
            )

        elif (
            chat_id is not None
            and text.startswith("/status")
        ):
            send(
                chat_id,
                (
                    "📊 <b>СТАТУС</b>\n\n"
                    f"Сканирований: "
                    f"{state['scans']}\n"
                    f"Сигналов: "
                    f"{state['signals']}\n"
                    f"Подписчиков: "
                    f"{len(subscribers)}"
                ),
                main_keyboard()
            )

        return

    callback = update.get(
        "callback_query"
    )

    if not callback:
        return

    chat_id = callback.get(
        "message",
        {}
    ).get(
        "chat",
        {}
    ).get("id")

    data = callback.get(
        "data",
        ""
    )

    if chat_id is not None:
        subscribers.add(chat_id)

    callback_answer(
        callback.get("id")
    )

    if data == "home":
        send(
            chat_id,
            (
                "⚽ <b>FOOTBALL LIVE</b>\n\n"
                "Выбери действие:"
            ),
            main_keyboard()
        )

    elif data == "live":
        text, keyboard = live_keyboard()

        send(
            chat_id,
            text,
            keyboard
        )

    elif data == "today":
        send(
            chat_id,
            today_text(),
            main_keyboard()
        )

    elif data == "status":
        send(
            chat_id,
            (
                "📊 <b>СТАТУС</b>\n\n"
                f"Сканирований: "
                f"{state['scans']}\n"
                f"Сигналов: "
                f"{state['signals']}\n"
                f"Подписчиков: "
                f"{len(subscribers)}"
            ),
            main_keyboard()
        )

    elif data.startswith("match:"):
        match_id_value = data.split(
            ":",
            1
        )[1]

        _, text = analyze(
            match_id_value
        )

        send(
            chat_id,
            text,
            match_keyboard(
                match_id_value
            )
        )


def telegram_loop():
    offset = 0

    print(
        "TELEGRAM LOOP STARTED"
    )

    while True:
        try:
            result = tg(
                "getUpdates",
                {
                    "timeout": 50,
                    "offset": offset
                }
            )

            for update in result.get(
                "result",
                []
            ):
                offset = (
                    update.get(
                        "update_id",
                        offset
                    )
                    + 1
                )

                try:
                    process_update(
                        update
                    )
                except Exception as e:
                    print(
                        "UPDATE ERROR",
                        e
                    )

        except Exception as e:
            print(
                "TELEGRAM LOOP ERROR",
                e
            )

            time.sleep(5)


def scanner():
    print(
        "SCANNER STARTED"
    )

    while True:
        try:
            matches = live()

            state["scans"] += 1

            print(
                "LIVE MATCHES:",
                len(matches)
            )

            for match in matches:
                match_id_value = mid(match)
                match_minute = match.get(
                    "_minute",
                    0
                )

                if match_id_value is None:
                    continue

                if match_minute < MIN_MINUTE:
                    continue

                if match_minute > MAX_MINUTE:
                    continue

                signal, text = analyze(
                    match_id_value
                )

                if not signal:
                    continue

                key = (
                    f"{match_id_value}:"
                    f"{match_minute // 5}"
                )

                if key in sent_signals:
                    continue

                sent_signals.add(key)

                for chat_id in list(
                    subscribers
                ):
                    send(
                        chat_id,
                        text,
                        match_keyboard(
                            match_id_value
                        )
                    )

        except Exception as e:
            print(
                "SCANNER ERROR",
                e
            )

        time.sleep(
            CHECK_EVERY
        )


class Handler(
    BaseHTTPRequestHandler
):
    def do_GET(self):
        body = (
            b"Football Live 3.0 is running."
        )

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; "
            "charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    def log_message(
        self,
        fmt,
        *args
    ):
        pass


def health():
    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    server = HTTPServer(
        (
            "0.0.0.0",
            port
        ),
        Handler
    )

    print(
        "HEALTH SERVER STARTED:",
        port
    )

    server.serve_forever()


if __name__ == "__main__":
    print(
        "FOOTBALL LIVE 3.0 STARTED"
    )

    threading.Thread(
        target=health,
        daemon=True
    ).start()

    threading.Thread(
        target=scanner,
        daemon=True
    ).start()

    telegram_loop()
