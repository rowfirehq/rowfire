"""Where batches wait when Rowfire cannot take them yet.

A spool holds encoded batches, oldest first, up to a size cap; past it the
oldest batch is evicted and its events counted as lost. Only the background
thread touches it, so a full disk or a slow one never reaches a request.

`MemorySpool` is the default: it rides out a Rowfire outage but not a restart.
`DiskSpool` survives a restart too. Each file is named for the process that
wrote it, so several workers of one app (gunicorn, uWSGI) can share a
directory: a process sends its own files, and adopts those of a process that is
no longer running, by renaming them, which only one adopter can win.
"""

from __future__ import annotations

import itertools
import os
import time
from collections import deque
from typing import Any

Batch = tuple[Any, bytes, int]  # (handle, body, event count)


class MemorySpool:
    def __init__(self, max_bytes: int = 16 * 1024 * 1024) -> None:
        self._max_bytes = max_bytes
        self._batches: deque[tuple[int, bytes, int]] = deque()
        self._bytes = 0
        self._ids = itertools.count()

    def put(self, body: bytes, count: int) -> int:
        self._batches.append((next(self._ids), body, count))
        self._bytes += len(body)
        evicted = 0
        while self._bytes > self._max_bytes and len(self._batches) > 1:
            _, old, n = self._batches.popleft()
            self._bytes -= len(old)
            evicted += n
        return evicted

    def peek(self) -> Batch | None:
        return self._batches[0] if self._batches else None

    def remove(self, handle: Any) -> None:
        if self._batches and self._batches[0][0] == handle:
            _, body, _ = self._batches.popleft()
            self._bytes -= len(body)

    def pending(self) -> int:
        return sum(n for _, _, n in self._batches)

    def reset(self) -> None:
        self._batches.clear()
        self._bytes = 0


class DiskSpool:
    SUFFIX = ".batch"

    def __init__(self, directory: str, max_bytes: int = 64 * 1024 * 1024) -> None:
        self.directory = directory
        self._max_bytes = max_bytes
        self._seq = itertools.count()
        os.makedirs(directory, exist_ok=True)

    def put(self, body: bytes, count: int) -> int:
        name = f"{time.time_ns():020d}-{next(self._seq):08d}-{os.getpid()}-{count}{self.SUFFIX}"
        path = os.path.join(self.directory, name)
        tmp = os.path.join(self.directory, f".{name}.tmp")
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
        return self._enforce_cap()

    def peek(self) -> Batch | None:
        for name in self._names():
            path = self._claim(name)
            if path is None:
                continue
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                continue
            return path, body, _count(name)
        return None

    def remove(self, handle: Any) -> None:
        try:
            os.unlink(handle)
        except FileNotFoundError:
            pass

    def pending(self) -> int:
        return sum(_count(n) for n in self._names() if self._ours(n))

    def reset(self) -> None:
        # The files belong to the parent process after a fork; nothing to reset.
        self._seq = itertools.count()

    def _names(self) -> list[str]:
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            return []
        return sorted(n for n in names if n.endswith(self.SUFFIX))

    def _ours(self, name: str) -> bool:
        pid = _pid(name)
        return pid == os.getpid() or not _alive(pid)

    def _claim(self, name: str) -> str | None:
        path = os.path.join(self.directory, name)
        pid = _pid(name)
        if pid == os.getpid():
            return path
        if _alive(pid):
            return None
        ns, seq, _, rest = name.split("-", 3)
        claimed = os.path.join(self.directory, f"{ns}-{seq}-{os.getpid()}-{rest}")
        try:
            os.rename(path, claimed)
        except FileNotFoundError:
            return None  # another process adopted it first
        return claimed

    def _enforce_cap(self) -> int:
        sizes = []
        for name in self._names():
            if not self._ours(name):
                continue
            try:
                sizes.append((name, os.path.getsize(os.path.join(self.directory, name))))
            except FileNotFoundError:
                continue
        total = sum(size for _, size in sizes)
        evicted = 0
        for name, size in sizes[:-1]:
            if total <= self._max_bytes:
                break
            self.remove(os.path.join(self.directory, name))
            total -= size
            evicted += _count(name)
        return evicted


def _pid(name: str) -> int:
    return int(name.split("-")[2])


def _count(name: str) -> int:
    return int(name.split("-")[3][: -len(DiskSpool.SUFFIX)])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
