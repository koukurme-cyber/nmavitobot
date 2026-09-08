import json
import html as html_module
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
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



def _extract_report_id(report):
    if not isinstance(report, dict):
        return None
    for key in ("id", "report_id", "reportId"):
        value = report.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def diagnose_placement_usage(account_key, token, timezone_name, start_timestamp):
    """
    Глубокая диагностика остатка размещений для транспортного тарифа.

    1) История операций с начала текущего тарифа: ищем listing fee / пакетные
       списания и операции, в названиях которых есть размещения/пакеты.
    2) Старый Autoload v2 (пока ещё работает) — отчёты и fees, где Avito
       документирует списания размещений из пакета.
    """
    if not start_timestamp:
        print(
            f"Placement scan v22 {account_key}: no tariff startTime",
            flush=True,
        )
        return

    tz = ZoneInfo(timezone_name)
    start_dt = datetime.fromtimestamp(int(start_timestamp), tz)
    now_dt = datetime.now(tz)

    print(
        f"Placement scan v22 {account_key}: "
        f"period={start_dt.isoformat()}..{now_dt.isoformat()}",
        flush=True,
    )

    # ---- 1. Operations history from tariff start ----
    all_ops = []
    cursor = start_dt
    window_no = 0

    while cursor < now_dt and window_no < 12:
        window_end = min(cursor + timedelta(days=7), now_dt)
        window_no += 1

        try:
            data = request_json(
                "POST",
                "/core/v1/accounts/operations_history/",
                token=token,
                data={
                    "dateTimeFrom": cursor.isoformat(timespec="seconds"),
                    "dateTimeTo": window_end.isoformat(timespec="seconds"),
                },
            )
            payload = data.get("result") if isinstance(data, dict) else None
            operations = (
                payload.get("operations") or []
                if isinstance(payload, dict)
                else []
            )
            all_ops.extend(operations)
            print(
                f"Placement scan v22 {account_key}: "
                f"operations window={window_no}, count={len(operations)}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"Placement scan v22 {account_key}: "
                f"operations history error: {exc}",
                flush=True,
            )
            break

        cursor = window_end

    service_counts = Counter(
        str(op.get("serviceType") or "")
        for op in all_ops
        if isinstance(op, dict)
    )
    print(
        f"Placement scan v22 {account_key}: "
        f"service_counts={dict(service_counts)}",
        flush=True,
    )

    interesting_ops = []
    for op in all_ops:
        if not isinstance(op, dict):
            continue
        service_type = str(op.get("serviceType") or "").casefold()
        text = " ".join(
            str(op.get(k) or "")
            for k in ("serviceName", "operationName", "operationType")
        ).casefold()

        if (
            service_type in {"lf", "tariff", "bundle"}
            or "размещ" in text
            or "пакет" in text
        ):
            interesting_ops.append(op)

    print(
        f"Placement scan v22 {account_key}: "
        f"interesting_operations={len(interesting_ops)}",
        flush=True,
    )

    for op in interesting_ops[:100]:
        print(
            f"Placement op {account_key}: "
            f"updatedAt={op.get('updatedAt')!r}, "
            f"serviceType={op.get('serviceType')!r}, "
            f"serviceId={op.get('serviceId')!r}, "
            f"itemId={op.get('itemId')!r}, "
            f"amountRub={op.get('amountRub')!r}, "
            f"amountTotal={op.get('amountTotal')!r}, "
            f"serviceName={op.get('serviceName')!r}, "
            f"operationType={op.get('operationType')!r}, "
            f"operationName={op.get('operationName')!r}",
            flush=True,
        )

    # ---- 2. Autoload v2 reports / fees ----
    try:
        reports_data = request_json(
            "GET",
            "/autoload/v2/reports?per_page=5&page=0",
            token=token,
        )
        reports = reports_data.get("reports") or []
        print(
            f"Placement autoload v22 {account_key}: "
            f"reports_count={len(reports)}, "
            f"top_keys={list(reports_data.keys()) if isinstance(reports_data, dict) else []}",
            flush=True,
        )

        for report in reports[:5]:
            if isinstance(report, dict):
                print(
                    f"Placement autoload report {account_key}: {report!r}",
                    flush=True,
                )

        report_id = _extract_report_id(reports[0]) if reports else None
        if report_id is not None:
            try:
                detail = request_json(
                    "GET",
                    f"/autoload/v2/reports/{report_id}",
                    token=token,
                )
                print(
                    f"Placement autoload detail {account_key}: "
                    f"report_id={report_id}, payload={detail!r}",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"Placement autoload detail {account_key}: "
                    f"report_id={report_id}, error={exc}",
                    flush=True,
                )

            try:
                fees_data = request_json(
                    "GET",
                    f"/autoload/v2/reports/{report_id}/items/fees"
                    "?per_page=200&page=0",
                    token=token,
                )
                fees = fees_data.get("fees") or []
                package_fees = [
                    fee for fee in fees
                    if isinstance(fee, dict)
                    and fee.get("fees_type") == "package"
                ]
                package_ids = Counter(
                    str(fee.get("fees_package_id"))
                    for fee in package_fees
                )
                placements_spent = sum(
                    int(fee.get("fees_amount") or 0)
                    for fee in package_fees
                    if isinstance(fee.get("fees_amount"), (int, float))
                )

                print(
                    f"Placement autoload fees {account_key}: "
                    f"report_id={report_id}, "
                    f"fees_count={len(fees)}, "
                    f"package_fees={len(package_fees)}, "
                    f"placements_spent={placements_spent}, "
                    f"package_ids={dict(package_ids)}",
                    flush=True,
                )

                for fee in package_fees[:50]:
                    print(
                        f"Placement fee {account_key}: {fee!r}",
                        flush=True,
                    )
            except Exception as exc:
                print(
                    f"Placement autoload fees {account_key}: "
                    f"report_id={report_id}, error={exc}",
                    flush=True,
                )

    except Exception as exc:
        print(
            f"Placement autoload v22 {account_key}: unavailable: {exc}",
            flush=True,
        )



def diagnose_web_tariff_tile(account_key, token):
    """
    Проверяет, отдаёт ли веб-страница Avito Pro плитку
    «Остаток размещений» при авторизации только OAuth Bearer-токеном API.
    Никакие cookies браузера не используются.
    """
    url = "https://www.avito.ru/professionals/tariff"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "Mozilla/5.0",
    }

    request = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            status = getattr(response, "status", None)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(
            f"Web tariff v23 {account_key}: "
            f"HTTP {exc.code}, url={exc.geturl()!r}, "
            f"body_prefix={body[:300]!r}",
            flush=True,
        )
        return None
    except Exception as exc:
        print(
            f"Web tariff v23 {account_key}: request failed: {exc}",
            flush=True,
        )
        return None

    text = raw.decode("utf-8", errors="replace")
    decoded = html_module.unescape(text)

    patterns = [
        r'"title":"Остаток размещений","value":"([^"]+)"',
        r'"title"\s*:\s*"Остаток размещений"\s*,\s*"value"\s*:\s*"([^"]+)"',
    ]

    value = None
    for pattern in patterns:
        match = re.search(pattern, decoded)
        if match:
            value = match.group(1)
            break

    has_tile_text = "Остаток размещений" in decoded

    print(
        f"Web tariff v23 {account_key}: "
        f"status={status}, final_url={final_url!r}, "
        f"content_type={content_type!r}, html_len={len(text)}, "
        f"has_tile_text={has_tile_text}, value={value!r}",
        flush=True,
    )

    if has_tile_text and value is None:
        pos = decoded.find("Остаток размещений")
        snippet = decoded[max(0, pos - 250):pos + 500]
        print(
            f"Web tariff v23 {account_key}: tile snippet={snippet!r}",
            flush=True,
        )

    return value


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

    current_packages = current.get("packages") or []
    scheduled_packages = scheduled.get("packages") or []

    print(
        f"Tariff contract v22 {account_key}: "
        f"current_level={current.get('level')!r}, "
        f"current_start={current.get('startTime')!r}, "
        f"current_close={current.get('closeTime')!r}, "
        f"current_price={current.get('price')!r}, "
        f"current_bonus={current.get('bonus')!r}, "
        f"current_packages={len(current_packages)}, "
        f"scheduled_level={scheduled.get('level')!r}, "
        f"scheduled_start={scheduled.get('startTime')!r}, "
        f"scheduled_price={scheduled.get('price')!r}, "
        f"scheduled_packages={len(scheduled_packages)}",
        flush=True,
    )

    for idx, package in enumerate(scheduled_packages, start=1):
        print(
            f"Tariff scheduled package {account_key} #{idx}: {package!r}",
            flush=True,
        )

    placements_remaining = None
    packages = current.get("packages") or []

    print(
        f"Tariff raw {account_key}: "
        f"current_keys={list(current.keys()) if isinstance(current, dict) else []}, "
        f"packages_count={len(packages) if isinstance(packages, list) else 0}",
        flush=True,
    )

    if isinstance(packages, list):
        for idx, package in enumerate(packages, start=1):
            if isinstance(package, dict):
                print(
                    f"Tariff package {account_key} #{idx}: "
                    f"keys={list(package.keys())}, "
                    f"payload={package!r}",
                    flush=True,
                )
            else:
                print(
                    f"Tariff package {account_key} #{idx}: unexpected={package!r}",
                    flush=True,
                )

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

    if account_key in {"nm_orange", "nm_blue"}:
        try:
            diagnose_web_tariff_tile(account_key, token)
        except Exception as exc:
            print(
                f"Web tariff v23 {account_key}: fatal diagnostic error: {exc}",
                flush=True,
            )

        try:
            diagnose_placement_usage(
                account_key,
                token,
                timezone_name,
                current.get("startTime"),
            )
        except Exception as exc:
            print(
                f"Placement scan v22 {account_key}: fatal diagnostic error: {exc}",
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


def get_subscription_details(seller, token, timezone_name):
    """
    Рассчитывает окончание текущего периода подписки по последней операции
    подписки в истории и длительности периода из config.json.
    """
    key = seller["key"]
    period_days = int(seller.get("subscription_period_days") or 30)
    operations = get_subscription_operations(key, token, timezone_name)

    if not operations:
        return {
            "subscription_end": None,
            "subscription_next_payment": None,
        }

    latest = operations[0]
    raw_dt = latest.get("paidAt") or latest.get("updatedAt")
    if not raw_dt:
        return {
            "subscription_end": None,
            "subscription_next_payment": None,
        }

    try:
        start_dt = datetime.fromisoformat(str(raw_dt).replace("Z", "+00:00"))
    except ValueError:
        return {
            "subscription_end": None,
            "subscription_next_payment": None,
        }

    end_dt = start_dt + timedelta(days=period_days)
    end_date = end_dt.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y")

    amount = latest.get("amountRub")
    next_payment = {
        "date": end_date,
        "amount": amount,
    }

    print(
        f"Subscription details {key}: "
        f"start={start_dt.isoformat()!r}, "
        f"period_days={period_days}, "
        f"end={end_date!r}, "
        f"amount={amount!r}, "
        f"operation={latest.get('operationName')!r}",
        flush=True,
    )

    return {
        "subscription_end": end_date,
        "subscription_next_payment": next_payment,
    }


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
        "subscription_end": None,
        "subscription_next_payment": None,
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
            subscription = get_subscription_details(seller, token, timezone_name)
            result["subscription_end"] = subscription.get("subscription_end")
            result["subscription_next_payment"] = subscription.get(
                "subscription_next_payment"
            )
        except Exception as exc:
            print(
                f"Subscription details failed for {key}: {exc}",
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
                    "subscription_end": None,
                    "subscription_next_payment": None,
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
