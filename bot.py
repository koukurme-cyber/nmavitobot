import html
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from avito_provider import get_status

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN")

API = f"https://api.telegram.org/bot{TOKEN}"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)


def telegram_api(method, data=None):
    body = urllib.parse.urlencode(data or {}).encode("utf-8")
    request = urllib.request.Request(f"{API}/{method}", data=body)

    with urllib.request.urlopen(request, timeout=70) as response:
        payload = json.loads(response.read().decode("utf-8"))

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


def render_status():
    data = get_status(CONFIG)
    blocks = ["<b>Авито</b>"]

    for seller in data["sellers"]:
        blocks.append(seller_block(seller))

    tz = CONFIG.get("timezone", "Europe/Moscow")
    now = datetime.now(ZoneInfo(tz)).strftime("%d.%m.%Y %H:%M")
    blocks.append(f"Обновлено: {now}")

    return "\n\n".join(blocks)


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
            print("DELETE ERROR:", exc)

    timer = threading.Timer(delay, delete)
    timer.daemon = True
    timer.start()


def send_status(chat_id):
    message = telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": render_status(),
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


def edit_status(chat_id, message_id):
    try:
        telegram_api(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": render_status(),
                "parse_mode": "HTML",
                "reply_markup": keyboard(),
                "disable_web_page_preview": "true",
            },
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            raise


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
    print("Bot started")
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
            print("ERROR:", exc)
            time.sleep(3)


if __name__ == "__main__":
    main()
