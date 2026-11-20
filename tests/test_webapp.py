import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from jobmatch_data.webapp import make_server
from test_resume_match import JOB, RESUME, response


class WebAppTests(unittest.TestCase):
    def setUp(self):
        class Index:
            jobs = [JOB]
            def search(self, resume, limit):
                return [JOB]
        class Client:
            def generate(self, system, prompt, schema):
                return response(), {"model": "test-stub", "usage": {}}
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp")
        self.server = make_server(Index(), Client(), 0, self.tmp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_home_samples_and_full_matching_request(self):
        with urlopen(self.base) as result:
            self.assertIn("JobMatch", result.read().decode())
        with urlopen(self.base + "/api/resumes") as result:
            self.assertEqual(len(json.load(result)), 15)
        request = Request(self.base + "/api/match", data=json.dumps({"resume_text": RESUME, "top_k": 1}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as result:
            report = json.load(result)
        self.assertEqual(report["model"], "test-stub")
        self.assertEqual(len(report["matches"]), 1)
        self.assertTrue(Path(report["report_folder"], "result.json").is_file())

    def test_archived_description_and_job_only_history(self):
        with urlopen(self.base + "/api/job-description?source=test&source_id=1") as result:
            page = result.read().decode()
        self.assertIn(JOB["description"], page)
        self.assertIn("2026-11-16", page)
        self.assertIn("Historical job description", page)
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + "/api/job-description?source=test&source_id=missing")
        self.assertEqual(caught.exception.code, 404)
        for name, data in [("older", {"matches": [], "resume_summary": "jobs"}),
                           ("newer", {"direction": "candidates", "matches": []})]:
            folder = Path(self.tmp.name, name)
            folder.mkdir()
            (folder / "result.json").write_text(json.dumps(data), encoding="utf-8")
        with urlopen(self.base + "/api/latest") as result:
            self.assertEqual(json.load(result)["resume_summary"], "jobs")
        with urlopen(self.base) as result:
            page = result.read().decode()
        self.assertIn("Your resume", page)
        self.assertNotIn('id="direction"', page)
        self.assertIn("View historical job description", page)

    def test_jd_advice_returns_a_source_preserving_resume_download(self):
        original = "# Candidate\ncontact@example.org\n\n## Experience\n" + RESUME
        request = Request(self.base + "/api/resume-optimize",
                          data=json.dumps({"resume_text": original, "source": "test", "source_id": "1"}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as result:
            report = json.load(result)
        self.assertEqual(report["suggestions"][0]["jd_requirement"], JOB["description"])
        self.assertEqual(report["suggestions"][0]["resume_evidence"], RESUME)
        before, after = report["optimized_markdown"].split("\n\n## Relevant highlights\n\n", 1)
        self.assertEqual(before + after.split("\n\n", 1)[1], original)

    def test_rejects_cross_site_requests_and_arbitrary_file_paths(self):
        request = Request(self.base + "/api/match", data=b"{}", headers={"Content-Type": "application/json", "Origin": "https://attacker.invalid"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + "/api/resume?id=../../output/resources/gemini_key.txt")
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
