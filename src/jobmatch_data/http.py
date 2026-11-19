"""Small read-only HTTP client with retries and an auditable response archive."""

import gzip
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class Client:
    def __init__(self, root: Path, delay: float = 0.5, archive: bool = True):
        self.root = root
        self.delay = delay
        self.archive = archive
        self.last_request = 0.0
        self.responses = []

    def get(self, url: str):
        for attempt in range(3):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            request = Request(url, headers={
                "User-Agent": "JobMatch-Research/0.1 (public job postings)",
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            })
            try:
                with urlopen(request, timeout=30) as response:
                    raw = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    if not self.archive:
                        return json.loads(raw)
                    fetched_at = datetime.now(timezone.utc).isoformat()
                    digest = hashlib.sha256(raw).hexdigest()
                    folder = self.root / "raw"
                    folder.mkdir(parents=True, exist_ok=True)
                    target = folder / f"{digest}.json"
                    target.write_bytes(raw)
                    self.responses.append({
                        "url": url, "fetched_at": fetched_at,
                        "sha256": digest, "path": str(target),
                        "status": response.status,
                        "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                    })
                    return json.loads(raw)
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                retry_after = exc.headers.get("Retry-After", "")
                time.sleep(min(float(retry_after), 30) if retry_after.isdigit() else 2 ** attempt)
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

