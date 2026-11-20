"""Small loopback-only web UI for selecting/pasting a resume and matching jobs."""
import json
from html import escape
import re
import socket
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .gemini import GeminiClient, load_config
from .archive_agent import agent_match
from .resume_match import ROOT, JobIndex, make_client, match_resume, save_report
from .resume_optimize import optimize_resume
from .http import Client
from .latest_refresh import Refresher
from .latest_web import LatestFeature, read_json_body

PAGE = Path(__file__).with_name("match.html")


class JobMatchServer(ThreadingHTTPServer):
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(index, client, port=8000, output_root=None, latest=None):
    provider = getattr(client, "provider", "openai")
    output_root = Path(output_root) if output_root else ROOT / "data/matches" / provider
    profile_dir = ROOT / "data/candidates/full_resumes_v1/markdown"
    lock = threading.Lock()
    from .candidate_match import ResumeIndex, match_candidates
    resume_index = ResumeIndex()
    profiles = resume_index.profiles
    allowed = {p["candidate_id"]: p for p in profiles}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Do not log resume content or request bodies.

        def reply(self, status, body, content_type="application/json; charset=utf-8"):
            raw = json.dumps(body, ensure_ascii=False).encode() if not isinstance(body, bytes) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def local_request(self):
            expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            return self.headers.get("Host") in expected and (
                not origin or urlsplit(origin).netloc in expected)

        def do_GET(self):
            if not self.local_request():
                return self.reply(403, {"error": "Only same-origin local requests are accepted."})
            path = urlsplit(self.path)
            if path.path in {
                "/assets/resume-pdf.mjs", "/assets/pdf.min.mjs", "/assets/pdf.worker.min.mjs"
            }:
                asset = PAGE.parent / "assets" / path.path.rsplit("/", 1)[-1]
                return self.reply(200, asset.read_bytes(), "text/javascript; charset=utf-8")
            hit = latest.handle_get(path.path) if latest else None
            if hit:
                return self.reply(*hit)
            if path.path == "/":
                return self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path.path == "/api/status":
                return self.reply(200, {"database_records": len(index.jobs), "provider": provider,
                                        "model_configured": client is not None,
                                        "rag_ready": isinstance(index, JobIndex)})
            if path.path == "/api/latest":
                for saved in sorted(output_root.glob("*/result.json"), key=lambda file: file.stat().st_mtime, reverse=True):
                    try:
                        report = json.loads(saved.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if report.get("direction", "jobs") == "jobs":
                        return self.reply(200, report)
                return self.reply(404, {"error": "No saved job recommendation report is available yet."})
            if path.path == "/api/job-description":
                query = parse_qs(path.query)
                source = query.get("source", [""])[0]
                source_id = query.get("source_id", [""])[0]
                item = next((job for job in index.jobs if str(job["source"]) == source and str(job["source_id"]) == source_id), None)
                if item is None:
                    return self.reply(404, {"error": "Historical job description not found."})
                title = escape(item["title"])
                content = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                           '<meta name="viewport" content="width=device-width,initial-scale=1">'
                           '<title>' + title + ' | Historical job description</title>'
                           '<style>body{max-width:900px;margin:40px auto;padding:0 24px;font:16px/1.7 system-ui;color:#172738}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}</style>'
                           '<h1>' + title + '</h1><p>' + escape(item["company"]) + ' · ' + escape(item.get("location") or "Location not specified") + '</p>'
                           '<p>Historical job description · Last captured: ' + escape(str(item.get("last_seen_at") or "Unknown")) + '</p>'
                           '<p>This archived record does not confirm that the position is currently open.</p><hr><pre>'
                           + escape(item["description"]) + '</pre></html>')
                return self.reply(200, content.encode("utf-8"), "text/html; charset=utf-8")
            if path.path == "/api/resumes":
                return self.reply(200, [{"id": p["candidate_id"], "title": p["headline"]} for p in profiles])
            if path.path == "/api/jobs":
                query = parse_qs(path.query).get("q", [""])[0].lower()
                return self.reply(200, [{"id": str(i), "title": item["title"] + " — " + item["company"]}
                                       for i, item in enumerate(index.jobs) if query in item["title"].lower()][:60])
            if path.path == "/api/job":
                identifier = parse_qs(path.query).get("id", [""])[0]
                if not identifier.isdigit() or not 0 <= int(identifier) < len(index.jobs):
                    return self.reply(404, {"error": "Job not found."})
                item = index.jobs[int(identifier)]
                return self.reply(200, {"text": item["title"] + "\n\n" + item["description"]})
            if path.path == "/api/resume-view":
                cid = parse_qs(path.query).get("id", [""])[0]
                if cid not in allowed:
                    return self.reply(404, {"error": "Resume not found."})
                return self.reply(200, (profile_dir / f"{cid}.md").read_bytes(), "text/plain; charset=utf-8")
            if path.path == "/api/resume":
                cid = parse_qs(path.query).get("id", [""])[0]
                if cid not in allowed:
                    return self.reply(404, {"error": "Resume not found."})
                from .resume_match import clean_resume
                return self.reply(200, {"text": clean_resume((profile_dir / f"{cid}.md").read_text(encoding="utf-8"))})
            return self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if not self.local_request():
                return self.reply(403, {"error": "Only same-origin local requests are accepted."})
            if latest and urlsplit(self.path).path.startswith("/api/new/"):
                try:
                    payload = read_json_body(self)
                except ValueError as exc:
                    return self.reply(400, {"error": str(exc)})
                hit = latest.handle_post(urlsplit(self.path).path, payload)
                return self.reply(*hit) if hit else self.reply(404, {"error": "Not found"})
            if self.path not in {"/api/match", "/api/resume-optimize"}:
                return self.reply(404, {"error": "Not found"})
            if client is None and self.path == "/api/match":
                return self.reply(503, {"error": "Configure an API key and restart the server."})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 500_000:
                    return self.reply(413, {"error": "Request size must be between 1 and 500,000 bytes."})
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    return self.reply(415, {"error": "A JSON request is required."})
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict) or not isinstance(payload.get("resume_text"), str):
                    raise ValueError("Resume text is required.")
                if self.path == "/api/resume-optimize":
                    job = next((item for item in index.jobs if item["source"] == payload.get("source")
                                and item["source_id"] == payload.get("source_id")), None)
                    if job is None:
                        return self.reply(404, {"error": "Target job description not found."})
                    return self.reply(200, optimize_resume(payload["resume_text"], job))
                top_k = payload.get("top_k", 5)
                if type(top_k) is not int or not 1 <= top_k <= 10:
                    raise ValueError("Match count must be between 1 and 10.")
                method = payload.get("method", "agent")
                if method not in ("agent", "pipeline"):
                    raise ValueError("Method must be agent or pipeline.")
                if not lock.acquire(blocking=False):
                    return self.reply(409, {"error": "Another match is running. Try again shortly."})
                try:
                    if payload.get("direction") == "candidates":
                        result = match_candidates(payload["resume_text"], resume_index, client, top_k, max(top_k, 7))
                    else:
                        shortlist = max(top_k, 7) if getattr(client, "compact", False) else max(top_k, 12)
                        run = agent_match if method == "agent" else match_resume  # pipeline = classic RAG baseline
                        result = run(payload["resume_text"], index, client, top_k, shortlist)
                    name = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
                    output = output_root / name
                    save_report(result, output)
                    result["report_folder"] = str(output)
                    return self.reply(200, result)
                finally:
                    lock.release()
            except (ValueError, RuntimeError, OSError) as exc:
                return self.reply(400, {"error": str(exc)})

    return JobMatchServer(("127.0.0.1", port), Handler)


def serve(args):
    if not 1 <= args.port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    index = JobIndex(args.database)
    try:
        client = make_client(args)
        problem = None
    except ValueError as exc:
        client, problem = None, str(exc)
    data_dir = ROOT / "data"
    refresher = Refresher(data_dir / "latest_jobs.sqlite3", lambda: Client(data_dir, archive=False))
    server = make_server(index, client, args.port, latest=LatestFeature(data_dir, client, refresher))
    refresher.start()
    print(f"JobMatch: http://127.0.0.1:{args.port} ({len(index.jobs)} indexed jobs)", flush=True)
    print(f"New US jobs: http://127.0.0.1:{args.port}/new (refreshes every 30 minutes)", flush=True)
    if client is None:
        print(f"No API key found ({problem}). Configure it and restart the server.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
