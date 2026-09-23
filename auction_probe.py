import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://api.avito.ru"
PROBE_VERSION = "v46"
MAX_ACTIVE_ITEMS = 200
DETAILS_PER_ACCOUNT = 4


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
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {"raw": raw[:500]}
            return int(response.status), payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"raw": raw[:500]}
        return int(exc.code), payload
    except Exception as exc:
        return None, {"error": str(exc)}


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


def _extract_balance(payload):
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("balance"), (int, float)):
        return int(payload["balance"])
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("balance"), (int, float)):
        return int(result["balance"])
    return None


def _balance_v3(token):
    return _request_json(
        "POST",
        "/cpa/v3/balanceInfo",
        token=token,
        data={},
        headers={"X-Source": "nmavitobot-cpxpromo-probe"},
    )


def _item_id(item):
    if not isinstance(item, dict):
        return None
    for key in ("id", "item_id", "itemId", "itemID"):
        value = item.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _active_item_ids(token):
    ids = []
    page = 1
    statuses = []

    while len(ids) < MAX_ACTIVE_ITEMS:
        query = urllib.parse.urlencode({
            "status": "active",
            "page": page,
            "per_page": 99,
        })
        status, payload = _request_json(
            "GET",
            f"/core/v1/items?{query}",
            token=token,
        )
        statuses.append(status)

        if status != 200 or not isinstance(payload, dict):
            return statuses, ids, payload

        resources = payload.get("resources")
        if not isinstance(resources, list):
            return statuses, ids, {
                "error": "unexpected items payload",
                "keys": list(payload.keys()),
            }

        for item in resources:
            value = _item_id(item)
            if value is not None:
                ids.append(value)
                if len(ids) >= MAX_ACTIVE_ITEMS:
                    break

        if len(resources) < 99:
            break

        page += 1
        if page > 10:
            break
        time.sleep(0.4)

    return statuses, ids, None


def _promotions(token, item_ids):
    if not item_ids:
        return None, {"items": []}

    return _request_json(
        "POST",
        "/cpxpromo/1/getPromotionsByItemIds",
        token=token,
        data={"itemIDs": item_ids[:200]},
    )


def _numeric_unique(values):
    result = []
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            result.append(int(value))
    return sorted(set(result))


def _summarize_promotions(payload):
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {
            "items": 0,
            "manual_count": 0,
            "auto_count": 0,
            "manual_bids": [],
            "records": [],
        }

    manual_bids = []
    records = []
    manual_count = 0
    auto_count = 0

    for row in items:
        if not isinstance(row, dict):
            continue

        item_id = _item_id(row)
        manual = row.get("manualPromotion")
        auto = row.get("autoPromotion")

        manual_bid = None
        if isinstance(manual, dict):
            manual_count += 1
            value = manual.get("bidPenny")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                manual_bid = int(value)
                manual_bids.append(manual_bid)

        if isinstance(auto, dict):
            auto_count += 1

        records.append({
            "item_id": item_id,
            "manual_bid": manual_bid,
            "action_type": row.get("actionTypeID"),
        })

    return {
        "items": len(items),
        "manual_count": manual_count,
        "auto_count": auto_count,
        "manual_bids": _numeric_unique(manual_bids),
        "records": records,
    }


def _select_detail_ids(active_ids, summary):
    records = [
        row for row in summary.get("records", [])
        if isinstance(row.get("item_id"), int)
    ]

    selected = []
    with_bid = [row for row in records if isinstance(row.get("manual_bid"), int)]

    if with_bid:
        ordered = sorted(with_bid, key=lambda row: row["manual_bid"])
        candidates = [ordered[-1], ordered[0]]
        if len(ordered) > 2:
            candidates += [ordered[len(ordered)//2]]
        candidates += list(reversed(ordered))
        for row in candidates:
            item_id = row["item_id"]
            if item_id not in selected:
                selected.append(item_id)
            if len(selected) >= DETAILS_PER_ACCOUNT:
                return selected

    for item_id in active_ids:
        if item_id not in selected:
            selected.append(item_id)
        if len(selected) >= DETAILS_PER_ACCOUNT:
            break

    return selected


def _detail(token, item_id):
    return _request_json(
        "GET",
        f"/cpxpromo/1/getBids/{item_id}",
        token=token,
    )


def _detail_summary(payload):
    if not isinstance(payload, dict):
        return {}

    manual = payload.get("manual")
    if not isinstance(manual, dict):
        manual = {}

    bids = manual.get("bids")
    bid_values = []
    if isinstance(bids, list):
        for bid in bids:
            if not isinstance(bid, dict):
                continue
            value = bid.get("valuePenny")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bid_values.append(int(value))

    return {
        "selected": payload.get("selectedType"),
        "action_type": payload.get("actionTypeID"),
        "manual_bid": manual.get("bidPenny"),
        "min_bid": manual.get("minBidPenny"),
        "rec_bid": manual.get("recBidPenny"),
        "max_bid": manual.get("maxBidPenny"),
        "bid_values": _numeric_unique(bid_values),
    }


def _probe_account(seller):
    key = seller.get("key", "?")
    prefix = seller.get("env_prefix")
    if not prefix:
        print(f"Cpx probe {PROBE_VERSION} {key}: missing env_prefix", flush=True)
        return

    client_id = os.getenv(prefix + "_CLIENT_ID")
    client_secret = os.getenv(prefix + "_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            f"Cpx probe {PROBE_VERSION} {key}: "
            f"missing {prefix}_CLIENT_ID/CLIENT_SECRET",
            flush=True,
        )
        return

    try:
        token = _get_token(client_id, client_secret)
    except Exception as exc:
        print(f"Cpx probe {PROBE_VERSION} {key}: token ERROR {exc}", flush=True)
        return

    balance_status, balance_payload = _balance_v3(token)
    print(
        f"Cpx probe {PROBE_VERSION} {key}: "
        f"cpa_v3_status={balance_status}, "
        f"balance={_extract_balance(balance_payload)!r}",
        flush=True,
    )

    item_statuses, active_ids, items_error = _active_item_ids(token)
    print(
        f"Cpx probe {PROBE_VERSION} {key}: "
        f"active_item_statuses={item_statuses}, "
        f"active_items={len(active_ids)}, "
        f"items_error={items_error!r}",
        flush=True,
    )
    if not active_ids:
        return

    promo_status, promo_payload = _promotions(token, active_ids)
    summary = _summarize_promotions(promo_payload)
    manual_bids = summary["manual_bids"]
    print(
        f"Cpx probe {PROBE_VERSION} {key}: "
        f"promotions_status={promo_status}, "
        f"promotion_items={summary['items']}, "
        f"manual_count={summary['manual_count']}, "
        f"auto_count={summary['auto_count']}, "
        f"manual_min={manual_bids[0] if manual_bids else None!r}, "
        f"manual_max={manual_bids[-1] if manual_bids else None!r}, "
        f"manual_unique={manual_bids[:30]}",
        flush=True,
    )

    for item_id in _select_detail_ids(active_ids, summary):
        status, payload = _detail(token, item_id)
        detail = _detail_summary(payload)
        print(
            f"Cpx probe {PROBE_VERSION} {key} item={item_id}: "
            f"status={status}, "
            f"selected={detail.get('selected')!r}, "
            f"actionTypeID={detail.get('action_type')!r}, "
            f"manual_bid={detail.get('manual_bid')!r}, "
            f"min_bid={detail.get('min_bid')!r}, "
            f"rec_bid={detail.get('rec_bid')!r}, "
            f"max_bid={detail.get('max_bid')!r}, "
            f"bid_values={detail.get('bid_values')!r}, "
            f"payload_keys={list(payload.keys()) if isinstance(payload, dict) else []}",
            flush=True,
        )
        time.sleep(0.6)


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
            f"Cpx probe {PROBE_VERSION} started: "
            + ", ".join(seller.get("key", "?") for seller in sellers),
            flush=True,
        )

        for seller in sellers:
            _probe_account(seller)
            time.sleep(1)

        print(f"Cpx probe {PROBE_VERSION} completed", flush=True)

    except Exception as exc:
        print(f"Cpx probe {PROBE_VERSION} fatal ERROR: {exc}", flush=True)
