"""Shared HTTP session with retries and a browser-like user agent."""
from __future__ import annotations

import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
TIMEOUT = 30
CONNECT_TIMEOUT = 10


class DeadlineExceeded(RuntimeError):
    pass

_local = threading.local()


def session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        retry = Retry(
            total=2,
            connect=1,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            respect_retry_after_header=True,
        )
        s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=10))
        s.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=10))
        s.headers.update(
            {
                "User-Agent": UA,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            }
        )
        _local.s = s
    return s


def set_deadline(ts: float | None) -> None:
    """Per-thread wall-clock limit (time.time() value); requests after it raise DeadlineExceeded."""
    _local.deadline = ts


def remaining() -> float | None:
    d = getattr(_local, "deadline", None)
    return None if d is None else d - time.time()


def check_deadline() -> None:
    left = remaining()
    if left is not None and left <= 0:
        raise DeadlineExceeded("time limit for this company reached")


def _timeout(kw: dict) -> None:
    check_deadline()
    left = remaining()
    read = TIMEOUT if left is None else max(3.0, min(TIMEOUT, left))
    kw.setdefault("timeout", (min(CONNECT_TIMEOUT, read), read))


def get(url: str, **kw) -> requests.Response:
    _timeout(kw)
    return session().get(url, **kw)


def post(url: str, **kw) -> requests.Response:
    _timeout(kw)
    return session().post(url, **kw)


def get_json(url: str, **kw):
    r = get(url, headers={"Accept": "application/json"}, **kw)
    r.raise_for_status()
    return r.json()


def polite_sleep(seconds: float = 0.4) -> None:
    time.sleep(seconds)
