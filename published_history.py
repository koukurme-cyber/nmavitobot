"""История числа опубликованных объявлений (v46).

При каждом подсчёте объявлений число «Опубликовано» запоминается по аккаунту.
Ориентир — максимум за последние 7 дней. Если сейчас опубликовано меньше
ориентира больше чем на 10, рядом со строкой «Опубликовано» ставится ⚠️.

История лежит в DATA_DIR/published_history.json и не зависит от автоудаления
сообщений в Telegram.
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WINDOW_SECONDS = 7 * 24 * 60 * 60
DROP_THRESHOLD = 10               # строго больше -> ⚠️
DEDUP_SECONDS = 10 * 60           # одинаковое значение чаще раза в 10 минут не пишем
TIMEZONE = "Europe/Moscow"

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PATH = Path(os.getenv("DATA_DIR", os.path.join(_BASE_DIR, "data"))) / "published_history.json"
_lock = threading.Lock()


def _load():
    try:
        if _PATH.exists():
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        print("PUBLISHED HISTORY READ ERROR:", exc, flush=True)
    return {}


def _save(data):
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PATH)
    except Exception as exc:
        print("PUBLISHED HISTORY WRITE ERROR:", exc, flush=True)


def reference_from(entries, now_ts):
    """Максимум за окно среди прошлых записей: {"max": n, "ts": t} или None."""
    best = None
    for ts, value in entries:
        if now_ts - ts > WINDOW_SECONDS or ts > now_ts:
            continue
        if best is None or value > best[1] or (value == best[1] and ts > best[0]):
            best = (ts, value)
    if best is None:
        return None
    return {"max": best[1], "ts": best[0]}


def record(account_key, published, now_ts=None):
    """Запомнить значение и вернуть ориентир по истории ДО этой записи."""
    if published is None:
        return None
    now_ts = now_ts or time.time()
    with _lock:
        data = _load()
        entries = [
            (ts, value) for ts, value in data.get(account_key, [])
            if now_ts - ts <= WINDOW_SECONDS
        ]
        ref = reference_from(entries, now_ts)

        last = entries[-1] if entries else None
        if not (last and last[1] == published and now_ts - last[0] < DEDUP_SECONDS):
            entries.append((now_ts, int(published)))
        data[account_key] = entries
        _save(data)
    return ref


def published_text(stats):
    """Строка «Опубликовано: …» с ⚠️ при существенном падении."""
    published = stats.get("published") if isinstance(stats, dict) else None
    if published is None:
        return "Опубликовано: —"
    number = f"{int(published):,}".replace(",", " ")
    ref = stats.get("published_ref") if isinstance(stats, dict) else None
    if ref and ref.get("max") is not None and ref["max"] - published > DROP_THRESHOLD:
        when = datetime.fromtimestamp(ref["ts"], ZoneInfo(TIMEZONE)).strftime("%d.%m")
        was = f"{int(ref['max']):,}".replace(",", " ")
        return f"⚠️ <b>Опубликовано: {number}</b> (было {was} на {when})"
    return f"Опубликовано: {number}"
