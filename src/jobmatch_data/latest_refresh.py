"""Refresh the latest-jobs store from Simplify and job APIs. No LLM calls, no raw archive."""
import threading
import time
import traceback
from urllib.error import HTTPError

from .latest_fetch import download_listings, fetch_jd
from .latest_rules import job_fields, listing_rows
from .latest_store import get_meta, pending_jd, save_jd, set_meta, sync_listings

LISTS = (("New-Grad-Positions", "newgrad"), ("Summer2027-Internships", "intern"))


def refresh(db, client, now=None, lists=LISTS, ref="dev"):
    now = time.time() if now is None else now
    # Download everything first: any failure here leaves the DB untouched.
    downloaded = [(download_listings(client, repo, ref), kind) for repo, kind in lists]
    rows = [row for listings, kind in downloaded for row in listing_rows(listings, kind, now)]
    report = sync_listings(db, rows, now)
    report.update(jd_ok=0, jd_failed=0)
    boards = {}
    for job in pending_jd(db):
        try:
            text = fetch_jd(client, job, boards)
        except HTTPError as exc:  # before OSError: HTTPError is a subclass
            if exc.code == 429 or exc.code >= 500:
                report["jd_failed"] += 1
                continue  # transient: stays pending for the next refresh
            text = None
        except OSError:  # network error or timeout: stays pending
            report["jd_failed"] += 1
            continue
        except (ValueError, KeyError, TypeError):
            text = None
        if text is None:
            save_jd(db, job["key"], None, {})
            report["jd_failed"] += 1
        else:
            save_jd(db, job["key"], text, job_fields(job["title"], text, job["sponsorship"]))
            report["jd_ok"] += 1
    set_meta(db, "last_refresh", str(now))
    report["finished_at"] = now
    return report


class Refresher:
    def __init__(self, db, make_client, interval=1800):
        self.db, self.make_client, self.interval = db, make_client, interval
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._running = False
        self._last_error = None

    def start(self):
        threading.Thread(target=self._loop, name="latest-refresh", daemon=True).start()

    def _loop(self):
        while True:
            with self._lock:
                self._running = True
            try:
                refresh(self.db, self.make_client())
                self._last_error = None
            except Exception as exc:  # keep the thread alive; the error is shown in status
                self._last_error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            finally:
                with self._lock:
                    self._running = False
            self._wake.wait(self.interval)
            self._wake.clear()

    def trigger(self):
        with self._lock:
            if self._running:
                return False
            self._wake.set()
            return True

    def status(self):
        value = get_meta(self.db, "last_refresh")
        return {"running": self._running, "last_error": self._last_error,
                "last_refresh": float(value) if value else None}
