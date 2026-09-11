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
PAYMENT_STATE_PATH = DATA_DIR / "payment_state.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_refresh_lock = threading.Lock()
_keyboard_cleared_chats = set()
MESSAGE_TTL_SECONDS = 20 * 60
APP_VERSION = "v34"

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





def _parse_payment_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%d.%m.%Y").date()
    except (TypeError, ValueError):
        return None


def _load_payment_state():
    try:
        if not PAYMENT_STATE_PATH.exists():
            return {}
        with PAYMENT_STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print("PAYMENT STATE READ ERROR:", exc, flush=True)
        return {}


def _save_payment_state(state):
    tmp_path = PAYMENT_STATE_PATH.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp_path.replace(PAYMENT_STATE_PATH)
    except Exception as exc:
        print("PAYMENT STATE WRITE ERROR:", exc, flush=True)


def _bootstrap_payment_state_from_status_cache(state):
    # При первом запуске v33 пытаемся забрать даты из старого status_cache.json.
    cached = load_cached_status()
    if not cached:
        return state

    changed = False
    for seller in (cached.get("data") or {}).get("sellers") or []:
        if not isinstance(seller, dict):
            continue

        key = seller.get("key")
        if not key or key in state:
            continue

        payment = seller.get("subscription_next_payment") or seller.get("next_tariff")
        if not isinstance(payment, dict) or not payment.get("date"):
            continue

        state[key] = {
            "date": payment.get("date"),
            "amount": payment.get("amount"),
            "source": "status_cache",
        }
        changed = True

    if changed:
        _save_payment_state(state)

    return state


def _seller_config_by_key(key):
    for seller in CONFIG.get("sellers", []):
        if seller.get("key") == key:
            return seller
    return {}


def _apply_payment_dates(data):
    """
    Устойчивое отображение даты регулярного платежа.

    Если Avito отдаёт дату — сохраняем её.
    Если дата временно исчезла — используем последнюю известную.
    Если известная дата уже прошла, а новой даты/операции нет —
    показываем "Платёж не подтверждён".
    """
    state = _bootstrap_payment_state_from_status_cache(_load_payment_state())
    tz_name = CONFIG.get("timezone", "Europe/Moscow")
    today = datetime.now(ZoneInfo(tz_name)).date()
    changed = False

    for seller in data.get("sellers", []):
        if not isinstance(seller, dict) or seller.get("error"):
            continue

        key = seller.get("key")
        mode = seller.get("financial_mode")
        payment_key = (
            "next_tariff"
            if mode == "placements"
            else "subscription_next_payment"
        )

        payment = seller.get(payment_key)
        if not isinstance(payment, dict):
            payment = {}

        api_date = payment.get("date")
        api_amount = payment.get("amount")
        stored = state.get(key) if isinstance(state.get(key), dict) else {}
        config = _seller_config_by_key(key)

        # Новая дата из API всегда заменяет сохранённую.
        if api_date:
            if (
                stored.get("date") != api_date
                or (
                    api_amount is not None
                    and stored.get("amount") != api_amount
                )
            ):
                state[key] = {
                    "date": api_date,
                    "amount": api_amount,
                    "source": "api",
                }
                stored = state[key]
                changed = True

        else:
            fallback_date = stored.get("date")
            fallback_amount = stored.get("amount")

            # Миграционный seed используется только когда истории ещё нет,
            # а Avito уже перестал отдавать дату.
            if not fallback_date:
                seed = config.get("payment_date_seed")
                if seed:
                    fallback_date = seed
                    fallback_amount = (
                        api_amount
                        if api_amount is not None
                        else config.get("payment_amount_seed")
                    )
                    state[key] = {
                        "date": fallback_date,
                        "amount": fallback_amount,
                        "source": "seed",
                    }
                    stored = state[key]
                    changed = True

            if fallback_date:
                payment = dict(payment)
                payment["date"] = fallback_date
                if api_amount is None and fallback_amount is not None:
                    payment["amount"] = fallback_amount
                payment["date_fallback"] = True
                seller[payment_key] = payment

        effective = seller.get(payment_key)
        if isinstance(effective, dict):
            effective_date = effective.get("date")
            effective_amount = effective.get("amount")

            if effective_date and key in state:
                if (
                    effective_amount is not None
                    and state[key].get("amount") != effective_amount
                ):
                    state[key]["amount"] = effective_amount
                    changed = True

            due = _parse_payment_date(effective_date)
            if due:
                days_left = (due - today).days
                if days_left < 0:
                    effective["payment_status"] = "unconfirmed"
                elif days_left <= 3:
                    effective["payment_status"] = "soon"
                else:
                    effective["payment_status"] = "normal"

        print(
            f"Payment date v33 {key}: "
            f"api_date={api_date!r}, "
            f"effective={seller.get(payment_key)!r}, "
            f"stored={state.get(key)!r}",
            flush=True,
        )

    if changed:
        _save_payment_state(state)

    return data


def _regular_payment_line(payment):
    if not isinstance(payment, dict) or not payment:
        return None

    date = payment.get("date")
    amount = payment.get("amount")
    status = payment.get("payment_status")

    parts = []
    if date:
        parts.append(html.escape(str(date)))
    if amount is not None:
        if parts:
            parts.append("—")
        parts.append(money(amount))

    if not parts:
        return None

    if status == "unconfirmed":
        return f"🔴 <b>Платёж не подтверждён: {' '.join(parts)}</b>"

    text = "Регулярный платёж: " + " ".join(parts)

    if status == "soon":
        return f"🔴 <b>{text}</b>"

    return text


def _seller_full_block(seller):
    name = html.escape(seller.get("name") or _seller_short_name(seller))

    if seller.get("error"):
        return f"<b>{name}</b>\nОшибка: {html.escape(str(seller['error']))}"

    stats = seller.get("ads") or {}
    lines = [f"<b>{name}</b>"]

    lines.append(f"Кошелёк: {money(seller.get('wallet'))}")

    if seller.get("financial_mode") == "placements":
        if seller.get("placements_remaining") is not None:
            lines.append(
                f"Остаток размещений: {plain_number(seller.get('placements_remaining'))}"
            )
    elif seller.get("advance") is not None:
        lines.append(f"Аванс: {money(seller.get('advance'))}")

    lines.extend([
        f"Опубликовано: {plain_number(stats.get('published'))}",
        f"Отклонено: {plain_number(stats.get('rejected'))}",
        f"Заблокировано: {plain_number(stats.get('blocked'))}",
        f"Снято: {plain_number(stats.get('removed'))}",
        f"Завершено: {plain_number(stats.get('old'))}",
    ])


    payment = seller.get("subscription_next_payment") or seller.get("next_tariff")
    payment_line = _regular_payment_line(payment)
    if payment_line:
        lines.append(payment_line)

    return "\n".join(lines)


def _seller_balance_block(seller):
    name = html.escape(seller.get("name") or _seller_short_name(seller))

    if seller.get("error"):
        return f"<b>{name}</b>\nОшибка: {html.escape(str(seller['error']))}"

    lines = [
        f"<b>{name}</b>",
        f"Кошелёк: {money(seller.get('wallet'))}",
    ]

    if seller.get("financial_mode") == "placements":
        if seller.get("placements_remaining") is not None:
            lines.append(
                f"Остаток размещений: {plain_number(seller.get('placements_remaining'))}"
            )
    elif seller.get("advance") is not None:
        lines.append(f"Аванс: {money(seller.get('advance'))}")


    payment = seller.get("subscription_next_payment") or seller.get("next_tariff")
    payment_line = _regular_payment_line(payment)
    if payment_line:
        lines.append(payment_line)

    return "\n".join(lines)


def _seller_ads_block(seller):
    name = html.escape(seller.get("name") or _seller_short_name(seller))

    if seller.get("error"):
        return f"<b>{name}</b>\nОшибка: {html.escape(str(seller['error']))}"

    stats = seller.get("ads") or {}
    return "\n".join([
        f"<b>{name}</b>",
        f"Опубликовано: {plain_number(stats.get('published'))}",
        f"Отклонено: {plain_number(stats.get('rejected'))}",
        f"Заблокировано: {plain_number(stats.get('blocked'))}",
        f"Снято: {plain_number(stats.get('removed'))}",
        f"Завершено: {plain_number(stats.get('old'))}",
    ])


def render_status(data, fetched_at=None):
    blocks = ["<b>АВИТО</b>"]
    blocks.extend(_seller_full_block(seller) for seller in data["sellers"])
    blocks.append(_updated_line(fetched_at))
    return "\n\n".join(blocks)


def render_balances(data, fetched_at=None):
    blocks = ["<b>АВИТО · БАЛАНС</b>"]
    blocks.extend(_seller_balance_block(seller) for seller in data["sellers"])
    blocks.append(_updated_line(fetched_at))
    return "\n\n".join(blocks)


def render_ads(data, fetched_at=None):
    blocks = ["<b>АВИТО · ОБЪЯВЛЕНИЯ</b>"]
    blocks.extend(_seller_ads_block(seller) for seller in data["sellers"])
    blocks.append(_updated_line(fetched_at))
    return "\n\n".join(blocks)


def render_account(data, fetched_at=None):
    return "\n\n".join([
        "<b>АВИТО</b>",
        _seller_full_block(data["sellers"][0]),
        _updated_line(fetched_at),
    ])


def render_account_balance(data, fetched_at=None):
    return "\n\n".join([
        "<b>АВИТО · БАЛАНС</b>",
        _seller_balance_block(data["sellers"][0]),
        _updated_line(fetched_at),
    ])


def render_account_ads(data, fetched_at=None):
    return "\n\n".join([
        "<b>АВИТО · ОБЪЯВЛЕНИЯ</b>",
        _seller_ads_block(data["sellers"][0]),
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
                renderer = render_account_balance if seller_key else render_balances
            elif mode == "ads":
                data = get_items_status(query_config)
                renderer = render_account_ads if seller_key else render_ads
            else:
                data = get_status(query_config)
                renderer = render_account if seller_key else render_status

            if mode != "ads":
                data = _apply_payment_dates(data)

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
                    "<b>АВИТО</b>\n\nНе удалось получить актуальные данные.",
                )
            except Exception as edit_exc:
                print("ERROR EDIT ERROR:", edit_exc, flush=True)
        finally:
            delete_message_later(chat_id, message_id, delay=MESSAGE_TTL_SECONDS)

def delete_message_later(chat_id, message_id, delay=MESSAGE_TTL_SECONDS):
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



ACCOUNT_COMMAND_SUFFIXES = {
    "aggregaty": "aggregaty",
    "avmex": "avmex",
    "orange": "nm_orange",
    "blue": "nm_blue",
    "tir": "tir",
}


def _parse_direct_command(command):
    """
    Команды только в прямом формате:
    /status_avmex
    /balance_avmex
    /ads_avmex
    и *_all для всех аккаунтов.
    """
    if command == "/status_all":
        return "full", None
    if command == "/balance_all":
        return "finance", None
    if command == "/ads_all":
        return "ads", None

    for suffix, seller_key in ACCOUNT_COMMAND_SUFFIXES.items():
        if command == f"/status_{suffix}":
            return "full", seller_key
        if command == f"/balance_{suffix}":
            return "finance", seller_key
        if command == f"/ads_{suffix}":
            return "ads", seller_key

    return None, None


def _commands_help():
    return (
        "<b>Команды</b>\n\n"
        "<code>/status_all</code> — всё по всем аккаунтам\n"
        "<code>/balance_all</code> — баланс по всем аккаунтам\n"
        "<code>/ads_all</code> — объявления по всем аккаунтам\n\n"
        "Для конкретного аккаунта:\n"
        "<code>/status_avmex</code>\n"
        "<code>/balance_avmex</code>\n"
        "<code>/ads_avmex</code>\n\n"
        "Суффиксы: <code>aggregaty</code>, <code>avmex</code>, "
        "<code>orange</code>, <code>blue</code>, <code>tir</code>"
    )


def send_query(chat_id, mode="full", seller_key=None):
    remove_legacy_keyboard(chat_id)
    labels = {
        "full": "Получаю актуальные данные…",
        "finance": "Получаю данные по балансу…",
        "ads": "Считаю статусы объявлений…",
    }
    message = telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": f"<b>АВИТО</b>\n\n{labels.get(mode, labels['full'])}",
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

    command = text.split(maxsplit=1)[0].split("@", 1)[0].casefold()

    # Удаляем и саму команду пользователя через 20 минут.
    # В личном чате это работает штатно; в группе боту нужны права на удаление.
    delete_message_later(
        chat_id,
        message["message_id"],
        delay=MESSAGE_TTL_SECONDS,
    )

    # /start оставляем только как вход в бота; он показывает список команд.
    if command == "/start":
        help_message = telegram_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": _commands_help(),
                "parse_mode": "HTML",
            },
        )
        delete_message_later(
            chat_id,
            help_message["message_id"],
            delay=MESSAGE_TTL_SECONDS,
        )
        return

    mode, seller_key = _parse_direct_command(command)
    if mode:
        send_query(chat_id, mode, seller_key)
        return

    if command in {"/help", "/commands"}:
        help_message = telegram_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": _commands_help(),
                "parse_mode": "HTML",
            },
        )
        delete_message_later(
            chat_id,
            help_message["message_id"],
            delay=MESSAGE_TTL_SECONDS,
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
                        {"command": "status_all", "description": "Вся информация по всем аккаунтам"},
                        {"command": "balance_all", "description": "Баланс по всем аккаунтам"},
                        {"command": "ads_all", "description": "Статусы объявлений по всем аккаунтам"},

                        {"command": "status_aggregaty", "description": "Всё: Агрегаты"},
                        {"command": "balance_aggregaty", "description": "Баланс: Агрегаты"},
                        {"command": "ads_aggregaty", "description": "Объявления: Агрегаты"},

                        {"command": "status_avmex", "description": "Всё: Авмекс"},
                        {"command": "balance_avmex", "description": "Баланс: Авмекс"},
                        {"command": "ads_avmex", "description": "Объявления: Авмекс"},

                        {"command": "status_orange", "description": "Всё: НМ оранжевые"},
                        {"command": "balance_orange", "description": "Баланс: НМ оранжевые"},
                        {"command": "ads_orange", "description": "Объявления: НМ оранжевые"},

                        {"command": "status_blue", "description": "Всё: НМ синие"},
                        {"command": "balance_blue", "description": "Баланс: НМ синие"},
                        {"command": "ads_blue", "description": "Объявления: НМ синие"},

                        {"command": "status_tir", "description": "Всё: ТИР"},
                        {"command": "balance_tir", "description": "Баланс: ТИР"},
                        {"command": "ads_tir", "description": "Объявления: ТИР"},

                        {"command": "commands", "description": "Показать список команд"},
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
