import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from jobmatch_data.latest_rules import listing_rows
from jobmatch_data.latest_store import save_jd, save_profile, sync_listings
from jobmatch_data.latest_web import LatestFeature
from jobmatch_data.resume_match import JobIndex
from jobmatch_data.storage import save
from jobmatch_data.webapp import make_server
from tests.fakes import ScriptedClient, listing
from tests.test_existing_regression import JOBS, RESUME

RESULT_FIELDS = {"rank", "key", "title", "company", "locations", "posted", "url", "degree", "min_years", "visa",
                 "skills", "score", "reason"}
TOPICS = ["Python FastAPI PostgreSQL backend", "React frontend", "PyTorch ML", "Verilog FPGA", "Spark data",
          "Swift iOS", "Kubernetes SRE", "Java Spring", "C++ firmware", "SIEM security", "Figma design", "Selenium QA"]


def submit_ten(prompt):
    return {"thought": "t", "action": "submit", "query": "", "job_id": "",
            "items": [{"job_id": f"J{i:03d}", "score": 90 - i, "reason": "Fit."} for i in range(1, 11)]}


class LatestWeb(unittest.TestCase):
    def start(self, client=None):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.tmp = Path(self.tmp_dir.name)
        save(JOBS, "test", self.tmp / "archive")
        self.feature = LatestFeature(self.tmp, client, None)
        server = make_server(JobIndex(self.tmp / "archive" / "jobs.sqlite3"), None, 0,
                             output_root=self.tmp / "matches", latest=self.feature)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def call(self, path, body=None, host=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if host:
            request.add_header("Host", host)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw.decode()

    def seed(self, count=12):
        now = time.time()
        items = [listing(f"k{i}", f"Role {i}", f"Co {i}", f"https://job-boards.greenhouse.io/co/jobs/{i}",
                         ["Toronto, ON", "Seattle, WA"], 1 + i * 0.1, now) for i in range(count)]
        rows = listing_rows(items, "newgrad", now)
        db = self.tmp / "latest_jobs.sqlite3"
        sync_listings(db, rows, now)
        for i, row in enumerate(rows):
            save_jd(db, row["key"], f"We need {TOPICS[i % len(TOPICS)]} experience.",
                    {"degree": [{"level": "bachelor", "priority": "required"}], "min_years": 2, "visa": "not_mentioned"})

    def save_resume(self, text=RESUME):
        save_profile(self.tmp, text, "both", False, time.time())

    def test_new_page(self):
        self.start()
        status, body = self.call("/new")
        self.assertEqual(status, 200)
        self.assertIn('id="find"', body)
        self.assertIn('id="results"', body)

    def test_status_empty(self):
        self.start()
        status, body = self.call("/api/new/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["counts"], {"total": 0, "with_jd": 0, "pending": 0, "no_jd": 0})
        self.assertFalse(body["profile"]["has_resume"])
        self.assertFalse(body["llm_configured"])
        self.assertEqual(body["refresh"], {})

    def test_profile_roundtrip_and_bad_job_type(self):
        self.start()
        sent = {"resume": RESUME, "job_type": "intern", "needs_sponsorship": True}
        self.assertEqual(self.call("/api/new/profile", sent)[0], 200)
        status, body = self.call("/api/new/profile")
        self.assertEqual({k: body[k] for k in sent}, sent)
        self.assertTrue(self.call("/api/new/status")[1]["profile"]["has_resume"])
        self.assertEqual(self.call("/api/new/profile", dict(sent, job_type="x"))[0], 400)

    def test_match_without_profile_400(self):
        self.start()
        self.assertEqual(self.call("/api/new/match", {}), (400, {"error": "Save your resume first."}))

    def test_match_short_resume_400(self):
        client = ScriptedClient([])
        self.start(client)
        self.seed()
        self.save_resume("too short")
        self.assertEqual(self.call("/api/new/match", {})[0], 400)
        self.assertEqual(client.prompts, [])

    def test_match_empty_pool_409(self):
        self.start()
        self.save_resume()
        status, body = self.call("/api/new/match", {})
        self.assertEqual(status, 409)
        self.assertIn("No recent US jobs with descriptions yet.", body["error"])

    def test_match_keyword_fallback_without_llm(self):
        self.start()
        self.seed()
        self.save_resume()
        status, body = self.call("/api/new/match", {})
        self.assertEqual(status, 200)
        self.assertEqual((body["method"], len(body["results"])), ("keyword", 10))
        self.assertIn("No LLM is configured", body["note"])
        self.assertEqual(set(body["results"][0]), RESULT_FIELDS)
        self.assertEqual(body["results"][0]["locations"], ["Seattle, WA"])
        self.assertEqual((body["days"], body["pool_size"], body["calls"]), (7, 12, 0))

    def test_match_agent_and_last(self):
        client = ScriptedClient([submit_ten])
        self.start(client)
        self.seed()
        self.save_resume()
        status, body = self.call("/api/new/match", {})
        self.assertEqual(status, 200)
        self.assertEqual((body["method"], body["note"], body["calls"]), ("agent", None, 1))
        self.assertEqual(body["results"][0]["key"], "k0")
        self.assertEqual(self.call("/api/new/last"), (200, body))

    def test_agent_failure_note(self):
        self.start(ScriptedClient([RuntimeError("timeout")]))
        self.seed()
        self.save_resume()
        body = self.call("/api/new/match", {})[1]
        self.assertEqual(body["method"], "keyword")
        self.assertEqual(body["note"], "The agent did not finish, so these results use keyword ranking.")

    def test_last_404_before_any_match(self):
        self.start()
        self.assertEqual(self.call("/api/new/last")[0], 404)

    def test_match_busy_409(self):
        self.start()
        self.seed()
        self.save_resume()
        self.feature.lock.acquire()
        self.addCleanup(self.feature.lock.release)
        self.assertEqual(self.call("/api/new/match", {}),
                         (409, {"error": "A match is already running. Try again shortly."}))

    def test_refresh_without_refresher(self):
        self.start()
        self.assertEqual(self.call("/api/new/refresh", {}), (200, {"started": False}))

    def test_foreign_host_403(self):
        self.start()
        self.assertEqual(self.call("/api/new/status", host="evil.example")[0], 403)

    def test_existing_routes_still_work(self):
        self.start()
        self.assertEqual(self.call("/")[0], 200)
        self.assertEqual(self.call("/api/status")[1]["database_records"], 3)
        self.assertEqual(self.call("/api/new/unknown", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
