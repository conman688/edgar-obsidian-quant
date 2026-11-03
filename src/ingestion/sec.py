"""Rate-limited SEC HTTP client shared by every ingestion job."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import requests


class SecClient:
    """Process-wide token bucket. SEC fair access caps at 10 req/s; we stay at 8."""

    def __init__(self, user_agent: str, rate_per_s: float = 8.0):
        if "@" not in user_agent:
            raise ValueError("SEC User-Agent must include a contact email")
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._interval = 1.0 / rate_per_s
        self._lock = threading.Lock()
        self._next = 0.0

    def _send(self, url: str, headers: dict | None = None, stream: bool = False,
              retries: int = 5) -> requests.Response:
        for attempt in range(retries):
            with self._lock:
                wait = self._next - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._next = max(self._next, time.monotonic()) + self._interval
            r = self._session.get(url, headers=headers, stream=stream, timeout=60)
            if r.status_code in (403, 429, 503):
                r.close()
                time.sleep(min(60, 2 ** attempt * 5))  # SEC 403 = throttled; back off hard
                continue
            if r.status_code != 304:
                r.raise_for_status()
            return r
        raise RuntimeError(f"SEC throttled after {retries} attempts: {url}")

    def get(self, url: str) -> bytes:
        return self._send(url).content

    def download(self, url: str, dest: Path) -> dict:
        """Stream `url` to `dest` with a conditional GET. Returns the file's metadata.

        Metadata (ETag, Last-Modified, sha256) lives in `<dest>.meta.json` and outlives
        the file, so raw zips can be deleted after loading and still be skipped next
        run. `changed` is False when the server answered 304 Not Modified.
        """
        meta_path = dest.with_name(dest.name + ".meta.json")
        old = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        headers = {}
        if old.get("etag"):
            headers["If-None-Match"] = old["etag"]
        if old.get("last_modified"):
            headers["If-Modified-Since"] = old["last_modified"]
        r = self._send(url, headers=headers, stream=True)
        if r.status_code == 304:
            r.close()
            return {**old, "changed": False, "path": None}

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        sha, n = hashlib.sha256(), 0
        with r, open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                sha.update(chunk)
                n += len(chunk)
        expected = r.headers.get("Content-Length")
        if expected and not r.headers.get("Content-Encoding") and int(expected) != n:
            tmp.unlink(missing_ok=True)
            raise IOError(f"truncated download {url}: {n} of {expected} bytes")
        os.replace(tmp, dest)
        meta = {"url": url, "etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified"),
                "sha256": sha.hexdigest(), "bytes": n}
        meta_path.write_text(json.dumps(meta, indent=2))
        return {**meta, "changed": True, "path": str(dest)}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
