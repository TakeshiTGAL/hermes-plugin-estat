"""HTTP client for the e-Stat API v3 (https://api.e-stat.go.jp/rest/3.0/app/json/).

Standard library only. The application ID is read from ESTAT_APP_ID on every
request and is sent to api.e-stat.go.jp only. e-Stat puts the ID in the query
string, so nothing in this module ever puts a request URL into an exception,
a log line, or a tool result.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

API_HOST = "api.e-stat.go.jp"
API_BASE = f"https://{API_HOST}/rest/3.0/app/json/"
APP_ID_ENV = "ESTAT_APP_ID"
SIGNUP_URL = "https://www.e-stat.go.jp/mypage/user/preregister"
GUIDE_URL = "https://www.e-stat.go.jp/api/api-info/api-guide"
USER_AGENT = "hermes-plugin-estat/0.1.0"
TIMEOUT_SECONDS = 20          # one HTTP request
CALL_BUDGET_SECONDS = 45      # one tool call, all of its requests together
RETRY_DELAY_SECONDS = 1.5

APP_ID_STEPS = (
    "How to get a free e-Stat application ID: "
    f"1) register an e-Stat account at {SIGNUP_URL}; "
    "2) log in, open My Page (マイページ) -> API機能（アプリケーションID発行）, enter any name and "
    "the URL http://test.localhost/ (allowed for non-public use), and press 発行; "
    f"3) run `hermes plugins install` again and paste the ID when asked for {APP_ID_ENV}, or add the line "
    f"{APP_ID_ENV}=<your ID> to the .env file in your Hermes home, then start a new session. "
    f"Guide: {GUIDE_URL}"
)


class EstatError(Exception):
    """A request e-Stat rejected, or that never reached it. The message is safe to show."""

    def __init__(self, message: str, kind: str = "api_error", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class AppIdError(EstatError):
    def __init__(self, message: str):
        super().__init__(message, kind="app_id")


def _app_id() -> str:
    value = os.environ.get(APP_ID_ENV, "").strip()
    if not value:
        raise AppIdError(f"{APP_ID_ENV} is not set, so e-Stat cannot be queried. {APP_ID_STEPS}")
    return value


def _scrub(text: str, secret: str) -> str:
    """Remove URLs and the application ID from any text before it can leave this module."""
    text = re.sub(r"https?://\S+", "<request URL removed>", str(text))
    if secret:
        text = text.replace(secret, "***")
        text = text.replace(urllib.parse.quote(secret, safe=""), "***")
    return text


# --- time budget ----------------------------------------------------------------
# A tool call may make several requests. They share one deadline so that a slow
# e-Stat cannot hold an unattended run (cron, gateway) for minutes.

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("estat_deadline", default=None)


@contextlib.contextmanager
def budget(seconds: float = CALL_BUDGET_SECONDS):
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def _remaining() -> float | None:
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


# --- transport --------------------------------------------------------------------


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https://api.e-stat.go.jp: the query string carries the ID."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or parts.hostname != API_HOST:
            raise EstatError(
                "e-Stat redirected the request to another host or to plain HTTP; the plugin refused to follow it "
                "so the application ID stays with e-Stat. Try again later.",
                kind="http_error",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameHostRedirects)


def _open(request, timeout):
    return _opener.open(request, timeout=timeout)


def _http_get(endpoint: str, params: dict, app_id: str) -> dict:
    query = urllib.parse.urlencode({"appId": app_id, **params})
    request = urllib.request.Request(
        API_BASE + endpoint + "?" + query,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    timeout = TIMEOUT_SECONDS
    remaining = _remaining()
    if remaining is not None:
        if remaining < 1:
            raise _out_of_time()
        timeout = min(timeout, remaining)
    try:
        with _open(request, timeout) as response:
            raw = response.read()
    except EstatError:
        raise
    except urllib.error.HTTPError as error:
        # HTTPError carries the full URL (with the ID); only the code is kept.
        code = error.code
        raise EstatError(
            f"e-Stat answered HTTP {code}. Try again in a minute; if it keeps failing, "
            "the service may be under maintenance (https://www.e-stat.go.jp/).",
            kind="http_error",
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        if _remaining() is not None and _remaining() < 1:
            raise _out_of_time() from None
        reason = _scrub(getattr(error, "reason", error), app_id)
        raise EstatError(
            f"Could not reach api.e-stat.go.jp ({reason}). Check the network connection and try again.",
            kind="network_error",
        ) from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EstatError(
            "e-Stat returned a response that is not JSON (often a maintenance page). Try again later.",
            kind="bad_response",
        ) from None


def _out_of_time() -> EstatError:
    return EstatError(
        f"e-Stat did not answer within {CALL_BUDGET_SECONDS} seconds. It is probably busy; try again in a "
        "few minutes, or ask for fewer rows (a single area or period).",
        kind="timeout",
    )


def _can_retry() -> bool:
    remaining = _remaining()
    return remaining is None or remaining > RETRY_DELAY_SECONDS + 5


def call(endpoint: str, params: dict) -> dict:
    """Call one e-Stat endpoint and return the body under its root key.

    Raises AppIdError for a missing/invalid ID and EstatError for every other
    failure. STATUS 1 (no matching data) is returned, not raised.
    """
    app_id = _app_id()
    params = {k: v for k, v in params.items() if v not in (None, "")}
    last_error = None
    for attempt in range(2):
        try:
            body = _http_get(endpoint, params, app_id)
        except EstatError as error:
            last_error = error
            if attempt == 0 and error.kind in ("network_error", "http_error", "bad_response") and _can_retry():
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            raise
        root = next(iter(body.values())) if isinstance(body, dict) and body else {}
        result = root.get("RESULT", {}) if isinstance(root, dict) else {}
        status = int(result.get("STATUS", 0))
        message = _scrub(result.get("ERROR_MSG", ""), app_id)
        if status in (0, 1, 2):
            return root
        if status == 100:
            raise AppIdError(
                f"e-Stat rejected the application ID in {APP_ID_ENV} (status 100: {message}). "
                f"Check that the whole ID was copied. {APP_ID_STEPS}"
            )
        if 200 <= status < 300 and attempt == 0 and _can_retry():
            last_error = EstatError(message, status=status)
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        kind = "not_found" if status == 300 else "service_error" if 200 <= status < 300 else "bad_request"
        raise EstatError(f"e-Stat status {status}: {message}", kind=kind, status=status)
    raise last_error or EstatError("e-Stat request failed.")


# --- metadata cache -----------------------------------------------------------
# Table metadata is large (CPI: ~150 KB) and every estat_get_data call needs it to
# translate names into codes. Keep the last few tables in memory for 30 minutes.

_META_TTL_SECONDS = 1800
_META_MAX = 16
_meta_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_meta_lock = threading.Lock()


def get_meta(table_id: str) -> dict:
    now = time.monotonic()
    with _meta_lock:
        hit = _meta_cache.get(table_id)
        if hit and now - hit[0] < _META_TTL_SECONDS:
            _meta_cache.move_to_end(table_id)
            return hit[1]
    root = call("getMetaInfo", {"statsDataId": table_id, "explanationGetFlg": "N"})
    with _meta_lock:
        _meta_cache[table_id] = (now, root)
        _meta_cache.move_to_end(table_id)
        while len(_meta_cache) > _META_MAX:
            _meta_cache.popitem(last=False)
    return root


def clear_cache() -> None:
    with _meta_lock:
        _meta_cache.clear()
