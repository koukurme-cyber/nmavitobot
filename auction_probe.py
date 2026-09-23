import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

BASE_URL = "https://api.avito.ru"
PROBE_VERSION = "v47"
KNOWN_THRESHOLDS_KOPEKS = {
    "aggregaty": 89930,
    "avmex": 5925,
}


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


def _profile(token):
    return _request_json("GET", "/core/v1/accounts/self", token=token)


def _balance_v3(token):
    return _request_json(
        "POST",
        "/cpa/v3/balanceInfo",
        token=token,
        data={},
        headers={"X-Source": "nmavitobot-spendings-probe"},
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


def _extract_user_id(profile):
    if not isinstance(profile, dict):
        return None
    for key in ("id", "user_id", "userId"):
        value = profile.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _spendings(token, user_id, date_from, date_to):
    return _request_json(
        "POST",
        f"/stats/v2/accounts/{user_id}/spendings",
        token=token,
        data={
            "dateFrom": date_from,
            "dateTo": date_to,
            "grouping": "day",
            "spendingTypes": ["presence"],
        },
        headers={"X-AgencyClientId": str(user_id)},
    )


def _metrics(token, user_id, date_from, date_to):
    return _request_json(
        "POST",
        f"/stats/v2/accounts/{user_id}/items",
        token=token,
        data={
            "dateFrom": date_from,
            "dateTo": date_to,
            "grouping": "day",
            "metrics": [
                "clickPackages",
                "presenceSpending",
                "activeItems",
                "views",
                "impressions",
            ],
        },
        headers={"X-AgencyClientId": str(user_id)},
    )


def _daily_spendings(payload):
    result = payload.get("result") if isinstance(payload, dict) else None
    groupings = result.get("groupings") if isinstance(result, dict) else None
    if not isinstance(groupings, list):
        return []

    rows = []
    for group in groupings:
        if not isinstance(group, dict):
            continue
        row = {
            "date": group.get("date"),
            "presence": 0.0,
            "cpa_click_package": 0.0,
            "cpa_target_call": 0.0,
            "cpa_target_chat": 0.0,
        }
        spendings = group.get("spendings")
        if isinstance(spendings, list):
            for spending in spendings:
                if not isinstance(spending, dict):
                    continue
                if spending.get("slug") == "presence":
                    try:
                        row["presence"] = float(spending.get("value") or 0)
                    except (TypeError, ValueError):
                        pass
                services = spending.get("services")
                if isinstance(services, list):
                    for service in services:
                        if not isinstance(service, dict):
                            continue
                        slug = service.get("slug")
                        if slug in row:
                            try:
                                row[slug] += float(service.get("value") or 0)
                            except (TypeError, ValueError):
                                pass
        rows.append(row)
    return rows


def _daily_metrics(payload):
    result = payload.get("result") if isinstance(payload, dict) else None
    groupings = result.get("groupings") if isinstance(result, dict) else None
    if not isinstance(groupings, list):
        return []

    rows = []
    for group in groupings:
        if not isinstance(group, dict):
            continue
        values = {"id": group.get("id")}
        metrics = group.get("metrics")
        if isinstance(metrics, list):
            for metric in metrics:
                if isinstance(metric, dict):
                    values[metric.get("slug")] = metric.get("value")
        rows.append(values)
    return rows


def _sum_last(rows, key, days):
    values = rows[-days:] if len(rows) >= days else rows
    total = 0.0
    for row in values:
        try:
            total += float(row.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return round(total, 2)


def _probe_account(seller):
    key = seller.get("key", "?")
    prefix = seller.get("env_prefix")
    if not prefix:
        print(f"Spendings probe {PROBE_VERSION} {key}: missing env_prefix", flush=True)
        return

    client_id = os.getenv(prefix + "_CLIENT_ID")
    client_secret = os.getenv(prefix + "_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            f"Spendings probe {PROBE_VERSION} {key}: missing API credentials",
            flush=True,
        )
        return

    try:
        token = _get_token(client_id, client_secret)
    except Exception as exc:
        print(f"Spendings probe {PROBE_VERSION} {key}: token ERROR {exc}", flush=True)
        return

    profile_status, profile_payload = _profile(token)
    user_id = _extract_user_id(profile_payload)
    balance_status, balance_payload = _balance_v3(token)
    balance = _extract_balance(balance_payload)

    print(
        f"Spendings probe {PROBE_VERSION} {key}: "
        f"profile_status={profile_status}, user_id={user_id!r}, "
        f"cpa_v3_status={balance_status}, balance_kopeks={balance!r}",
        flush=True,
    )

    if not user_id:
        return

    today = date.today()
    date_from = (today - timedelta(days=13)).isoformat()
    date_to = today.isoformat()

    spend_status, spend_payload = _spendings(
        token, user_id, date_from, date_to
    )
    rows = _daily_spendings(spend_payload)

    print(
        f"Spendings probe {PROBE_VERSION} {key}: "
        f"spendings_status={spend_status}, "
        f"days={len(rows)}, "
        f"payload_keys={list(spend_payload.keys()) if isinstance(spend_payload, dict) else []}",
        flush=True,
    )

    if rows:
        compact = [
            {
                "date": row["date"],
                "presence": row["presence"],
                "click": row["cpa_click_package"],
                "call": row["cpa_target_call"],
                "chat": row["cpa_target_chat"],
            }
            for row in rows
        ]
        print(
            f"Spendings probe {PROBE_VERSION} {key}: daily={compact}",
            flush=True,
        )
        print(
            f"Spendings probe {PROBE_VERSION} {key}: "
            f"click_1d={_sum_last(rows, 'cpa_click_package', 1)}, "
            f"click_3d={_sum_last(rows, 'cpa_click_package', 3)}, "
            f"click_7d={_sum_last(rows, 'cpa_click_package', 7)}, "
            f"presence_1d={_sum_last(rows, 'presence', 1)}, "
            f"presence_3d={_sum_last(rows, 'presence', 3)}, "
            f"presence_7d={_sum_last(rows, 'presence', 7)}",
            flush=True,
        )

    threshold = KNOWN_THRESHOLDS_KOPEKS.get(key)
    if threshold is not None:
        print(
            f"Spendings probe {PROBE_VERSION} {key}: "
            f"known_browser_threshold={threshold} kopeks "
            f"({threshold / 100:.2f} rub)",
            flush=True,
        )

    metrics_status, metrics_payload = _metrics(
        token, user_id, date_from, date_to
    )
    metrics_rows = _daily_metrics(metrics_payload)
    print(
        f"Spendings probe {PROBE_VERSION} {key}: "
        f"metrics_status={metrics_status}, metrics_days={len(metrics_rows)}, "
        f"metrics={metrics_rows}",
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
            f"Spendings probe {PROBE_VERSION} started: "
            + ", ".join(seller.get("key", "?") for seller in sellers),
            flush=True,
        )

        for seller in sellers:
            _probe_account(seller)
            time.sleep(1)

        print(f"Spendings probe {PROBE_VERSION} completed", flush=True)

    except Exception as exc:
        print(f"Spendings probe {PROBE_VERSION} fatal ERROR: {exc}", flush=True)
