import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import avito_provider as _ap


_ORIGINAL_GET_DIAG = _ap.get_cpa_web_profile_diag


def _cookie_env_name(account_key):
    return f"AVITO_{str(account_key).upper()}_WEB_COOKIE"


def _request_with_cookie(path, cookie, timeout=20):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/152.0.0.0 Safari/537.36"
        ),
        "Referer": _ap.WEB_BASE_URL + "/tariff/cpa/profile",
        "Cookie": cookie,
    }
    request = urllib.request.Request(
        _ap.WEB_BASE_URL + path,
        headers=headers,
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except Exception:
                data = None
            return int(response.status), data, raw[:500]
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = None
        return int(exc.code), data, raw[:500]
    except Exception as exc:
        return None, None, str(exc)[:500]


def _get_cpa_web_profile_diag_v44(account_key, token):
    cookie = os.getenv(_cookie_env_name(account_key)) or None

    # Без browser cookie сохраняем прежнюю Bearer-диагностику.
    if not cookie:
        value = _ORIGINAL_GET_DIAG(account_key, token)
        if isinstance(value, dict):
            value = dict(value)
            value["auth_mode"] = "bearer"
        return value

    cache_key = f"{account_key}:cookie"
    now = time.time()
    cached = _ap._cpa_web_profile_cache.get(cache_key)
    if cached and cached["expires_at"] > now:
        return cached["value"]

    path = "/web/3/tariff/cpa/profile?entryPoint=desktop_profile"
    status, data, raw_prefix = _request_with_cookie(path, cookie)

    result_obj = {}
    if isinstance(data, dict):
        candidate = data.get("result")
        if isinstance(candidate, dict):
            result_obj = candidate

    threshold = result_obj.get("advanceThreshold")
    balance = result_obj.get("advanceBalance")
    cpa_alert = result_obj.get("cpaAlert")

    # Cookie никогда не выводится в лог.
    print(
        f"CPA web threshold v44 {account_key}: "
        f"auth='cookie', "
        f"status={status!r}, "
        f"advanceThreshold={threshold!r}, "
        f"advanceBalance={balance!r}, "
        f"cpaAlert={cpa_alert!r}, "
        f"top_keys={list(data.keys()) if isinstance(data, dict) else []}, "
        f"result_keys={list(result_obj.keys())[:30]}, "
        f"raw_prefix={raw_prefix!r}",
        flush=True,
    )

    value = {
        "http_status": status,
        "auth_mode": "cookie",
        "advance_threshold": threshold,
        "advance_balance": balance,
        "cpa_alert": cpa_alert,
    }
    _ap._cpa_web_profile_cache[cache_key] = {
        "value": value,
        "expires_at": now + 60,
    }
    return value


def _run_cpa_threshold_probe_v44(config):
    sellers = [
        seller for seller in config.get("sellers", [])
        if seller.get("financial_mode", "advance") == "advance"
    ]

    print(
        "CPA threshold startup probe v44: "
        + ", ".join(seller.get("key", "?") for seller in sellers),
        flush=True,
    )

    def probe_one(seller):
        key = seller["key"]
        prefix = seller["env_prefix"]
        try:
            client_id = _ap.env(prefix + "_CLIENT_ID")
            client_secret = _ap.env(prefix + "_CLIENT_SECRET")
            token = _ap.get_token(key, client_id, client_secret)
            return key, _get_cpa_web_profile_diag_v44(key, token), None
        except Exception as exc:
            return key, None, str(exc)

    workers = max(1, min(len(sellers), 3))
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="cpa-threshold-probe",
    ) as pool:
        futures = [pool.submit(probe_one, seller) for seller in sellers]
        for future in as_completed(futures):
            key, value, error = future.result()
            if error:
                print(
                    f"CPA threshold startup probe v44 {key}: ERROR {error}",
                    flush=True,
                )
            else:
                print(
                    f"CPA threshold startup probe v44 {key}: "
                    f"auth={value.get('auth_mode')!r}, "
                    f"status={value.get('http_status')!r}, "
                    f"advanceThreshold={value.get('advance_threshold')!r}, "
                    f"advanceBalance={value.get('advance_balance')!r}, "
                    f"cpaAlert={value.get('cpa_alert')!r}",
                    flush=True,
                )


_ap.get_cpa_web_profile_diag = _get_cpa_web_profile_diag_v44
_ap.run_cpa_threshold_probe = _run_cpa_threshold_probe_v44
