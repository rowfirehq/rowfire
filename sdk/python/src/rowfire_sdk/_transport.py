"""HTTP to Rowfire: one kept-alive connection, used only by the background thread.

Each response is sorted into one of three outcomes, because the worker treats
them differently:

- ok: delivered.
- retry: Rowfire did not take it but might later (network error, timeout, 408,
  429, 5xx). The batch is kept and resent, after `Retry-After` when given.
- reject: Rowfire will never take this body (any other 4xx). Resending it would
  fail forever and hold up everything behind it, so it is dropped and counted.

Sentry drops on "retry" as well. For telemetry that is right; for events that
open tickets it is not.
"""

from __future__ import annotations

import http.client
import json
import ssl
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from rowfire_sdk._version import VERSION

OK = "ok"
RETRY = "retry"
REJECT = "reject"


@dataclass(frozen=True)
class Outcome:
    kind: str
    reason: str = ""
    retry_after: float | None = None


class Transport:
    def __init__(self, endpoint: str, api_key: str, timeout: float = 5.0) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"endpoint must be an http(s) URL, got {endpoint!r}")
        self._https = parts.scheme == "https"
        self._host = parts.hostname
        self._port = parts.port
        self._base = parts.path.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._conn: http.client.HTTPConnection | None = None

    def send_batch(self, body: bytes, timeout: float | None = None) -> Outcome:
        try:
            status, headers, _ = self._request(
                "POST",
                "/v1/events",
                body,
                {"Content-Type": "application/json", "Content-Encoding": "gzip"},
                timeout,
            )
        except (OSError, http.client.HTTPException) as exc:
            return Outcome(RETRY, f"network: {type(exc).__name__}")
        if 200 <= status < 300:
            return Outcome(OK)
        if status in (408, 429) or status >= 500:
            return Outcome(RETRY, f"status_{status}", _retry_after(headers.get("retry-after")))
        return Outcome(REJECT, f"status_{status}")

    def fetch_config(self, etag: str | None) -> tuple[int, str | None, dict[str, Any] | None]:
        """(status, etag, config); config is None unless the status is 200."""
        headers = {"If-None-Match": etag} if etag else {}
        status, response_headers, data = self._request("GET", "/v1/config", None, headers, None)
        if status != 200:
            return status, etag, None
        return status, response_headers.get("etag"), json.loads(data)

    def abandon(self) -> None:
        """Forget the connection without closing it: after a fork its socket
        belongs to the parent, and closing it here would close it there."""
        self._conn = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float | None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {
            **headers,
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": f"rowfire-sdk-python/{VERSION}",
        }
        conn = self._connection()
        conn.timeout = timeout if timeout is not None else self._timeout
        if conn.sock is not None:
            conn.sock.settimeout(conn.timeout)
        try:
            conn.request(method, self._base + path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
        except BaseException:
            # A half-used connection cannot be trusted with the next request.
            self.close()
            raise
        if response.getheader("connection", "").lower() == "close":
            self.close()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, data

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            if self._https:
                self._conn = http.client.HTTPSConnection(
                    self._host,
                    self._port,
                    timeout=self._timeout,
                    context=ssl.create_default_context(),
                )
            else:
                self._conn = http.client.HTTPConnection(
                    self._host, self._port, timeout=self._timeout
                )
        return self._conn


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None
