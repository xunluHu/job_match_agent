"""Regression net for the existing archive features. No network, no API key."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from jobmatch_data.pipeline import summarize
from jobmatch_data.resume_match import JobIndex, match_resume
from jobmatch_data.resume_optimize import optimize_resume
from jobmatch_data.storage import save
from jobmatch_data.webapp import make_server

JOBS = [
    {"id": "1", "title": "Backend Engineer", "company": "Acme", "url": "https://example.com/1", "location": "Seattle",
     "description": "About the role\nWe build payment APIs.\nRequirements:\n2+ years of experience building services\n"
                    "Python, FastAPI, PostgreSQL, Docker and Kubernetes in production\nDesign REST APIs and own reliability"},
    {"id": "2", "title": "Data Engineer", "company": "Globex", "url": "https://example.com/2", "location": "Remote",
     "description": "Responsibilities\nBuild ETL pipelines with Python, Airflow and Snowflake\n"
                    "Requirements:\nSQL and Python in production\nDocker and PostgreSQL experience is useful"},
    {"id": "3", "title": "Senior Backend Engineer", "company": "Initech", "url": "https://example.com/3", "location": "NYC",
     "description": "Requirements:\n8+ years of experience building services\nPython, PostgreSQL, Docker, Kubernetes\n"
                    "Lead design of distributed systems"},
]

RESUME = """# Sample Candidate

## Summary
Backend engineer with 3 years of experience building Python services.

## Experience
Software Engineer, Foo Inc — Jan 2022 - Jan 2025
- Built FastAPI services on PostgreSQL and deployed them with Docker and Kubernetes
- Designed REST APIs used by four internal teams and automated CI/CD with GitHub Actions

## Education
Bachelor of Science in Computer Science
"""


class FakeMatchClient:
    provider = "fake"
    compact = True

    def generate(self, system, prompt, schema):
        data = json.loads(prompt)
        rref = data["resume"][0]["ref"]
        matches = []
        for job in data["jobs"][:data["return_at_most"]]:
            jref = next(e["ref"] for e in job["evidence"] if len(e["text"]) >= 8)
            score = {"Backend Engineer": 60, "Data Engineer": 95}.get(job["title"], 99)
            matches.append({
                "job_ref": job["job_ref"], "fit_score": score, "verdict": "partial", "reason": "r",
                "evidence": [{"requirement": "q", "resume_ref": rref, "job_evidence_ref": jref}],
                "gaps": [], "uncertainties": [],
                "capabilities": [{"axis": a, "required_level": 50, "candidate_level": 50, "requirement": "q",
                                  "assessment": "a", "resume_ref": rref, "job_evidence_ref": jref}
                                 for a in ("technical", "experience", "delivery", "systems", "ownership")]})
        return {"resume_summary": "s", "matches": matches}, {"provider": "fake", "model": "fake", "usage": {}}


class ExistingFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        save(JOBS, "test", self.root)
        self.index = JobIndex(self.root / "jobs.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_storage_and_stats(self):
        stats = summarize(self.root)
        self.assertEqual(stats["records"], 3)
        self.assertEqual(stats["with_description"], 3)

    def test_match_resume_with_fake_client(self):
        result = match_resume(RESUME, self.index, FakeMatchClient(), top_k=3, shortlist=3)
        self.assertEqual(result["method"], "sqlite_bm25_parent_child_then_fake")
        self.assertEqual([m["title"] for m in result["matches"]],
                         ["Backend Engineer", "Data Engineer", "Senior Backend Engineer"])

    def test_resume_optimize(self):
        advice = optimize_resume(RESUME, self.index.jobs[0])
        self.assertTrue(advice["suggestions"])
        self.assertIn("## Relevant highlights", advice["optimized_markdown"])

    def test_web_endpoints(self):
        server = make_server(self.index, None, 0, output_root=self.root / "matches")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def call(path, body=None, host=None):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(base + path, data=data)
            if data is not None:
                request.add_header("Content-Type", "application/json")
            if host:
                request.add_header("Host", host)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    raw = response.read()
                    status = response.status
            except urllib.error.HTTPError as error:
                raw, status = error.read(), error.code
            try:
                return status, json.loads(raw)
            except ValueError:
                return status, raw

        self.assertEqual(call("/")[0], 200)
        self.assertEqual(call("/api/status")[1]["database_records"], 3)
        self.assertEqual(call("/api/match", {"resume_text": "x", "top_k": 5})[0], 503)
        self.assertEqual(call("/api/status", host="evil.example")[0], 403)


if __name__ == "__main__":
    unittest.main()
