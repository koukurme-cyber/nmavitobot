import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_URL = "https://api.avito.ru"
PROBE_VERSION = "v48"
TIMEZONE = "Europe/Moscow"
KNOWN_THRESHOLDS_KOPEKS = {
    "aggregaty": 89930,
    "avmex": 5925,
}

CPA_WORDS = (
    "cpa",
    "целев",
    "просмотр",
    "клик",
    "контакт",
    "звон",
    "чат",
    "пакет",
)


def _request_json(method, path, token=None, data=None, headers=None, timeout=25):
    request_headers = {"Accept": "application/json"}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    if headers:
        request_headers.update(headers)

    body = None
    if data is not None:
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
    form = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL + "/token",
        data=form,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["access_token"]


def _balance_v3(token):
    return _request_json(
        "POST",
        "/cpa/v3/balanceInfo",
        token=token,
        data={},
        headers={"X-Source": "nmavitobot-operations-probe"},
    )


def _extract_balance(payload):
    if not isinstance(payload, dict):
        return None
    value = payload.get("balance")
    if isinstance(value, (int, float)):
        return int(value)
    result = payload.get("result")
    if isinstance(result, dict):
        value = result.get("balance")
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _operations(token, start_dt, end_dt):
    return _request_json(
        "POST",
        "/core/v1/accounts/operations_history/",
        token=token,
        data={
            "dateTimeFrom": start_dt.isoformat(timespec="seconds"),
            "dateTimeTo": end_dt.isoformat(timespec="seconds"),
        },
    )


def _extract_operations(payload):
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict):
        operations = result.get("operations")
        if isinstance(operations, list):
            return operations
    operations = payload.get("operations")
    return operations if isinstance(operations, list) else []


def _op_text(op):
    return " ".join(
        str(op.get(key) or "")
        for key in (
            "serviceType",
            "serviceName",
            "operationType",
            "operationName",
        )
    ).casefold()


def _is_cpa_like(op):
    text = _op_text(op)
    return any(word in text for word in CPA_WORDS)


def _amount(op):
    value = op.get("amountRub")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _group_key(op):
    return (
        str(op.get("serviceType") or ""),
        str(op.get("serviceName") or ""),
        str(op.get("operationType") or ""),
        str(op.get("operationName") or ""),
    )


def _compact_group(group):
    service_type, service_name, operation_type, operation_name = group
    return {
        "serviceType": service_type,
        "serviceName": service_name,
        "operationType": operation_type,
        "operationName": operation_name,
    }


def _probe_account(seller):
    key = seller.get("key", "?")
    prefix = seller.get("env_prefix")
    if not prefix:
        print(f"Operations probe {PROBE_VERSION} {key}: missing env_prefix", flush=True)
        return

    client_id = os.getenv(prefix + "_CLIENT_ID")
    client_secret = os.getenv(prefix + "_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(f"Operations probe {PROBE_VERSION} {key}: missing API credentials", flush=True)
        return

    try:
        token = _get_token(client_id, client_secret)
    except Exception as exc:
        print(f"Operations probe {PROBE_VERSION} {key}: token ERROR {exc}", flush=True)
        return

    balance_status, balance_payload = _balance_v3(token)
    balance = _extract_balance(balance_payload)
    threshold = KNOWN_THRESHOLDS_KOPEKS.get(key)

    print(
        f"Operations probe {PROBE_VERSION} {key}: "
        f"cpa_v3_status={balance_status}, balance_kopeks={balance!r}, "
        f"known_threshold_kopeks={threshold!r}",
        flush=True,
    )

    tz = ZoneInfo(TIMEZONE)
    end_dt = datetime.now(tz)
    start_dt = end_dt - timedelta(days=2)

    status, payload = _operations(token, start_dt, end_dt)
    operations = _extract_operations(payload)

    print(
        f"Operations probe {PROBE_VERSION} {key}: "
        f"history_status={status}, operations={len(operations)}, "
        f"payload_keys={list(payload.keys()) if isinstance(payload, dict) else []}",
        flush=True,
    )

    if status != 200 or not operations:
        return

    cpa_ops = [op for op in operations if isinstance(op, dict) and _is_cpa_like(op)]
    groups = Counter(_group_key(op) for op in operations if isinstance(op, dict))
    cpa_groups = Counter(_group_key(op) for op in cpa_ops)

    print(
        f"Operations probe {PROBE_VERSION} {key}: "
        f"cpa_like_operations={len(cpa_ops)}, "
        f"unique_groups={len(groups)}, cpa_groups={len(cpa_groups)}",
        flush=True,
    )

    grouped_amounts = defaultdict(list)
    for op in cpa_ops:
        amount = _amount(op)
        if amount is not None:
            grouped_amounts[_group_key(op)].append(amount)

    for group, count in cpa_groups.most_common(20):
        amounts = grouped_amounts.get(group, [])
        unique_amounts = sorted(set(round(x, 2) for x in amounts))
        print(
            f"Operations probe {PROBE_VERSION} {key} CPA group: "
            f"count={count}, fields={_compact_group(group)!r}, "
            f"amount_min={min(amounts) if amounts else None!r}, "
            f"amount_max={max(amounts) if amounts else None!r}, "
            f"amount_unique={unique_amounts[:25]!r}",
            flush=True,
        )

    if not cpa_groups:
        for group, count in groups.most_common(15):
            print(
                f"Operations probe {PROBE_VERSION} {key} top group: "
                f"count={count}, fields={_compact_group(group)!r}",
                flush=True,
            )

    recent_cpa = sorted(
        cpa_ops,
        key=lambda op: str(op.get("paidAt") or op.get("updatedAt") or ""),
        reverse=True,
    )[:20]

    for op in recent_cpa:
        print(
            f"Operations probe {PROBE_VERSION} {key} CPA op: "
            f"paidAt={op.get('paidAt')!r}, "
            f"updatedAt={op.get('updatedAt')!r}, "
            f"serviceType={op.get('serviceType')!r}, "
            f"serviceName={op.get('serviceName')!r}, "
            f"operationType={op.get('operationType')!r}, "
            f"amountRub={op.get('amountRub')!r}, "
            f"operationName={op.get('operationName')!r}",
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
            f"Operations probe {PROBE_VERSION} started: "
            + ", ".join(seller.get("key", "?") for seller in sellers),
            flush=True,
        )

        for seller in sellers:
            _probe_account(seller)
            time.sleep(1)

        print(f"Operations probe {PROBE_VERSION} completed", flush=True)

    except Exception as exc:
        print(f"Operations probe {PROBE_VERSION} fatal ERROR: {exc}", flush=True)
