"""Read-only DogAPI station balance and token usage adapter.

The station API is queried with a user-supplied API key.  The key is never
included in snapshots, logs, exceptions, or UI text.  DogAPI exposes two
different concepts: the account wallet balance and the current token's
accumulated usage/quota.  Keeping them separate avoids presenting a wallet
balance as per-request billing.
"""

from __future__ import annotations

import json
import math
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://www.dogapi.cc"
USAGE_PATH = "/api/usage/token/"
BALANCE_PATH = "/user/balance"
DAILY_STAT_PATH = "/api/log/self/stat"
ACCOUNT_LOG_PATH = "/api/log/self"
TOKEN_LOG_PATH = "/api/log/token/"
STATUS_PATH = "/api/status"
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
BILLING_REQUIRED = "配置账单访问令牌后可查询今日消耗。"


class StationFailure(RuntimeError):
    """A safe, user-facing station error without credential material."""

    def __init__(self, message: str, code: str = "station", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _network_failure(error: Exception) -> StationFailure:
    """Keep useful failure categories without exposing URLs or credentials."""
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return StationFailure('DogAPI 安全连接的证书校验失败。', 'tls_certificate')
    if isinstance(reason, ssl.SSLError):
        return StationFailure('DogAPI 安全连接建立失败。', 'tls', True)
    if isinstance(reason, FileNotFoundError):
        return StationFailure('额度查询组件初始化失败，请更新桌宠程序。', 'tls_runtime')
    if isinstance(reason, socket.gaierror):
        return StationFailure('DogAPI 域名解析失败，请检查网络连接。', 'dns', True)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return StationFailure('DogAPI 连接超时，请稍后重试。', 'timeout', True)
    if isinstance(reason, ConnectionRefusedError):
        return StationFailure('DogAPI 连接被拒绝，请检查网络或代理是否可用。', 'connection_refused', True)
    if isinstance(reason, PermissionError):
        return StationFailure('DogAPI 网络访问被系统拒绝。', 'network_permission')
    return StationFailure('DogAPI 网络连接失败，请检查网络或代理。', 'network', True)


def normalize_base_url(value: str | None) -> str:
    value = (value or DEFAULT_BASE_URL).strip()
    if not value:
        value = DEFAULT_BASE_URL
    if not value.startswith(("http://", "https://")):
        raise StationFailure("中转站地址必须以 http:// 或 https:// 开头。", "config")
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.username or parsed.password:
        raise StationFailure("请填写中转站网站地址，例如 https://www.dogapi.cc。", "config")
    path = parsed.path.rstrip("/")
    # Users commonly paste the dashboard or OpenAI-compatible API URL. The
    # balance endpoints live at the station root, outside either route.
    if path == "/v1" or path.startswith("/dashboard") or path in (USAGE_PATH.rstrip("/"), BALANCE_PATH):
        path = ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _json_request(base_url: str, path: str, api_key: str, timeout: float, extra_headers: dict | None = None) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": "BailongPet/0.5.1",
    }
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    if extra_headers:
        headers.update(extra_headers)
    request = Request(
        urljoin(base_url + "/", path.lstrip("/")),
        headers=headers,
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as error:
        if error.code in (401, 403):
            raise StationFailure("DogAPI API Key 无效或已失效。", "authentication") from None
        if error.code in (408, 429, 500, 502, 503, 504):
            raise StationFailure("DogAPI 暂时无法返回数据，请稍后重试。", "network", True) from None
        raise StationFailure(f"DogAPI 返回 HTTP {error.code}。", "service") from None
    except (TimeoutError, URLError, OSError) as error:
        raise _network_failure(error) from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StationFailure("DogAPI 返回了无法解析的数据。", "protocol") from None
    if not isinstance(payload, dict):
        raise StationFailure("DogAPI 返回格式不正确。", "protocol")
    if payload.get("code") is False or payload.get("success") is False:
        message = payload.get("message") or payload.get("error") or "DogAPI 查询失败。"
        message = str(message)
        if api_key:
            message = message.replace(api_key, "[已隐藏]")
        raise StationFailure(message[:180], "service")
    return payload


def _data(payload: dict) -> dict:
    value = payload.get("data", payload)
    return value if isinstance(value, dict) else {}


def _display(value, currency: str = "") -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _number(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _currency_id(value: str) -> str:
    aliases = {"¥": "CNY", "￥": "CNY", "RMB": "CNY", "$": "USD"}
    value = value.strip().upper()
    return aliases.get(value, value)


def _daily_conversion(base_url: str, usage: dict, currency: str, timeout: float) -> tuple[Decimal, str]:
    """Use an evidenced station currency ratio; never guess quota units."""
    raw_used = _number(usage.get("total_used"))
    shown_used = _number(usage.get("total_used_display"))
    if currency and raw_used is not None and raw_used > 0 and shown_used is not None and shown_used > 0:
        return shown_used / raw_used, currency
    status = _data(_json_request(base_url, STATUS_PATH, "", timeout))
    kind = str(status.get("quota_display_type") or "").upper()
    per_unit = _number(status.get("quota_per_unit"))
    if kind == "TOKENS":
        rate, target_currency = Decimal(1), "TOKENS"
    else:
        if per_unit is None or per_unit <= 0:
            raise StationFailure("站点未提供额度换算单位，暂不能换算今日消耗。", "conversion")
        if kind == "USD":
            rate, target_currency = Decimal(1) / per_unit, "$"
        elif kind in ("CNY", "CUSTOM"):
            exchange_key = "usd_exchange_rate" if kind == "CNY" else "custom_currency_exchange_rate"
            exchange = _number(status.get(exchange_key))
            target_currency = "¥" if kind == "CNY" else str(status.get("custom_currency_symbol") or "").strip()
            if exchange is None or exchange <= 0 or not target_currency:
                raise StationFailure("站点未提供货币换算配置，暂不能换算今日消耗。", "conversion")
            rate = exchange / per_unit
        else:
            raise StationFailure("站点未提供货币类型，暂不能换算今日消耗。", "conversion")
    if currency and _currency_id(currency) != _currency_id(target_currency):
        raise StationFailure("站点货币配置不一致，暂不能换算今日消耗。", "conversion")
    return rate, currency or target_currency


def _today_failure(error: StationFailure) -> str:
    """Map billing errors to fixed text, never a raw upstream message."""
    lower = str(error).lower()
    if error.code in ("conversion", "billing_config"):
        return str(error)
    if error.code == "authentication" or any(term in lower for term in ("unauthorized", "access token", "permission", "forbidden", "user id", "new-api-user", "权限", "访问令牌", "未登录", "用户id", "用户 id")):
        return "今日消耗查询未获授权，请检查账单访问令牌与账号 ID。"
    network_messages = {
        "tls_certificate": "安全连接证书校验失败。",
        "tls": "安全连接建立失败。",
        "tls_runtime": "查询组件初始化失败，请更新桌宠程序。",
        "dns": "域名解析失败，请检查网络。",
        "timeout": "连接超时，请稍后重试。",
        "connection_refused": "连接被拒绝，请检查网络或代理。",
        "network_permission": "网络访问被系统拒绝。",
        "network": "网络连接失败，请稍后重试。",
    }
    if error.code in network_messages:
        return "今日消耗暂时无法查询：" + network_messages[error.code]
    if error.code == "protocol":
        return "账单接口返回格式不正确，暂时无法查询今日消耗。"
    return "今日消耗暂时查询失败，请稍后重试。"


def _billing_headers(account_id: str) -> dict:
    account_id = str(account_id or "").strip()
    if account_id and (not account_id.isascii() or not account_id.isdecimal() or int(account_id) <= 0):
        raise StationFailure("账号 ID 应填写 DogAPI 个人资料中的数字 User ID。", "billing_config")
    return {"New-Api-User": account_id} if account_id else {}


def _fetch_today(base_url: str, usage: dict, snapshot: dict, billing_access_token: str, account_id: str, timeout: float) -> None:
    headers = _billing_headers(account_id)
    checked = snapshot["checkedAt"]
    midnight = datetime.fromtimestamp(checked, SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
    query = urlencode({"type": 2, "start_timestamp": int(midnight.timestamp()), "end_timestamp": int(checked)})
    payload = _json_request(base_url, DAILY_STAT_PATH + "?" + query, billing_access_token, timeout, extra_headers=headers)
    quota = _number(_data(payload).get("quota"))
    if quota is None or quota < 0:
        raise StationFailure("账单接口没有返回有效的消费汇总。", "protocol")
    rate, currency = _daily_conversion(base_url, usage, snapshot["currency"], timeout)
    try:
        amount = (quota * rate).quantize(Decimal("0.000001"))
    except InvalidOperation:
        raise StationFailure("账单消费汇总无法换算。", "protocol") from None
    snapshot["todayUsed"] = format(amount, "f").rstrip("0").rstrip(".") if amount else "0"
    snapshot["todayError"] = None
    snapshot["currency"] = currency


def _milliseconds(value, multiplier: int = 1) -> int | float | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    try:
        result = float(number * multiplier)
    except (ValueError, OverflowError, InvalidOperation):
        return None
    if not math.isfinite(result):
        return None
    return int(result) if result.is_integer() else result


def parse_recent_latency(payload: dict, source: str) -> dict:
    """Read the latest completed consumption call, never query RTT or a mean.

    DogAPI stores stream first-token latency in ``other.frt`` milliseconds;
    ``use_time`` is total duration in seconds. The token endpoint truncates
    older records, which does not affect selecting its most recent call.
    """
    result = {"latencyFirstTokenMs": None, "latencyTotalMs": None,
              "latencySource": None, "latencyCheckedAt": None}
    rows = payload.get("data")
    if isinstance(rows, dict):
        rows = rows.get("items")
    if not isinstance(rows, list):
        raise StationFailure("日志接口没有返回调用记录列表。", "protocol")
    candidates = [(index, row) for index, row in enumerate(rows)
                  if isinstance(row, dict) and _number(row.get("type")) == 2]
    if not candidates:
        return result
    def recent_key(item):
        index, row = item
        created = _number(row.get("created_at"))
        # Both endpoints already order newest first. Keep their order for
        # equal-second records; exposed ids may be remapped display ids.
        return (created if created is not None else Decimal(-1), -index)
    _, row = max(candidates, key=recent_key)
    other = row.get("other")
    if isinstance(other, str):
        try:
            other = json.loads(other)
        except (ValueError, TypeError):
            other = None
    if not isinstance(other, dict):
        other = {}
    if row.get("is_stream") is True or row.get("is_stream") == 1:
        result["latencyFirstTokenMs"] = _milliseconds(other.get("frt"))
    result["latencyTotalMs"] = _milliseconds(row.get("use_time"), 1000)
    result["latencySource"] = source
    result["latencyCheckedAt"] = _milliseconds(row.get("created_at"))
    return result


def _fetch_latency(base_url: str, api_key: str, billing_access_token: str, account_id: str, timeout: float) -> dict:
    if billing_access_token:
        try:
            query = urlencode({"p": 1, "page_size": 5, "type": 2})
            payload = _json_request(base_url, ACCOUNT_LOG_PATH + "?" + query, billing_access_token, timeout,
                                    extra_headers=_billing_headers(account_id))
            return parse_recent_latency(payload, "account_log")
        except Exception:
            # Recent-call telemetry is optional. If dashboard permissions are
            # unavailable, the relay key can still read its own recent calls.
            pass
    payload = _json_request(base_url, TOKEN_LOG_PATH, api_key, timeout)
    return parse_recent_latency(payload, "token_log")


def parse_station_payload(usage_payload: dict, balance_payload: dict, checked_at: float | None = None) -> dict:
    """Normalize the documented DogAPI responses into a UI-safe snapshot."""

    usage = _data(usage_payload)
    balance = _data(balance_payload)
    infos = balance.get("balance_infos")
    wallet = infos[0] if isinstance(infos, list) and infos and isinstance(infos[0], dict) else {}
    currency = str(usage.get("currency") or wallet.get("currency") or "")
    used = usage.get("total_used_display")
    if used is None:
        used = usage.get("total_used")
    available = None if usage.get("unlimited_quota") else usage.get("total_available_display")
    if available is None and not usage.get("unlimited_quota"):
        available = usage.get("total_available")
    wallet_balance = usage.get("user_balance_display")
    if wallet_balance is None:
        wallet_balance = wallet.get("total_balance")
    checked = checked_at if checked_at is not None else time.time()
    expires_at = usage.get("expires_at")
    try:
        expires_at = float(expires_at) if expires_at else None
    except (TypeError, ValueError):
        expires_at = None
    return {
        "ok": True,
        "provider": "DogAPI",
        "name": str(usage.get("name") or "当前 API Key"),
        "currency": currency,
        "walletBalance": _display(wallet_balance, currency),
        "tokenUsed": _display(used, currency),
        "tokenAvailable": _display(available, currency),
        "tokenGranted": _display(usage.get("total_granted_display"), currency),
        "unlimitedQuota": bool(usage.get("unlimited_quota")),
        "expiresAt": expires_at,
        "checkedAt": checked,
        "checkedAtText": datetime.fromtimestamp(checked, timezone.utc).astimezone().strftime("%H:%M:%S"),
        "todayUsed": None,
        "todayError": BILLING_REQUIRED,
        "todayDate": datetime.fromtimestamp(checked, SHANGHAI).strftime("%Y-%m-%d"),
        "latencyFirstTokenMs": None,
        "latencyTotalMs": None,
        "latencySource": None,
        "latencyCheckedAt": None,
    }


def fetch_station(api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 8, billing_access_token: str = "", account_id: str = "") -> dict:
    """Fetch wallet balance and token usage with a read-only API key."""

    api_key = (api_key or "").strip()
    if not api_key:
        raise StationFailure("尚未配置 DogAPI API Key。", "config")
    base_url = normalize_base_url(base_url)
    usage = _json_request(base_url, USAGE_PATH, api_key, timeout)
    try:
        balance = _json_request(base_url, BALANCE_PATH, api_key, timeout)
    except StationFailure:
        # Token usage also carries the account balance on DogAPI. A failure
        # of the supplementary wallet endpoint must not discard valid data.
        data = _data(usage)
        if data.get("user_balance_display") is None:
            raise
        balance = {}
    snapshot = parse_station_payload(usage, balance)
    billing_access_token = (billing_access_token or "").strip()
    if billing_access_token.lower().startswith("bearer "):
        billing_access_token = billing_access_token[7:].strip()
    if billing_access_token:
        try:
            _fetch_today(base_url, _data(usage), snapshot, billing_access_token, account_id, timeout)
        except StationFailure as error:
            snapshot["todayError"] = _today_failure(error)
        except (ValueError, UnicodeError, InvalidOperation, OverflowError):
            snapshot["todayError"] = "账单查询配置或返回数据格式不正确，请检查访问令牌与账号 ID。"
    try:
        snapshot.update(_fetch_latency(base_url, api_key, billing_access_token, account_id, timeout))
    except Exception:
        # A failed log request must not discard a valid wallet/daily bill or
        # expose upstream log text, credentials, or proxy diagnostics.
        pass
    return snapshot


__all__ = [
    "BALANCE_PATH",
    "DAILY_STAT_PATH",
    "ACCOUNT_LOG_PATH",
    "TOKEN_LOG_PATH",
    "DEFAULT_BASE_URL",
    "StationFailure",
    "USAGE_PATH",
    "fetch_station",
    "normalize_base_url",
    "parse_station_payload",
    "parse_recent_latency",
]
