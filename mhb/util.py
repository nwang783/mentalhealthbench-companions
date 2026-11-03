"""Small shared helpers: .env loading, HTTP with retry, rate limiting, cache, progress."""
import hashlib
import json
import os
import random
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}


def load_env(path: Path) -> None:
    """Read KEY=VALUE lines into os.environ. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key, value)


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message[:500]}" if status else message[:500])
        self.status = status


def post(url: str, body: dict, headers: dict, timeout: float = 180, retries: int = 5, raw: bool = False):
    """POST JSON. Retries on rate limits, server errors, and network errors. Returns parsed JSON (or text if raw)."""
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", **headers}
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            return text if raw else json.loads(text)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            if e.code in RETRY_STATUS and attempt < retries:
                retry_after = e.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else 2 ** attempt
                time.sleep(min(delay, 60) + random.random())
                continue
            raise APIError(e.code, text) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            if attempt < retries:
                time.sleep(min(2 ** attempt, 60) + random.random())
                continue
            raise APIError(0, f"{type(e).__name__}: {e}") from None


class RateLimiter:
    """Spaces calls at least min_interval seconds apart, across threads."""

    def __init__(self, min_interval: float = 0.0):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.min_interval
        time.sleep(max(0.0, slot - now))


def cache_key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Cache:
    """SQLite key-value store, so an interrupted or repeated run does not pay twice."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
            self._conn.commit()


class Progress:
    def __init__(self, label: str, total: int):
        self.label, self.total, self.done, self.errors, self.cached = label, total, 0, 0, 0
        self._lock = threading.Lock()
        self._render()

    def tick(self, error: bool = False, cached: bool = False) -> None:
        with self._lock:
            self.done += 1
            self.errors += error
            self.cached += cached
            self._render()

    def _render(self) -> None:
        line = f"  {self.label}: {self.done}/{self.total}  (cached {self.cached}, errors {self.errors})"
        finished = self.done >= self.total
        if sys.stdout.isatty():
            sys.stdout.write("\r" + line + ("\n" if finished else ""))
        elif finished or self.done % max(1, self.total // 10) == 0:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
