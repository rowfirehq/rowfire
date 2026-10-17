"""Reaching Rowfire through a proxy, as most production networks require."""

from __future__ import annotations

import select
import socket
import threading

import rowfire_sdk as rowfire
from rowfire_sdk._transport import resolve_proxy

CHECKOUT = {"id": "checkout", "operation": "POST /checkout", "fields": ["params.n"]}


class ConnectProxy:
    """A minimal CONNECT proxy that records which hosts it tunnelled to."""

    def __init__(self) -> None:
        self.tunnels: list[str] = []
        self.auth: list[str | None] = []
        self._server = socket.create_server(("127.0.0.1", 0))
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://user:secret@127.0.0.1:{self._server.getsockname()[1]}"

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._tunnel, args=(client,), daemon=True).start()

    def _tunnel(self, client: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            head += client.recv(4096)
        lines = head.decode().split("\r\n")
        target = lines[0].split()[1]
        self.tunnels.append(target)
        auth = [ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("proxy-auth")]
        self.auth.append(auth[0] if auth else None)
        host, port = target.rsplit(":", 1)
        upstream = socket.create_connection((host, int(port)))
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        sockets = [client, upstream]
        while True:
            readable, _, _ = select.select(sockets, [], [], 5)
            if not readable:
                break
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    client.close()
                    upstream.close()
                    return
                (upstream if sock is client else client).sendall(data)

    def close(self) -> None:
        self._server.close()


def test_events_go_through_the_proxy_it_is_given(start, ingest):
    proxy = ConnectProxy()
    try:
        start([CHECKOUT], catalog=False, proxy=proxy.url)
        with rowfire.operation("POST /checkout", params={"n": 1}):
            pass
        assert rowfire.flush()
        assert [e["data"] for e in ingest.events] == [{"params.n": 1}]
        host, port = ingest.url.removeprefix("http://").split(":")
        assert proxy.tunnels == [f"{host}:{port}"]
        assert proxy.auth == ["Basic dXNlcjpzZWNyZXQ="]  # user:secret
    finally:
        proxy.close()


def test_the_proxy_comes_from_the_environment(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "rowfire.internal,localhost")
    assert resolve_proxy(True, "https", "api.rowfire.com") == ("proxy.internal", 3128, {})
    assert resolve_proxy(True, "https", "rowfire.internal") is None  # listed in NO_PROXY
    assert resolve_proxy(True, "http", "api.rowfire.com") is None  # no HTTP_PROXY set
    assert resolve_proxy(False, "https", "api.rowfire.com") is None
