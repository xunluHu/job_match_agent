import csv
import tempfile
import time
import unittest
from pathlib import Path

from jobmatch_data.latest_eval import (METHODS, check_constraints, label_rows, load_eval_resumes, read_labels, report,
                                       run_eval, snapshot, to_markdown, write_labels)
from jobmatch_data.latest_rules import listing_rows
from jobmatch_data.latest_store import JOB_TYPES, save_jd, set_meta, sync_listings
from jobmatch_data.resume_match import clean_resume
from tests.fakes import ScriptedClient, listing, pool_job

ROOT = Path(__file__).resolve().parents[1]
THEN = time.time() - 30 * 86400
RESUME = ("## Summary\nBackend engineer building Python services with FastAPI and PostgreSQL.\n"
          "## Experience\nSoftware Engineer, Example Co — Jan 2022 - Jan 2025\n"
          "- Built FastAPI services on PostgreSQL and deployed them with Docker and Kubernetes\n")
RESUMES = [{"name": "backend", "resume": RESUME, "job_type": "fulltime", "needs_sponsorship": False}]
RUN_KEYS = {"resume", "method", "repeat", "matches", "calls", "input_tokens", "output_tokens", "seconds",
            "violations", "fallback", "tool_errors", "steps"}


def submit_ten(prompt):
    return {"thought": "t", "action": "submit", "query": "", "job_id": "",
            "items": [{"job_id": f"J{i:03d}", "score": 90 - i, "reason": "Fit."} for i in range(1, 11)]}


class LatestEval(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.tmp = Path(self.tmp_dir.name)
        db = self.tmp / "latest_jobs.sqlite3"
        items = [listing(f"k{i}", f"Role {i}", f"Co {i}", f"https://job-boards.greenhouse.io/co/jobs/{i}",
                         ["Seattle, WA"], 1 + i * 0.1, THEN) for i in range(12)]
        rows = listing_rows(items, "newgrad", THEN)
        sync_listings(db, rows, THEN)
        for i, row in enumerate(rows):
            save_jd(db, row["key"], f"Python FastAPI role {i}" if i < 3 else f"Java role {i}",
                    {"degree": [], "min_years": None, "visa": "not_mentioned"})
        set_meta(db, "last_refresh", str(THEN))
        self.snap = snapshot(db, self.tmp / "snapshots", THEN)

    def test_snapshot_name(self):
        self.assertEqual(self.snap.name, time.strftime("%Y%m%d-%H%M", time.gmtime(THEN)) + ".sqlite3")
        self.assertTrue(self.snap.exists())

    def test_methods_registry(self):
        self.assertEqual(set(METHODS), {"keyword", "score_all", "agent", "agent_low"})

    def test_window_is_relative_to_snapshot(self):
        run = run_eval(self.snap, RESUMES, ["keyword"], None)
        self.assertEqual(run["now"], THEN)
        self.assertEqual(len(run["runs"][0]["matches"]), 10)
        self.assertEqual(run["runs"][0]["matches"][0]["key"], "k0")

    def test_run_record_shape(self):
        run = run_eval(self.snap, RESUMES, ["keyword", "agent"], ScriptedClient([submit_ten] * 3), repeats=3)
        self.assertEqual(set(run), {"snapshot", "now", "created_at", "runs"})
        self.assertEqual([(r["method"], r["repeat"]) for r in run["runs"]],
                         [("keyword", 0), ("agent", 0), ("agent", 1), ("agent", 2)])
        for record in run["runs"]:
            self.assertEqual(set(record), RUN_KEYS)
            self.assertEqual(record["violations"], 0)
        agent = run["runs"][1]
        self.assertEqual((agent["calls"], agent["steps"], agent["tool_errors"], agent["fallback"]), (1, 1, 0, None))

    def test_check_constraints(self):
        pool = [pool_job("a", "A", "Co", "jd", THEN), pool_job("b", "B", "Co", "jd", THEN, locations=("London, UK",)),
                pool_job("c", "C", "Co", "jd", THEN, url="javascript:alert(1)")]
        self.assertEqual(check_constraints([{"key": "a"}], pool[:1]), 0)
        self.assertEqual(check_constraints([{"key": "a"}, {"key": "a"}, {"key": "zzz"}], pool[:1]), 2)
        self.assertEqual(check_constraints([{"key": "b"}, {"key": "c"}], pool), 2)

    def test_labels_union_and_merge(self):
        run = run_eval(self.snap, RESUMES, ["keyword", "agent"], ScriptedClient([submit_ten]))
        rows = label_rows(run)
        keys = {m["key"] for r in run["runs"] for m in r["matches"]}
        self.assertEqual(len(rows), len(keys))
        self.assertEqual(set(rows[0]), {"resume", "key", "title", "company", "url", "label"})
        self.assertTrue(rows[0]["url"].startswith("https://"))
        path = self.tmp / "run.labels.csv"
        write_labels(run, path)
        with path.open(newline="", encoding="utf-8") as f:
            table = list(csv.DictReader(f))
        table[0]["label"] = "1"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
        write_labels(run, path)
        self.assertEqual(read_labels(path), {(table[0]["resume"], table[0]["key"]): 1})

    def test_report_precision(self):
        run = {"runs": [{"resume": "r", "method": "agent", "repeat": 0,
                         "matches": [{"key": f"k{i}"} for i in range(10)], "calls": 3, "input_tokens": 100,
                         "output_tokens": 20, "seconds": 2.0, "violations": 0, "fallback": None,
                         "tool_errors": 1, "steps": 4}]}
        labels = {("r", "k0"): 1, ("r", "k1"): 1, ("r", "k2"): 1, ("r", "k3"): 0, ("r", "other"): 1}
        rows = report(run, labels)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["method"], row["precision_at_10"], row["labeled"]), ("agent", 0.75, 4))
        self.assertEqual((row["calls"], row["input_tokens"], row["fallback_rate"], row["tool_error_rate"]),
                         (3, 100, 0, 0.25))
        self.assertTrue(to_markdown(rows).startswith("| method |"))

    def test_report_unlabeled_and_keyword(self):
        run = {"runs": [{"resume": "r", "method": "keyword", "repeat": 0, "matches": [{"key": "k0"}], "calls": 0,
                         "input_tokens": 0, "output_tokens": 0, "seconds": 0.1, "violations": 0, "fallback": None,
                         "tool_errors": 0, "steps": 0}]}
        row = report(run, {})[0]
        self.assertIsNone(row["precision_at_10"])
        self.assertIsNone(row["tool_error_rate"])

    def test_eval_resumes_load(self):
        resumes = load_eval_resumes(ROOT / "eval" / "resumes")
        self.assertEqual(len(resumes), 4)
        for entry in resumes:
            self.assertGreaterEqual(len(clean_resume(entry["resume"])), 600)
            self.assertIn(entry["job_type"], JOB_TYPES)
            self.assertIsInstance(entry["needs_sponsorship"], bool)
            for heading in ("## Summary", "## Experience", "## Projects", "## Education", "## Skills"):
                self.assertIn(heading, entry["resume"], entry["name"])
            self.assertIn("@example.com", entry["resume"])


if __name__ == "__main__":
    unittest.main()
