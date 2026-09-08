import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE_URL = "https://api.avito.ru"

_token_cache = {}
_advance_cache = {}
_items_cache = {}
_tariff_cache = {}
_subscription_ops_cache = {}


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
    return request_json("GET", "/core/v1/accounts/self", token=token)


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

        payload = data.get("result") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            payload = data if isinstance(data, dict) else {}

        raw_advance = payload.get("advance")
        api_error = payload.get("error") or (
            data.get("error") if isinstance(data, dict) else None
        )

        raw_balance = payload.get("balance")
        raw_debt = payload.get("debt")

        print(
            f"CPA v2 {account_key}: "
            f"balance={raw_balance!r}, "
            f"debt={raw_debt!r}, "
            f"advance={raw_advance!r}, "
            f"error={api_error!r}, "
            f"top_keys={list(data.keys()) if isinstance(data, dict) else []}, "
            f"payload_keys={list(payload.keys())}",
            flush=True,
        )

        if api_error and raw_balance is None:
            raise RuntimeError(f"Avito CPA error: {api_error}")

        # В интерфейсе Avito Pro показатель "Аванс" соответствует
        # полю balance из /cpa/v2/balanceInfo, а не полю advance.
        # API отдаёт сумму в копейках; кабинет показывает целые рубли
        # без округления вверх.
        value = None
        if raw_balance is not None:
            try:
                value = int(float(raw_balance)) // 100
            except (TypeError, ValueError):
                print(
                    f"CPA v2 {account_key}: unexpected balance value {raw_balance!r}",
                    flush=True,
                )

        _advance_cache[account_key] = {
            "value": value,
            "expires_at": now + 65,
        }
        return value

    except Exception as exc:
        print(f"CPA advance unavailable for {account_key}: {exc}", flush=True)

        if cached and "value" in cached:
            _advance_cache[account_key] = {
                "value": cached["value"],
                "expires_at": now + 65,
            }
            return cached["value"]

        return None


def get_items_counts(account_key, token):
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

        time.sleep(2.6)
        page += 1

        if page > 1000:
            raise RuntimeError("Слишком много страниц объявлений")

    _items_cache[account_key] = {
        "value": counts,
        "expires_at": now + 600,
    }

    return counts


def _format_unix_date(timestamp, timezone_name):
    if not timestamp:
        return None
    dt = datetime.fromtimestamp(int(timestamp), ZoneInfo(timezone_name))
    return dt.strftime("%d.%m.%Y")


def get_tariff_details(account_key, token, timezone_name):
    now = time.time()
    cached = _tariff_cache.get(account_key)

    if cached and cached["expires_at"] > now:
        return cached["value"]

    try:
        data = request_json("GET", "/tariff/info/1", token=token)
    except Exception as exc:
        print(f"Tariff unavailable for {account_key}: {exc}", flush=True)
        value = {
            "placements_remaining": None,
            "tariff_end": None,
            "next_tariff": None,
        }
        _tariff_cache[account_key] = {
            "value": value,
            "expires_at": now + 600,
        }
        return value

    current = data.get("current") or {}
    scheduled = data.get("scheduled") or {}

    placements_remaining = None
    packages = current.get("packages") or []
    remains = [
        package.get("remain")
        for package in packages
        if isinstance(package.get("remain"), (int, float))
    ]
    if remains:
        placements_remaining = int(sum(remains))

    tariff_end = _format_unix_date(
        current.get("closeTime"),
        timezone_name,
    )

    next_tariff = None
    if scheduled:
        start_date = _format_unix_date(
            scheduled.get("startTime"),
            timezone_name,
        )
        amount = (scheduled.get("price") or {}).get("price")
        if start_date or amount is not None:
            next_tariff = {
                "date": start_date,
                "amount": amount,
            }

    value = {
        "placements_remaining": placements_remaining,
        "tariff_end": tariff_end,
        "next_tariff": next_tariff,
    }

    print(
        f"Tariff {account_key}: "
        f"remain={placements_remaining!r}, "
        f"end={tariff_end!r}, "
        f"next={next_tariff!r}",
        flush=True,
    )

    _tariff_cache[account_key] = {
        "value": value,
        "expires_at": now + 600,
    }
    return value



def get_subscription_operations(account_key, token, timezone_name):
    """
    Диагностика CPA-подписок через историю операций.
    Ничего не выводит пользователю: только пишет в лог найденные операции,
    чтобы определить, где Avito хранит дату/период подписки.
    """
    now = time.time()
    cached = _subscription_ops_cache.get(account_key)

    if cached and cached["expires_at"] > now:
        return cached["value"]

    tz = ZoneInfo(timezone_name)
    end_dt = datetime.now(tz)
    found = []

    print(f"Subscription scan v17 {account_key}: started", flush=True)

    # API истории принимает окно не больше недели.
    # Берём последние 35 дней.
    for window in range(5):
        window_end = end_dt - timedelta(days=7 * window)
        window_start = window_end - timedelta(days=7)

        body = {
            "dateTimeFrom": window_start.isoformat(timespec="seconds"),
            "dateTimeTo": window_end.isoformat(timespec="seconds"),
        }

        try:
            data = request_json(
                "POST",
                "/core/v1/accounts/operations_history/",
                token=token,
                data=body,
            )
        except Exception as exc:
            print(
                f"Subscription history unavailable for {account_key}: {exc}",
                flush=True,
            )
            break

        payload = data.get("result") if isinstance(data, dict) else None
        if isinstance(payload, dict):
            operations = payload.get("operations") or []
        else:
            operations = data.get("operations") or [] if isinstance(data, dict) else []

        print(
            f"Subscription scan v17 {account_key}: "
            f"window={window + 1}, operations={len(operations)}",
            flush=True,
        )
        for op in operations:
            service_type = str(op.get("serviceType") or "").casefold()
            service_name = str(op.get("serviceName") or "")
            operation_name = str(op.get("operationName") or "")
            haystack = f"{service_type} {service_name} {operation_name}".casefold()

            if service_type == "subscription" or "подпис" in haystack:
                found.append(op)

    def op_date(op):
        return str(op.get("paidAt") or op.get("updatedAt") or "")

    found.sort(key=op_date, reverse=True)

    if found:
        for op in found[:10]:
            print(
                "Subscription operation "
                f"{account_key}: "
                f"paidAt={op.get('paidAt')!r}, "
                f"updatedAt={op.get('updatedAt')!r}, "
                f"serviceType={op.get('serviceType')!r}, "
                f"serviceName={op.get('serviceName')!r}, "
                f"operationType={op.get('operationType')!r}, "
                f"amountRub={op.get('amountRub')!r}, "
                f"operationName={op.get('operationName')!r}",
                flush=True,
            )
    else:
        print(
            f"Subscription operations {account_key}: none found in last 35 days",
            flush=True,
        )

    _subscription_ops_cache[account_key] = {
        "value": found,
        "expires_at": now + 21600,
    }
    return found


def get_seller_finances(seller, timezone_name):
    key = seller["key"]
    prefix = seller["env_prefix"]
    financial_mode = seller.get("financial_mode", "advance")

    client_id = env(prefix + "_CLIENT_ID")
    client_secret = env(prefix + "_CLIENT_SECRET")

    token = get_token(key, client_id, client_secret)
    profile = get_profile(token)
    user_id = profile.get("id")

    if not user_id:
        raise RuntimeError("Avito не вернул ID профиля")

    result = {
        "key": key,
        "name": seller["name"],
        "financial_mode": financial_mode,
        "subscription_name": seller.get("subscription_name"),
        "wallet": None,
        "advance": None,
        "placements_remaining": None,
        "tariff_end": None,
        "next_tariff": None,
        "ads": {},
        "ads_pending": True,
        "warnings": [],
    }

    try:
        result["wallet"] = get_wallet(token, user_id)
    except Exception as exc:
        result["warnings"].append(f"кошелёк: {exc}")

    if financial_mode == "advance":
        try:
            result["advance"] = get_advance(key, token)
        except Exception as exc:
            result["warnings"].append(f"аванс: {exc}")

        try:
            get_subscription_operations(key, token, timezone_name)
        except Exception as exc:
            print(
                f"Subscription diagnostic failed for {key}: {exc}",
                flush=True,
            )

    # /tariff/info/1 подходит для транспортного тарифа.
    # Для CPA-аккаунтов (advance) он возвращает 404 и не описывает их подписку,
    # поэтому здесь его вызываем только для placement-аккаунтов.
    if financial_mode == "placements":
        try:
            tariff = get_tariff_details(key, token, timezone_name)
            result["tariff_end"] = tariff.get("tariff_end")
            result["next_tariff"] = tariff.get("next_tariff")
            result["placements_remaining"] = tariff.get("placements_remaining")
        except Exception as exc:
            result["warnings"].append(f"тариф: {exc}")

    return result


def _seller_token(seller):
    key = seller["key"]
    prefix = seller["env_prefix"]
    client_id = env(prefix + "_CLIENT_ID")
    client_secret = env(prefix + "_CLIENT_SECRET")
    return get_token(key, client_id, client_secret)


def add_seller_items(result, seller_config):
    key = result["key"]

    try:
        token = _seller_token(seller_config)
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
    finally:
        result["ads_pending"] = False

    return result


def get_financial_status(config):
    timezone_name = config.get("timezone", "Europe/Moscow")
    sellers = []

    for index, seller in enumerate(config["sellers"]):
        try:
            sellers.append(get_seller_finances(seller, timezone_name))
        except Exception as exc:
            sellers.append(
                {
                    "key": seller["key"],
                    "name": seller["name"],
                    "financial_mode": seller.get("financial_mode", "advance"),
                    "subscription_name": seller.get("subscription_name"),
                    "wallet": None,
                    "advance": None,
                    "placements_remaining": None,
                    "tariff_end": None,
                    "next_tariff": None,
                    "ads": {},
                    "ads_pending": False,
                    "warnings": [],
                    "error": str(exc),
                }
            )

        if index < len(config["sellers"]) - 1:
            time.sleep(1.0)

    print("Financial phase completed", flush=True)
    return {"sellers": sellers, "phase": "finances"}


def enrich_status_with_items(data, config):
    configs_by_key = {seller["key"]: seller for seller in config["sellers"]}

    for result in data["sellers"]:
        if result.get("error"):
            continue

        seller_config = configs_by_key.get(result["key"])
        if not seller_config:
            result["ads_pending"] = False
            result["warnings"].append("объявления: конфигурация аккаунта не найдена")
            continue

        add_seller_items(result, seller_config)

    data["phase"] = "complete"
    return data


def get_status(config):
    data = get_financial_status(config)
    return enrich_status_with_items(data, config)
