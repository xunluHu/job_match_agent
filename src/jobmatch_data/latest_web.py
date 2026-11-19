"""Endpoints and page for the "New US jobs" feature, plugged into webapp.make_server."""
import json
import sqlite3
import threading
import time
from pathlib import Path

from .latest_agent import agent_rank
from .latest_rankers import candidate_pool, keyword_rank, present
from .latest_store import counts, load_profile, save_profile
from .resume_match import clean_resume

PAGE = Path(__file__).with_name("latest.html")
JSON = "application/json; charset=utf-8"
NO_LLM = "No LLM is configured, so these results use keyword ranking."
AGENT_FAILED = "The agent did not finish, so these results use keyword ranking."
EMPTY_POOL = "No recent US jobs with descriptions yet. Wait for the background refresh or select Refresh now."


def read_json_body(handler):
    size = int(handler.headers.get("Content-Length") or 0)
    if not 0 < size <= 500_000:
        raise ValueError("Request size must be between 1 and 500,000 bytes.")
    if not handler.headers.get("Content-Type", "").startswith("application/json"):
        raise ValueError("A JSON request is required.")
    payload = json.loads(handler.rfile.read(size))
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required.")
    return payload


class LatestFeature:
    def __init__(self, data_dir, client, refresher):
        self.data_dir = Path(data_dir)
        self.client = client
        self.refresher = refresher
        self.db = self.data_dir / "latest_jobs.sqlite3"
        self.last = self.data_dir / "latest_matches" / "last.json"
        self.lock = threading.Lock()

    def handle_get(self, path):
        try:
            if path == "/new":
                return 200, PAGE.read_bytes(), "text/html; charset=utf-8"
            if path == "/api/new/status":
                profile = load_profile(self.data_dir) or {}
                return 200, {"refresh": self.refresher.status() if self.refresher else {},
                             "counts": counts(self.db, 7, time.time()),
                             "profile": {"has_resume": bool(profile), "job_type": profile.get("job_type"),
                                         "needs_sponsorship": profile.get("needs_sponsorship"),
                                         "updated_at": profile.get("updated_at")},
                             "llm_configured": self.client is not None}, JSON
            if path == "/api/new/profile":
                return 200, load_profile(self.data_dir) or {}, JSON
            if path == "/api/new/last":
                if not self.last.exists():
                    return 404, {"error": "No saved result yet."}, JSON
                return 200, json.loads(self.last.read_text(encoding="utf-8")), JSON
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            return 500, {"error": f"Could not read saved data: {exc}"}, JSON
        return None

    def handle_post(self, path, payload):
        try:
            if path == "/api/new/profile":
                try:
                    profile = save_profile(self.data_dir, payload.get("resume"), payload.get("job_type"),
                                           payload.get("needs_sponsorship"), time.time())
                except ValueError as exc:
                    return 400, {"error": str(exc)}, JSON
                return 200, profile, JSON
            if path == "/api/new/refresh":
                return 200, {"started": self.refresher.trigger() if self.refresher else False}, JSON
            if path == "/api/new/match":
                return self.match()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
            return 500, {"error": f"Could not complete the request: {type(exc).__name__}: {exc}"}, JSON
        return None

    def match(self):
        profile = load_profile(self.data_dir)
        if profile is None:
            return 400, {"error": "Save your resume first."}, JSON
        try:
            resume = clean_resume(profile["resume"])
        except ValueError as exc:
            return 400, {"error": str(exc)}, JSON
        now = time.time()
        jobs, days = candidate_pool(self.db, profile, now)
        if not jobs:
            return 409, {"error": EMPTY_POOL}, JSON
        if not self.lock.acquire(blocking=False):
            return 409, {"error": "A match is already running. Try again shortly."}, JSON
        try:
            if self.client is None:
                ranked, note = keyword_rank(jobs, resume), NO_LLM
            else:
                ranked = agent_rank(jobs, resume, self.client)
                note = AGENT_FAILED if ranked["fallback"] else None
            body = {"method": "agent" if self.client is not None and not ranked["fallback"] else "keyword",
                    "days": days, "pool_size": len(jobs), "results": present(ranked["matches"], jobs, resume),
                    "counts": counts(self.db, 7, now), "calls": ranked["calls"],
                    "input_tokens": ranked["input_tokens"], "output_tokens": ranked["output_tokens"],
                    "note": note, "created_at": now}
            self.last.parent.mkdir(parents=True, exist_ok=True)
            self.last.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            return 200, body, JSON
        finally:
            self.lock.release()
