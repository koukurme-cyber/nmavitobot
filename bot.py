import html
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from avito_provider import get_status

# BotHost/Docker может буферизовать stdout. Делаем логи видимыми сразу.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = Path(os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data")))
CACHE_PATH = DATA_DIR / "status_cache.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_refresh_lock = threading.Lock()

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN")

API = f"https://api.telegram.org/bot{TOKEN}"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)


def telegram_api(method, data=None):
    body = urllib.parse.urlencode(data or {}).encode("utf-8")
    request = urllib.request.Request(f"{API}/{method}", data=body)

    try:
        with urllib.request.urlopen(request, timeout=70) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API {exc.code}: {raw[:500]}") from exc

    if not payload.get("ok"):
        raise RuntimeError(payload)

    return payload["result"]


def money(value):
    if value is None:
        return "—"
    try:
        return f"{int(round(float(value))):,}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return str(value)


def seller_block(seller):
    rows = [f"<b>{html.escape(seller['name'])}</b>"]

    if seller.get("error"):
        rows.append("Ошибка получения данных: " + html.escape(seller["error"]))
        return "\n".join(rows)

    rows.append(f"Кошелёк: <b>{html.escape(money(seller.get('wallet')))}</b>")

    if seller.get("advance") is not None:
        rows.append(f"Аванс: <b>{html.escape(money(seller.get('advance')))}</b>")

    labels = [
        ("published", "Опубликовано"),
        ("rejected", "Отклонено"),
        ("blocked", "Заблокировано"),
        ("removed", "Снято"),
        ("old", "Завершено"),
    ]

    stats = seller.get("ads", {})
    for key, label in labels:
        if key in stats:
            rows.append(f"{label}: <b>{html.escape(str(stats[key]))}</b>")

    payment = seller.get("next_payment")
    if payment:
        date = payment.get("date") or "—"
        amount = money(payment.get("amount"))
        rows.append(
            f"Ближайший платёж: <b>{html.escape(str(date))} — {html.escape(amount)}</b>"
        )

    warnings = seller.get("warnings") or []
    if warnings:
        # Не засоряем сообщение телом ответа API.
        if any("429" in warning for warning in warnings):
            rows.append("<i>Часть данных временно недоступна: лимит Avito</i>")

    return "\n".join(rows)


def render_status(data, fetched_at=None):
    blocks = ["<b>Авито</b>"]

    for seller in data["sellers"]:
        blocks.append(seller_block(seller))

    if fetched_at:
        blocks.append(f"Обновлено: {html.escape(str(fetched_at))}")
    else:
        tz_name = CONFIG.get("timezone", "Europe/Moscow")
        now = datetime.now(ZoneInfo(tz_name)).strftime("%d.%m.%Y %H:%M")
        blocks.append(f"Обновлено: {now}")

    return "\n\n".join(blocks)


def load_cached_status():
    try:
        if not CACHE_PATH.exists():
            return None
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            cached = json.load(f)
        if not isinstance(cached, dict) or "data" not in cached:
            return None
        return cached
    except Exception as exc:
        print("CACHE READ ERROR:", exc, flush=True)
        return None


def save_cached_status(data, fetched_at):
    tmp_path = CACHE_PATH.with_suffix(".tmp")
    payload = {
        "data": data,
        "fetched_at": fetched_at,
    }
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp_path.replace(CACHE_PATH)
    except Exception as exc:
        print("CACHE WRITE ERROR:", exc, flush=True)


def refresh_status_message(chat_id, message_id):
    # Не запускаем второй тяжёлый обход, пока первый ещё идёт.
    if not _refresh_lock.acquire(blocking=False):
        return

    try:
        data = get_status(CONFIG)
        tz_name = CONFIG.get("timezone", "Europe/Moscow")
        fetched_at = datetime.now(ZoneInfo(tz_name)).strftime("%d.%m.%Y %H:%M")
        save_cached_status(data, fetched_at)

        try:
            telegram_api(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": render_status(data, fetched_at),
                    "parse_mode": "HTML",
                    "reply_markup": keyboard(),
                    "disable_web_page_preview": "true",
                },
            )
        except Exception as exc:
            # Сообщение могло быть удалено пользователем или уже истечь.
            if "message is not modified" not in str(exc).lower():
                print("BACKGROUND EDIT ERROR:", exc, flush=True)
    except Exception as exc:
        print("BACKGROUND REFRESH ERROR:", exc, flush=True)
    finally:
        _refresh_lock.release()

def keyboard():
    return json.dumps(
        {
            "inline_keyboard": [
                [{"text": "Обновить", "callback_data": "refresh"}]
            ]
        },
        ensure_ascii=False,
    )


def delete_message_later(chat_id, message_id, delay=600):
    def delete():
        try:
            telegram_api(
                "deleteMessage",
                {"chat_id": chat_id, "message_id": message_id},
            )
        except Exception as exc:
            print("DELETE ERROR:", exc, flush=True)

    timer = threading.Timer(delay, delete)
    timer.daemon = True
    timer.start()


def start_background_refresh(chat_id, message_id):
    thread = threading.Thread(
        target=refresh_status_message,
        args=(chat_id, message_id),
        daemon=True,
    )
    thread.start()


def send_status(chat_id):
    cached = load_cached_status()

    if cached:
        text = render_status(
            cached["data"],
            cached.get("fetched_at"),
        )
    else:
        text = "<b>Авито</b>\n\nПолучаю актуальные данные…"

    message = telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard(),
            "disable_web_page_preview": "true",
        },
    )

    delete_message_later(
        chat_id,
        message["message_id"],
        delay=600,
    )

    # Сразу возвращаем управление Telegram, а Avito обновляем отдельно.
    start_background_refresh(
        chat_id,
        message["message_id"],
    )


def edit_status(chat_id, message_id):
    cached = load_cached_status()

    if cached:
        try:
            telegram_api(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": render_status(
                        cached["data"],
                        cached.get("fetched_at"),
                    ),
                    "parse_mode": "HTML",
                    "reply_markup": keyboard(),
                    "disable_web_page_preview": "true",
                },
            )
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    start_background_refresh(
        chat_id,
        message_id,
    )

def handle_update(update):
    message = update.get("message")
    if message:
        text = (message.get("text") or "").strip()
        chat_id = message["chat"]["id"]

        if text.startswith("/start") or text.startswith("/status"):
            send_status(chat_id)

        return

    callback = update.get("callback_query")
    if callback:
        telegram_api(
            "answerCallbackQuery",
            {"callback_query_id": callback["id"]},
        )

        if callback.get("data") == "refresh" and callback.get("message"):
            message = callback["message"]
            edit_status(
                message["chat"]["id"],
                message["message_id"],
            )


def main():
    print("Bot started", flush=True)

    # Этот бот работает через long polling.
    # Если ранее для токена был установлен webhook, убираем его.
    try:
        telegram_api("deleteWebhook", {"drop_pending_updates": "false"})
    except Exception as exc:
        print("Webhook cleanup warning:", exc, flush=True)

    try:
        telegram_api(
            "setMyCommands",
            {
                "commands": json.dumps(
                    [
                        {"command": "start", "description": "Показать статус Avito"},
                        {"command": "status", "description": "Показать статус Avito"},
                    ],
                    ensure_ascii=False,
                )
            },
        )
    except Exception as exc:
        print("Command setup warning:", exc, flush=True)

    offset = None

    while True:
        try:
            params = {
                "timeout": 50,
                "allowed_updates": json.dumps(
                    ["message", "callback_query"]
                ),
            }

            if offset is not None:
                params["offset"] = offset

            updates = telegram_api("getUpdates", params)

            for update in updates:
                offset = update["update_id"] + 1
                handle_update(update)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("ERROR:", exc, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
