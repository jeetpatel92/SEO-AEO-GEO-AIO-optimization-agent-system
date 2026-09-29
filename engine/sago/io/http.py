"""A small, paced HTTP client (stdlib + certifi) that maps failures to typed errors.

Pacing is per host and global to the process: the crawler, the verifier and the
CMS client all share it, so the system never bursts the site no matter how many
agents ask at once.
"""
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import certifi

from ..errors import AuthError, RateLimited, TransientError

_CTX = ssl.create_default_context(cafile=certifi.where())
_LAST: dict[str, float] = {}
_LOCK = threading.Lock()


@dataclass
class Response:
    status: int
    url: str
    headers: dict
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.body or b"null")


def _pace(host: str, min_interval: float) -> None:
    with _LOCK:
        last = _LAST.get(host, 0.0)
        wait = last + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST[host] = time.monotonic()


def request(method: str, url: str, *, headers: dict | None = None, data: bytes | None = None,
            json_body=None, timeout: float = 40, min_interval: float = 0.0, adapter: str = "http",
            follow_redirects: bool = True) -> Response:
    host = urllib.parse.urlsplit(url).netloc
    if min_interval:
        _pace(host, min_interval)
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX))
    if not follow_redirects:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX), NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return Response(r.status, r.geturl(), dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        resp = Response(e.code, url, dict(e.headers or {}), body)
        if e.code in (301, 302, 307, 308) and not follow_redirects:
            return resp
        if e.code == 429:
            ra = e.headers.get("Retry-After") if e.headers else None
            raise RateLimited(f"{adapter}: 429 from {host}", float(ra) if ra and ra.isdigit() else None) from e
        if e.code in (401, 403) and adapter != "http":
            raise AuthError(adapter, f"{e.code} from {host}: {body[:200]!r}") from e
        if e.code >= 500:
            raise TransientError(f"{adapter}: {e.code} from {host}") from e
        return resp
    except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as e:
        raise TransientError(f"{adapter}: {type(e).__name__} reaching {host}: {e}") from e
