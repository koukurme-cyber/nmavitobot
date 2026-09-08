import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BASE_URL = "https://api.avito.ru"

_token_cache = {}
_advance_cache = {}
_items_cache = {}


def env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


def request_json(
    method,
    path,
    token=None,
    data=None,
    headers=None,
    timeout=25,
    max_retries=5,
):
    request_headers = {"Accept": "application/json"}

    if token:
        request_headers["Authorization"] = f"Bearer {token}"

    if headers:
        request_headers.update(headers)

    body = None

    if data is not None:
        if request_headers.get("Content-Type") == "application/x-www-form-urlencoded":
            body = urllib.parse.urlencode(data).encode("utf-8")
        else:
            request_headers.setdefault("Content-Type", "application/json")
            body = json.dumps(data).encode("utf-8")

    for attempt in range(max_retries + 1):
        request = urllib.request.Request(
            BASE_URL + path,
            data=body,
            headers=request_headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}

        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")

            if exc.code == 429 and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After")
                try:
                    wait_seconds = float(retry_after) if retry_after else 0
                except (TypeError, ValueError):
                    wait_seconds = 0

                if wait_seconds <= 0:
                    # Более осторожный backoff для Avito:
                    # 5, 10, 20, 30, 45 секунд.
                    schedule = [5, 10, 20, 30, 45]
                    wait_seconds = schedule[min(attempt, len(schedule) - 1)]

                print(
                    f"Avito 429 for {path}; retry in {wait_seconds:.1f}s "
                    f"({attempt + 1}/{max_retries})",
                    flush=True,
                )
                time.sleep(wait_seconds)
                continue

            raise RuntimeError(f"Avito API {exc.code}: {raw[:500]}") from exc


def get_token(account_key, client_id, client_secret):
    now = time.time()
    cached = _token_cache.get(account_key)

    if cached and cached["expires_at"] > now + 60:
        return cached["token"]

    result = request_json(
        "POST",
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    token = result["access_token"]
    expires_in = int(result.get("expires_in", 86400))

    _token_cache[account_key] = {
        "token": token,
        "expires_at": now + expires_in,
    }

    return token


def get_profile(token):
    return request_json(
        "GET",
        "/core/v1/accounts/self",
        token=token,
    )


def get_wallet(token, user_id):
    data = request_json(
        "GET",
        f"/core/v1/accounts/{user_id}/balance/",
        token=token,
    )

    return data.get("real")


def get_advance(account_key, token):
    now = time.time()
    cached = _advance_cache.get(account_key)

    # CPA v2 имеет жёсткий лимит около 1 запроса в минуту.
    # Поэтому используем кэш минимум 65 секунд.
    if cached and cached["expires_at"] > now:
        return cached["value"]

    try:
        data = request_json(
            "POST",
            "/cpa/v2/balanceInfo",
            token=token,
            data={},
            headers={"X-Source": "avito-telegram-status-bot"},
        )

        amount = data.get("advance")
        if isinstance(amount, (int, float)):
            value = amount / 100.0
        else:
            value = None

        _advance_cache[account_key] = {
            "value": value,
            "expires_at": now + 65,
        }
        return value

    except Exception as exc:
        print(f"CPA advance unavailable for {account_key}: {exc}", flush=True)

        # Если раньше уже было успешное значение, не теряем его из-за
        # временного лимита/API-сбоя.
        if cached and "value" in cached:
            _advance_cache[account_key] = {
                "value": cached["value"],
                "expires_at": now + 65,
            }
            return cached["value"]

        return None

def get_items_counts(account_key, token):
    # Статусы объявлений меняются не каждую секунду.
    # Кэшируем на 10 минут, чтобы /start и кнопка "Обновить"
    # не пересчитывали тысячи объявлений заново.
    now = time.time()
    cached = _items_cache.get(account_key)

    if cached and cached["expires_at"] > now:
        return cached["value"]

    statuses = ["active", "removed", "old", "blocked", "rejected"]
    counts = {status: 0 for status in statuses}

    page = 1
    per_page = 99

    while True:
        query = urllib.parse.urlencode(
            {
                "status": ",".join(statuses),
                "page": page,
                "per_page": per_page,
            }
        )

        data = request_json(
            "GET",
            f"/core/v1/items?{query}",
            token=token,
        )

        items = data.get("resources") or []

        for item in items:
            status = item.get("status")
            if status in counts:
                counts[status] += 1

        if len(items) < per_page:
            break

        # Намного осторожнее: максимум примерно 1 страница в 1.5 сек.
        time.sleep(1.5)

        page += 1

        if page > 1000:
            raise RuntimeError("Слишком много страниц объявлений")

    _items_cache[account_key] = {
        "value": counts,
        "expires_at": now + 600,
    }

    return counts

def get_next_tariff_payment(token, timezone_name):
    try:
        data = request_json(
            "GET",
            "/tariff/info/1",
            token=token,
        )
    except Exception:
        return None

    scheduled = data.get("scheduled")
    if not scheduled:
        return None

    timestamp = scheduled.get("startTime")
    price = (scheduled.get("price") or {}).get("price")

    if timestamp is None and price is None:
        return None

    date_text = None

    if timestamp:
        dt = datetime.fromtimestamp(
            int(timestamp),
            ZoneInfo(timezone_name),
        )
        date_text = dt.strftime("%d.%m.%Y")

    return {
        "date": date_text,
        "amount": price,
    }


def get_seller_status(seller, timezone_name):
    key = seller["key"]
    prefix = seller["env_prefix"]

    client_id = env(prefix + "_CLIENT_ID")
    client_secret = env(prefix + "_CLIENT_SECRET")

    token = get_token(
        key,
        client_id,
        client_secret,
    )

    profile = get_profile(token)
    user_id = profile.get("id")

    if not user_id:
        raise RuntimeError("Avito не вернул ID профиля")

    result = {
        "key": key,
        "name": seller["name"],
        "wallet": None,
        "advance": None,
        "ads": {},
        "next_payment": None,
        "warnings": [],
    }

    # Сначала быстрые финансовые данные.
    try:
        result["wallet"] = get_wallet(token, user_id)
    except Exception as exc:
        result["warnings"].append(f"кошелёк: {exc}")

    try:
        result["advance"] = get_advance(key, token)
    except Exception as exc:
        result["warnings"].append(f"аванс: {exc}")

    try:
        result["next_payment"] = get_next_tariff_payment(
            token,
            timezone_name,
        )
    except Exception as exc:
        result["warnings"].append(f"тариф: {exc}")

    # И только после этого — тяжёлый обход объявлений.
    try:
        counts = get_items_counts(key, token)
        result["ads"] = {
            "published": counts["active"],
            "rejected": counts["rejected"],
            "blocked": counts["blocked"],
            "removed": counts["removed"],
            "old": counts["old"],
        }
    except Exception as exc:
        result["warnings"].append(f"объявления: {exc}")

    return result

def get_status(config):
    timezone_name = config.get("timezone", "Europe/Moscow")
    sellers = []

    for index, seller in enumerate(config["sellers"]):
        try:
            sellers.append(
                get_seller_status(
                    seller,
                    timezone_name,
                )
            )
        except Exception as exc:
            sellers.append(
                {
                    "key": seller["key"],
                    "name": seller["name"],
                    "error": str(exc),
                }
            )

        # Не начинаем второй аккаунт сразу вслед за первым.
        if index < len(config["sellers"]) - 1:
            time.sleep(1.5)

    return {"sellers": sellers}
