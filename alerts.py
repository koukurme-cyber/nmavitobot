"""Предупреждения «вне расписания» (v42).

1. Аванс < 1000 ₽ у аккаунтов с авансом (Агрегаты, Авмекс, ТИР), пока показы ещё идут.
2. Больше 10 объявлений за проверку ушли из публикации или попали в «Отклонено».

Модуль запускается потоком из bot.py до загрузки основной логики, поэтому
всё, что нужно из бота (telegram_api, CONFIG, реестр чатов и т.д.), берётся
из словаря globals() бота лениво, уже во время работы.
"""

import html
import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ADVANCE_THRESHOLD = 1000          # ₽, строго меньше -> тревога
ADS_CHANGE_THRESHOLD = 10         # строго больше -> тревога

MONEY_CHECK_INTERVAL = 30 * 60    # сек
MONEY_REPEAT_INTERVAL = 3 * 60 * 60
MONEY_WINDOW = (8, 20)            # проверять с 08:00 до 20:00 по Москве, только Пн–Пт

ADS_CHECK_TIMES = ("09:00", "13:00", "17:00")
ADS_SLOT_GRACE = 10 * 60          # допустимое опоздание слота (рестарт бота)

STATE_FILE = "alert_state.json"


# ---------- чистая логика (без сети) ----------

def _num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def evaluate_money(sellers, state, now_ts):
    """Вернуть (список тревог, новое состояние). Только поле «Аванс»."""
    money_state = dict(state.get("money") or {})
    alerts = []

    for seller in sellers:
        key = seller.get("key")
        if seller.get("error") or seller.get("financial_mode") == "placements":
            continue
        advance = seller.get("advance")
        if advance is None:
            continue
        try:
            advance = float(advance)
        except (TypeError, ValueError):
            continue

        if advance >= ADVANCE_THRESHOLD:
            money_state.pop(key, None)  # восстановился -> сбрасываем, чтобы сработало снова
            continue

        last = (money_state.get(key) or {}).get("last_alert_ts", 0)
        if now_ts - last >= MONEY_REPEAT_INTERVAL:
            alerts.append({"seller": seller, "advance": advance})
            money_state[key] = {"last_alert_ts": now_ts}

    new_state = dict(state)
    new_state["money"] = money_state
    return alerts, new_state


def evaluate_ads(sellers, state):
    """Вернуть (список тревог, новое состояние). Сравнение с прошлой проверкой."""
    prev_all = state.get("ads") or {}
    new_all = dict(prev_all)
    alerts = []

    for seller in sellers:
        key = seller.get("key")
        if seller.get("error"):
            continue  # базу не трогаем, чтобы не потерять прошлые значения
        stats = seller.get("ads") or {}
        cur = {name: _num(stats.get(name)) for name in
               ("published", "rejected", "blocked", "removed", "old")}
        if cur["published"] is None or cur["rejected"] is None:
            continue

        prev = prev_all.get(key)
        new_all[key] = cur
        if not prev:
            continue  # первая проверка — только запоминаем базу

        def delta(name):
            a, b = cur.get(name), prev.get(name)
            return None if a is None or b is None else a - b

        dropped = -(delta("published") or 0)       # сколько вышло из публикации
        rejected_up = delta("rejected") or 0       # сколько новых отклонённых

        if dropped > ADS_CHANGE_THRESHOLD or rejected_up > ADS_CHANGE_THRESHOLD:
            alerts.append({
                "seller": seller,
                "published": cur["published"],
                "dropped": dropped,
                "rejected_up": rejected_up,
                "blocked_up": delta("blocked") or 0,
                "removed_up": delta("removed") or 0,
                "old_up": delta("old") or 0,
            })

    new_state = dict(state)
    new_state["ads"] = new_all
    return alerts, new_state


def _fmt(value):
    return f"{int(value):,}".replace(",", " ")


def _name(seller):
    return html.escape(seller.get("name") or seller.get("key") or "?")


def money_text(alert):
    advance = alert["advance"]
    if advance <= 0:
        head = "🔴 <b>Аванс закончился</b>"
    else:
        head = "⚠️ <b>Аванс заканчивается</b>"
    return f"{head}\n{_name(alert['seller'])}: {_fmt(advance)} ₽ (порог {_fmt(ADVANCE_THRESHOLD)} ₽)"


def ads_text(alert):
    lines = [f"⚠️ <b>Объявления выпадают из публикации</b>", _name(alert["seller"])]
    lines.append(f"Опубликовано сейчас: {_fmt(alert['published'])}")
    parts = [
        ("Вышло из публикации", max(alert["dropped"], 0)),
        ("Новых отклонённых", alert["rejected_up"]),
        ("Новых заблокированных", alert["blocked_up"]),
        ("Снято", alert["removed_up"]),
        ("Завершено", alert["old_up"]),
    ]
    for label, value in parts:
        if value:
            lines.append(f"{label}: {_fmt(value)}")
    lines.append("<i>С прошлой проверки.</i>")
    return "\n".join(lines)


# ---------- работа с состоянием ----------

def _state_path(g):
    return g["DATA_DIR"] / STATE_FILE


def _load_state(g):
    try:
        path = _state_path(g)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        print("ALERT STATE READ ERROR:", exc, flush=True)
    return {}


def _save_state(g, state):
    try:
        path = _state_path(g)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        print("ALERT STATE WRITE ERROR:", exc, flush=True)


def _chat_ids(g):
    ids = []
    for key in g["_load_notify_settings"]().keys():
        try:
            ids.append(int(key))
        except ValueError:
            ids.append(key)
    return ids


def _send(g, text):
    # Предупреждения намеренно не удаляются автоматически.
    for chat_id in _chat_ids(g):
        try:
            g["telegram_api"]("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            })
        except Exception as exc:
            print(f"ALERT SEND ERROR chat={chat_id}: {exc}", flush=True)


# ---------- цикл ----------

def _check_money(g, now_ts):
    from avito_provider import get_financial_status

    lock = g.get("_refresh_lock") or threading.Lock()
    with lock:
        data = get_financial_status(g["CONFIG"])
    state = _load_state(g)
    alerts, state = evaluate_money(data["sellers"], state, now_ts)
    _save_state(g, state)
    for alert in alerts:
        _send(g, money_text(alert))
    print(f"Alerts money check: {len(alerts)} sent", flush=True)


def _check_ads(g):
    from avito_provider import get_items_status

    lock = g.get("_refresh_lock") or threading.Lock()
    with lock:
        data = get_items_status(g["CONFIG"])
    state = _load_state(g)
    alerts, state = evaluate_ads(data["sellers"], state)
    _save_state(g, state)
    for alert in alerts:
        _send(g, ads_text(alert))
    print(f"Alerts ads check: {len(alerts)} sent", flush=True)


def _alerts_loop(get_globals):
    last_money_check = 0.0

    while True:
        try:
            g = get_globals()
            needed = ("telegram_api", "CONFIG", "DATA_DIR", "_load_notify_settings")
            if not all(name in g for name in needed):
                time.sleep(5)  # основной модуль бота ещё загружается
                continue

            tz = ZoneInfo(g["CONFIG"].get("timezone", "Europe/Moscow"))
            now = datetime.now(tz)
            now_ts = time.time()

            if now.weekday() >= 5:  # выходные: ничего не проверяем
                time.sleep(60)
                continue

            if (MONEY_WINDOW[0] <= now.hour < MONEY_WINDOW[1]
                    and now_ts - last_money_check >= MONEY_CHECK_INTERVAL):
                last_money_check = now_ts
                try:
                    _check_money(g, now_ts)
                except Exception as exc:
                    print("ALERT MONEY CHECK ERROR:", exc, flush=True)

            today = now.date().isoformat()
            for slot in ADS_CHECK_TIMES:
                hh, mm = [int(x) for x in slot.split(":", 1)]
                slot_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                late = (now - slot_dt).total_seconds()
                if not (0 <= late < ADS_SLOT_GRACE):
                    continue
                state = _load_state(g)
                done = state.setdefault("ads_slots", {})
                if done.get(slot) == today:
                    continue
                done[slot] = today
                _save_state(g, state)
                try:
                    _check_ads(g)
                except Exception as exc:
                    print("ALERT ADS CHECK ERROR:", exc, flush=True)
        except Exception as exc:
            print("ALERTS LOOP ERROR:", exc, flush=True)

        time.sleep(20)


def run_alerts(get_globals):
    _alerts_loop(get_globals)
