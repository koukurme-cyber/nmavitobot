import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://api.avito.ru"
PROBE_VERSION = "v45"
MAX_PAGES = 10
BATCH_SIZE = 200


def _request_json(method, path, token=None, data=None, headers=None, timeout=25):
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

    request = urllib.request.Request(
        BASE_URL + path,
        data=body,
        headers=request_headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"raw": raw[:500]}
        return exc.code, payload


def _get_token(client_id, client_secret):
    status, payload = _request_json(
        "POST",
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status != 200 or not isinstance(payload, dict) or "access_token" not in payload:
        raise RuntimeError(f"token HTTP {status}: {payload}")
    return payload["access_token"]


def _numeric_values(values):
    result = []
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            result.append(int(value))
    return result


def _compact_unique(values, limit=40):
    uniq = sorted(set(_numeric_values(values)))
    if len(uniq) <= limit:
        return uniq
    return uniq[:limit] + [f"... +{len(uniq) - limit}"]


def _balance_v3(token):
    return _request_json(
        "POST",
        "/cpa/v3/balanceInfo",
        token=token,
        data={},
        headers={"X-Source": "nmavitobot-auction-probe"},
    )


def _auction_pages(token):
    all_items = []
    cursor = 0
    statuses = []

    for page_no in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode(
            {"fromItemID": cursor, "batchSize": BATCH_SIZE}
        )
        status, payload = _request_json(
            "GET",
            f"/auction/1/bids?{query}",
            token=token,
        )
        statuses.append(status)

        if status != 200:
            return statuses, all_items, payload

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return statuses, all_items, {
                "error": "unexpected payload",
                "payload": payload,
            }

        all_items.extend(items)

        if len(items) < BATCH_SIZE:
            return statuses, all_items, None

        last_id = items[-1].get("itemID") if items and isinstance(items[-1], dict) else None
        if not isinstance(last_id, int) or last_id <= cursor:
            return statuses, all_items, {
                "error": "pagination cursor did not advance",
                "last_id": last_id,
                "cursor": cursor,
            }
        cursor = last_id

    return statuses, all_items, {
        "warning": f"stopped after {MAX_PAGES} pages"
    }


def _summarize_auction(items):
    current_prices = []
    available_prices = []
    per_item_min_available = []
    per_item_max_available = []
    null_current = 0

    for item in items:
        if not isinstance(item, dict):
            continue

        current = item.get("pricePenny")
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            current_prices.append(int(current))
        else:
            null_current += 1

        available = item.get("availablePrices")
        if not isinstance(available, list):
            continue

        prices = []
        for option in available:
            if not isinstance(option, dict):
                continue
            price = option.get("pricePenny")
            if isinstance(price, (int, float)) and not isinstance(price, bool):
                value = int(price)
                prices.append(value)
                available_prices.append(value)

        if prices:
            per_item_min_available.append(min(prices))
            per_item_max_available.append(max(prices))

    def extrema(values):
        return {
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }

    return {
        "items": len(items),
        "current_count": len(current_prices),
        "current_null": null_current,
        "current_extrema": extrema(current_prices),
        "current_unique": _compact_unique(current_prices),
        "available_count": len(available_prices),
        "available_extrema": extrema(available_prices),
        "available_unique": _compact_unique(available_prices),
        "max_of_item_min_available": max(per_item_min_available) if per_item_min_available else None,
        "min_of_item_min_available": min(per_item_min_available) if per_item_min_available else None,
        "max_of_item_max_available": max(per_item_max_available) if per_item_max_available else None,
    }


def _extract_balance(payload):
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("balance"), (int, float)):
        return int(payload["balance"])
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("balance"), (int, float)):
        return int(result["balance"])
    return None


def _probe_account(seller):
    key = seller.get("key", "?")
    prefix = seller.get("env_prefix")
    if not prefix:
        print(f"Auction probe {PROBE_VERSION} {key}: missing env_prefix", flush=True)
        return

    client_id = os.getenv(prefix + "_CLIENT_ID")
    client_secret = os.getenv(prefix + "_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            f"Auction probe {PROBE_VERSION} {key}: "
            f"missing {prefix}_CLIENT_ID/CLIENT_SECRET",
            flush=True,
        )
        return

    try:
        token = _get_token(client_id, client_secret)
    except Exception as exc:
        print(f"Auction probe {PROBE_VERSION} {key}: token ERROR {exc}", flush=True)
        return

    balance_status, balance_payload = _balance_v3(token)
    balance = _extract_balance(balance_payload)
    print(
        f"Auction probe {PROBE_VERSION} {key}: "
        f"cpa_v3_status={balance_status}, balance={balance!r}, "
        f"cpa_v3_keys={list(balance_payload.keys()) if isinstance(balance_payload, dict) else []}",
        flush=True,
    )

    statuses, items, auction_error = _auction_pages(token)
    if not statuses or statuses[-1] != 200:
        print(
            f"Auction probe {PROBE_VERSION} {key}: "
            f"auction_statuses={statuses}, auction_error={auction_error}",
            flush=True,
        )
        return

    summary = _summarize_auction(items)
    print(
        f"Auction probe {PROBE_VERSION} {key}: "
        f"auction_statuses={statuses}, "
        f"items={summary['items']}, "
        f"current_count={summary['current_count']}, "
        f"current_null={summary['current_null']}, "
        f"current_min={summary['current_extrema']['min']!r}, "
        f"current_max={summary['current_extrema']['max']!r}, "
        f"max_item_min_available={summary['max_of_item_min_available']!r}, "
        f"min_item_min_available={summary['min_of_item_min_available']!r}, "
        f"max_item_max_available={summary['max_of_item_max_available']!r}, "
        f"auction_error={auction_error!r}",
        flush=True,
    )
    print(
        f"Auction probe {PROBE_VERSION} {key}: "
        f"current_unique={summary['current_unique']}, "
        f"available_unique={summary['available_unique']}",
        flush=True,
    )


def run_auction_probe():
    try:
        time.sleep(5)

        config_path = Path(__file__).with_name("config.json")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        sellers = [
            seller
            for seller in config.get("sellers", [])
            if seller.get("financial_mode") == "advance"
        ]

        print(
            f"Auction probe {PROBE_VERSION} started: "
            + ", ".join(seller.get("key", "?") for seller in sellers),
            flush=True,
        )

        for seller in sellers:
            _probe_account(seller)
            time.sleep(1)

        print(f"Auction probe {PROBE_VERSION} completed", flush=True)

    except Exception as exc:
        print(f"Auction probe {PROBE_VERSION} fatal ERROR: {exc}", flush=True)
