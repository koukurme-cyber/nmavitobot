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

from avito_provider import get_financial_status, get_items_status, get_status

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
_keyboard_cleared_chats = set()
APP_VERSION = "v27"

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


def plain_number(value):
    if value is None:
        return "—"
    try:
        return f"{int(round(float(value))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def short_date(value):
    if not value:
        return "—"
    return str(value)


def _seller_short_name(seller):
    return seller.get("short_name") or {
        "aggregaty": "Агрегаты",
        "avmex": "Авмекс",
        "nm_orange": "НМ оранж.",
        "nm_blue": "НМ синие",
        "tir": "ТИР",
    }.get(seller.get("key"), seller.get("name", seller.get("key", "—")))


def _pre(lines):
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


def _format_table(headers, rows, aligns=None):
    rows = [[str(cell) for cell in row] for row in rows]
    headers = [str(cell) for cell in headers]
    widths = []
    for i, header in enumerate(headers):
        widths.append(max([len(header)] + [len(row[i]) for row in rows if i < len(row)]))

    def format_row(row):
        cells = []
        for i, width in enumerate(widths):
            cell = row[i] if i < len(row) else ""
            align = aligns[i] if aligns and i < len(aligns) else "left"
            cells.append(cell.rjust(width) if align == "right" else cell.ljust(width))
        return "  ".join(cells).rstrip()

    return [format_row(headers), format_row(["─" * w for w in widths])] + [
        format_row(row) for row in rows
    ]


def _financial_rows(sellers):
    rows = []
    for seller in sellers:
        if seller.get("error"):
            rows.append([_seller_short_name(seller), "ошибка", "—", "—"])
            continue
        advance = plain_number(seller.get("advance")) if seller.get("financial_mode") != "placements" else "—"
        placements = plain_number(seller.get("placements_remaining")) if seller.get("financial_mode") == "placements" else "—"
        rows.append([
            _seller_short_name(seller),
            plain_number(seller.get("wallet")),
            advance,
            placements,
        ])
    return rows


def _ads_rows(sellers):
    rows = []
    for seller in sellers:
        stats = seller.get("ads") or {}
        if seller.get("error"):
            rows.append([_seller_short_name(seller), "ошибка", "—", "—", "—", "—"])
            continue
        rows.append([
            _seller_short_name(seller),
            plain_number(stats.get("published")),
            plain_number(stats.get("rejected")),
            plain_number(stats.get("blocked")),
            plain_number(stats.get("removed")),
            plain_number(stats.get("old")),
        ])
    return rows


def _period_rows(sellers):
    rows = []
    for seller in sellers:
        if seller.get("error"):
            continue
        end_date = seller.get("subscription_end") or seller.get("tariff_end")
        payment = seller.get("subscription_next_payment") or seller.get("next_tariff") or {}
        next_date = payment.get("date") if isinstance(payment, dict) else None
        amount = payment.get("amount") if isinstance(payment, dict) else None
        if end_date or next_date or amount is not None:
            rows.append([
                _seller_short_name(seller),
                short_date(end_date),
                short_date(next_date),
                plain_number(amount),
            ])
    return rows


def _updated_line(fetched_at=None):
    if fetched_at:
        return f"Обновлено: {html.escape(str(fetched_at))}"
    tz_name = CONFIG.get("timezone", "Europe/Moscow")
    now = datetime.now(ZoneInfo(tz_name)).strftime("%d.%m.%Y %H:%M")
    return f"Обновлено: {now}"


def render_status(data, fetched_at=None):
    sellers = data["sellers"]
    blocks = ["<b>Авито · общий статус</b>"]

    blocks.append("<b>Финансы, ₽</b>\n" + _pre(_format_table(
        ["Аккаунт", "Кошел.", "Аванс", "Места"],
        _financial_rows(sellers),
        ["left", "right", "right", "right"],
    )))

    blocks.append("<b>Объявления</b>\n" + _pre(_format_table(
        ["Аккаунт", "Опубл", "Откл", "Блок", "Снято", "Зав."],
        _ads_rows(sellers),
        ["left", "right", "right", "right", "right", "right"],
    )))

    period_rows = _period_rows(sellers)
    if period_rows:
        blocks.append("<b>Тарифы и подписки</b>\n" + _pre(_format_table(
            ["Аккаунт", "До", "След.", "Сумма"],
            period_rows,
            ["left", "left", "left", "right"],
        )))

    errors = [seller for seller in sellers if seller.get("error")]
    if errors:
        blocks.append("\n".join(
            f"{html.escape(_seller_short_name(s))}: {html.escape(str(s['error']))}"
            for s in errors
        ))

    blocks.append(_updated_line(fetched_at))
    return "\n\n".join(blocks)


def render_balances(data, fetched_at=None):
    sellers = data["sellers"]
    lines = _format_table(
        ["Аккаунт", "Кошел.", "Аванс", "Места"],
        _financial_rows(sellers),
        ["left", "right", "right", "right"],
    )
    return "\n\n".join([
        "<b>Авито · балансы, ₽</b>",
        _pre(lines),
        _updated_line(fetched_at),
    ])


def render_ads(data, fetched_at=None):
    sellers = data["sellers"]
    lines = _format_table(
        ["Аккаунт", "Опубл", "Откл", "Блок", "Снято", "Зав."],
        _ads_rows(sellers),
        ["left", "right", "right", "right", "right", "right"],
    )
    return "\n\n".join([
        "<b>Авито · объявления</b>",
        _pre(lines),
        _updated_line(fetched_at),
    ])


def render_account(data, fetched_at=None):
    seller = data["sellers"][0]
    title = html.escape(seller.get("name") or _seller_short_name(seller))
    if seller.get("error"):
        return f"<b>{title}</b>\n\nОшибка получения данных: {html.escape(str(seller['error']))}"

    stats = seller.get("ads") or {}
    rows = [
        ["Кошелёк", money(seller.get("wallet"))],
    ]
    if seller.get("financial_mode") == "placements":
        if seller.get("placements_remaining") is not None:
            rows.append(["Остаток размещений", plain_number(seller.get("placements_remaining"))])
    elif seller.get("advance") is not None:
        rows.append(["Аванс", money(seller.get("advance"))])

    rows.extend([
        ["Опубликовано", plain_number(stats.get("published"))],
        ["Отклонено", plain_number(stats.get("rejected"))],
        ["Заблокировано", plain_number(stats.get("blocked"))],
        ["Снято", plain_number(stats.get("removed"))],
        ["Завершено", plain_number(stats.get("old"))],
    ])

    end_date = seller.get("subscription_end") or seller.get("tariff_end")
    if end_date:
        rows.append(["Подписка до" if seller.get("financial_mode") != "placements" else "Тариф до", str(end_date)])

    payment = seller.get("subscription_next_payment") or seller.get("next_tariff")
    if payment:
        date = payment.get("date")
        amount = payment.get("amount")
        if date and amount is not None:
            value = f"{date} · {money(amount)}"
        elif date:
            value = str(date)
        elif amount is not None:
            value = money(amount)
        else:
            value = "—"
        rows.append(["След. платёж", value])

    width = max(len(row[0]) for row in rows)
    lines = [f"{label.ljust(width)}  {value}" for label, value in rows]
    return "\n\n".join([
        f"<b>{title}</b>",
        _pre(lines),
        _updated_line(fetched_at),
    ])

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
    payload = {"data": data, "fetched_at": fetched_at}
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp_path.replace(CACHE_PATH)
    except Exception as exc:
        print("CACHE WRITE ERROR:", exc, flush=True)


def _edit_result_message(chat_id, message_id, text):
    try:
        telegram_api(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        return True
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return True
        print("BACKGROUND EDIT ERROR:", exc, flush=True)
        return False


def _config_for_seller(seller_key=None):
    if not seller_key:
        return CONFIG
    sellers = [seller for seller in CONFIG["sellers"] if seller["key"] == seller_key]
    if not sellers:
        raise RuntimeError(f"Неизвестный аккаунт: {seller_key}")
    return {
        "timezone": CONFIG.get("timezone", "Europe/Moscow"),
        "sellers": sellers,
    }


def refresh_query_message(chat_id, message_id, mode="full", seller_key=None):
    # Один активный сбор за раз: это защищает от одновременной тяжёлой пагинации
    # по одним и тем же Avito-аккаунтам и лишних 429.
    with _refresh_lock:
        try:
            tz_name = CONFIG.get("timezone", "Europe/Moscow")
            query_config = _config_for_seller(seller_key)

            if mode == "finance":
                data = get_financial_status(query_config)
                renderer = render_balances
            elif mode == "ads":
                data = get_items_status(query_config)
                renderer = render_ads
            else:
                data = get_status(query_config)
                renderer = render_account if seller_key else render_status

            fetched_at = datetime.now(ZoneInfo(tz_name)).strftime("%d.%m.%Y %H:%M")

            # Постоянный кэш обновляем только полным общим /status.
            if mode == "full" and not seller_key:
                save_cached_status(data, fetched_at)

            if _edit_result_message(chat_id, message_id, renderer(data, fetched_at)):
                print(
                    f"Telegram updated: mode={mode}, seller={seller_key or 'all'}",
                    flush=True,
                )

        except Exception as exc:
            print("BACKGROUND REFRESH ERROR:", exc, flush=True)
            try:
                _edit_result_message(
                    chat_id,
                    message_id,
                    "<b>Авито</b>\n\nНе удалось получить актуальные данные.",
                )
            except Exception as edit_exc:
                print("ERROR EDIT ERROR:", edit_exc, flush=True)
        finally:
            delete_message_later(chat_id, message_id, delay=600)

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


def start_background_query(chat_id, message_id, mode="full", seller_key=None):
    thread = threading.Thread(
        target=refresh_query_message,
        args=(chat_id, message_id, mode, seller_key),
        daemon=True,
    )
    thread.start()


def remove_legacy_keyboard(chat_id):
    # ReplyKeyboardRemove нельзя прикреплять к сообщению, которое потом
    # редактируется через editMessageText. Поэтому старую клавиатуру
    # убираем отдельным временным сообщением.
    if chat_id in _keyboard_cleared_chats:
        return

    try:
        temp = telegram_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "Обновляю статус…",
                "reply_markup": json.dumps({"remove_keyboard": True}),
            },
        )
        _keyboard_cleared_chats.add(chat_id)
        delete_message_later(chat_id, temp["message_id"], delay=1)
    except Exception as exc:
        print("KEYBOARD REMOVE ERROR:", exc, flush=True)


ACCOUNT_ALIASES = {
    "aggregaty": "aggregaty",
    "агрегаты": "aggregaty",
    "avmex": "avmex",
    "авмекс": "avmex",
    "orange": "nm_orange",
    "nm_orange": "nm_orange",
    "оранжевые": "nm_orange",
    "blue": "nm_blue",
    "nm_blue": "nm_blue",
    "синие": "nm_blue",
    "tir": "tir",
    "тир": "tir",
}

DIRECT_ACCOUNT_COMMANDS = {
    "/aggregaty": "aggregaty",
    "/avmex": "avmex",
    "/orange": "nm_orange",
    "/blue": "nm_blue",
    "/tir": "tir",
}


def _resolve_account(value):
    if not value:
        return None
    return ACCOUNT_ALIASES.get(value.strip().casefold())


def _accounts_help():
    return (
        "<b>Аккаунты</b>\n\n"
        "<code>aggregaty</code> — Агрегаты\n"
        "<code>avmex</code> — Авмекс\n"
        "<code>orange</code> — НМ (оранжевые)\n"
        "<code>blue</code> — НМ (синие)\n"
        "<code>tir</code> — ТИР\n\n"
        "Пример: <code>/account avmex</code>"
    )


def send_query(chat_id, mode="full", seller_key=None):
    remove_legacy_keyboard(chat_id)
    labels = {
        "full": "Получаю актуальные данные…",
        "finance": "Получаю данные по балансам…",
        "ads": "Считаю статусы объявлений…",
    }
    message = telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": f"<b>Авито</b>\n\n{labels.get(mode, labels['full'])}",
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )
    start_background_query(chat_id, message["message_id"], mode, seller_key)


def handle_update(update):
    message = update.get("message")
    if not message:
        return

    text = (message.get("text") or "").strip()
    chat_id = message["chat"]["id"]
    if not text.startswith("/"):
        return

    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].casefold()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in {"/start", "/status"}:
        send_query(chat_id, "full")
        return

    if command in DIRECT_ACCOUNT_COMMANDS:
        send_query(chat_id, "full", DIRECT_ACCOUNT_COMMANDS[command])
        return

    if command == "/account":
        seller_key = _resolve_account(argument)
        if not seller_key:
            telegram_api(
                "sendMessage",
                {"chat_id": chat_id, "text": _accounts_help(), "parse_mode": "HTML"},
            )
            return
        send_query(chat_id, "full", seller_key)
        return

    if command == "/balance_all":
        send_query(chat_id, "finance")
        return

    if command in {"/balance", "/balances"}:
        seller_key = _resolve_account(argument) if argument else None
        if argument and not seller_key:
            telegram_api(
                "sendMessage",
                {"chat_id": chat_id, "text": _accounts_help(), "parse_mode": "HTML"},
            )
            return
        send_query(chat_id, "finance", seller_key)
        return

    if command in {"/ads", "/items"}:
        seller_key = _resolve_account(argument) if argument else None
        if argument and not seller_key:
            telegram_api(
                "sendMessage",
                {"chat_id": chat_id, "text": _accounts_help(), "parse_mode": "HTML"},
            )
            return
        send_query(chat_id, "ads", seller_key)
        return

    if command == "/accounts":
        telegram_api(
            "sendMessage",
            {"chat_id": chat_id, "text": _accounts_help(), "parse_mode": "HTML"},
        )

def main():
    print(f"Bot started {APP_VERSION}", flush=True)

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
                        {"command": "status", "description": "Общий статус всех аккаунтов"},
                        {"command": "balance_all", "description": "Балансы всех аккаунтов"},
                        {"command": "balance", "description": "Баланс аккаунта: /balance avmex"},
                        {"command": "ads", "description": "Только статусы объявлений"},
                        {"command": "account", "description": "Аккаунт по имени: /account avmex"},
                        {"command": "aggregaty", "description": "Вся информация: Агрегаты"},
                        {"command": "avmex", "description": "Вся информация: Авмекс"},
                        {"command": "orange", "description": "Вся информация: НМ оранжевые"},
                        {"command": "blue", "description": "Вся информация: НМ синие"},
                        {"command": "tir", "description": "Вся информация: ТИР"},
                        {"command": "accounts", "description": "Показать имена аккаунтов"},
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
                "allowed_updates": json.dumps(["message"]),
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
