import tempfile
import unittest
from pathlib import Path

from jobmatch_data.latest_rankers import (TECH_ONLY, UNTRUSTED, bm25_scores, candidate_pool, jd_excerpt,
                                          keyword_rank, present, score_all, usage_tokens)
from jobmatch_data.latest_rules import listing_rows
from jobmatch_data.latest_store import save_jd, sync_listings
from tests.fakes import ScriptedClient, listing, pool_job

NOW = 1_800_000_000
BACKEND_RESUME = """## Summary
Backend engineer building Python services.
## Experience
Software Engineer, Example Co — 2022 - 2025
- Built FastAPI services on PostgreSQL, deployed with Docker and Kubernetes
- Designed REST APIs and backend services
"""
JDS = {
    "backend": "Backend engineer for REST APIs and backend services. Python, FastAPI, PostgreSQL, Docker, Kubernetes.",
    "frontend": "Frontend engineer. React, TypeScript, CSS and accessibility.",
    "ml": "Machine learning intern. PyTorch, TensorFlow, model training and evaluation.",
    "hardware": "Hardware intern. Verilog, FPGA, circuit design and lab testing.",
    "data": "Data engineer. Spark, Airflow, Snowflake and SQL pipelines. Some Python.",
    "mobile": "Mobile engineer. Swift, Kotlin, iOS and Android apps.",
    "devops": "Site reliability engineer. Kubernetes, Terraform, AWS and on-call.",
    "java": "Java developer. Spring Boot microservices and Kafka.",
    "embedded": "Embedded software engineer. C, C++ and RTOS firmware.",
    "security": "Security analyst. Threat modeling, SIEM and incident response.",
    "design": "Product designer. Figma prototypes and user research.",
    "qa": "QA engineer. Selenium test automation and test plans.",
}
POOL12 = [pool_job(k, k.title() + " Engineer", "Co " + k, jd, NOW - i) for i, (k, jd) in enumerate(JDS.items())]


class LatestRankers(unittest.TestCase):
    def test_jd_excerpt(self):
        self.assertEqual(jd_excerpt("a" * 2500), "a" * 2500)
        text = "h" * 1800 + "m" * 1000 + "t" * 700
        self.assertEqual(jd_excerpt(text), "h" * 1800 + "\n...\n" + "t" * 700)

    def test_usage_tokens(self):
        self.assertEqual(usage_tokens({"usage": {"input_tokens": 100, "output_tokens": 20}}), (100, 20))
        self.assertEqual(usage_tokens({"usage": {"promptTokenCount": 90, "candidatesTokenCount": 10,
                                                 "thoughtsTokenCount": 5}}), (90, 15))
        self.assertEqual(usage_tokens({}), (0, 0))
        self.assertEqual(usage_tokens({"usage": None}), (0, 0))

    def test_bm25_prefers_doc_with_both_terms(self):
        scores = bm25_scores("python kubernetes", ["python only", "python and kubernetes", "java"])
        self.assertEqual(max(range(3), key=scores.__getitem__), 1)
        self.assertEqual(scores[2], 0.0)
        self.assertEqual(bm25_scores("", ["x"]), [0.0])
        self.assertEqual(bm25_scores("python", []), [])

    def test_keyword_rank(self):
        result = keyword_rank(POOL12, BACKEND_RESUME)
        self.assertEqual(len({m["key"] for m in result["matches"]}), 10)
        self.assertEqual(result["matches"][0]["key"], "backend")
        self.assertTrue(result["matches"][0]["reason"].startswith("Matched skills: "))
        self.assertIn("FastAPI", result["matches"][0]["reason"])
        self.assertTrue(all(0 <= m["score"] <= 100 and isinstance(m["score"], int) for m in result["matches"]))
        self.assertEqual((result["calls"], result["input_tokens"], result["output_tokens"], result["fallback"]),
                         (0, 0, 0, None))
        self.assertEqual(len(keyword_rank(POOL12[:3], BACKEND_RESUME)["matches"]), 3)
        self.assertEqual(keyword_rank([], BACKEND_RESUME)["matches"], [])

    def test_keyword_rank_reason_without_skills(self):
        result = keyword_rank(POOL12[-2:], "I like gardening and long walks.")
        self.assertEqual(result["matches"][0]["reason"], "Matched skills: none")

    def test_present(self):
        pool = [pool_job("a", "SWE", "Acme", "Python and Docker", NOW, locations=("Toronto, ON", "Seattle, WA"))]
        items = present([{"key": "a", "score": 80, "reason": "r"}, {"key": "zzz", "score": 70, "reason": "r"}],
                        pool, "Python developer")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item["rank"], item["key"], item["locations"], item["skills"], item["score"], item["reason"]),
                         (1, "a", ["Seattle, WA"], ["Python"], 80, "r"))
        for field in ("title", "company", "posted", "url", "degree", "min_years", "visa"):
            self.assertIn(field, item)


def scores(*pairs):
    return {"items": [{"i": i, "score": score, "reason": f"r{i}"} for i, score in pairs]}


SCORE_POOL = [pool_job(f"k{i}", f"Role {i}", f"Co {i}", f"Python job {i}", NOW - i, url=f"https://jobs.example.com/{i}")
              for i in range(11)] + [pool_job("best", "Best", "Co B", "Python FastAPI", NOW - 20)]


class ScoreAll(unittest.TestCase):
    def test_score_all_batches_and_sorts(self):
        client = ScriptedClient([scores(*[(i, 50 + i) for i in range(1, 11)]), scores((1, 10), (2, 99))])
        r = score_all(SCORE_POOL, BACKEND_RESUME, client)
        self.assertEqual(r["calls"], 2)
        self.assertEqual(r["matches"][0], {"key": "best", "score": 99, "reason": "r2"})
        self.assertEqual([m["key"] for m in r["matches"][1:4]], ["k9", "k8", "k7"])
        self.assertEqual(len(r["matches"]), 10)
        self.assertNotIn("k10", [m["key"] for m in r["matches"]])  # score 10 falls outside the top 10
        self.assertEqual(r["output_tokens"], 40)
        self.assertIsNone(r["fallback"])
        self.assertTrue(all("https://" not in p for _, p, _ in client.prompts))
        system, prompt, schema = client.prompts[1]
        self.assertIn(UNTRUSTED, system)
        self.assertIn(TECH_ONLY, system)
        self.assertIn("[2] Best — Co B (Seattle, WA)", prompt)
        self.assertEqual(schema["properties"]["items"]["items"]["required"], ["i", "score", "reason"])

    def test_ties_break_by_newer_posting(self):
        client = ScriptedClient([scores((1, 70), (2, 70))])
        self.assertEqual([m["key"] for m in score_all(SCORE_POOL[:2], "", client)["matches"]], ["k0", "k1"])

    def test_missing_item_dropped(self):
        client = ScriptedClient([scores((1, 80), (2, 70), (4, 60), (99, 90), (2, 10))])
        r = score_all(SCORE_POOL[:4], BACKEND_RESUME, client)
        self.assertEqual([m["key"] for m in r["matches"]], ["k0", "k1", "k3"])
        self.assertTrue(any("k2" in str(t) for t in r["trace"]))

    def test_malformed_reply_items_ignored(self):
        bad = {"items": ["x", None, {"i": 1, "score": 70, "reason": "ok"}]}
        r = score_all(SCORE_POOL[:2], BACKEND_RESUME, ScriptedClient([bad]))
        self.assertEqual([m["key"] for m in r["matches"]], ["k0"])
        self.assertEqual(score_all(SCORE_POOL[:2], "", ScriptedClient([{"items": "nope"}]))["matches"], [])

    def test_failed_batch_is_skipped(self):
        client = ScriptedClient([RuntimeError("timeout"), scores((1, 10), (2, 99))])
        r = score_all(SCORE_POOL, BACKEND_RESUME, client)
        self.assertEqual([m["key"] for m in r["matches"]], ["best", "k10"])
        self.assertIsNone(r["fallback"])
        self.assertTrue(any("timeout" in str(t) for t in r["trace"]))

    def test_all_batches_fail_fall_back(self):
        client = ScriptedClient([RuntimeError("down"), RuntimeError("down")])
        r = score_all(SCORE_POOL, BACKEND_RESUME, client)
        self.assertEqual((r["fallback"], len(r["matches"]), r["calls"]), ("llm_error", 10, 0))


class CandidatePool(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp_dir.name) / "latest_jobs.sqlite3"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def add(self, kind, ages):
        items = [listing(f"{kind}{i}", f"Role {i}", f"{kind} Co {i}", f"https://job-boards.greenhouse.io/c/jobs/{i}",
                         ["Austin, TX"], age, NOW) for i, age in enumerate(ages)]
        rows = listing_rows(items, kind, NOW)
        sync_listings(self.db, rows, NOW)
        for row in rows:
            save_jd(self.db, row["key"], "Python", {"degree": [], "min_years": None, "visa": "not_mentioned"})

    def test_candidate_pool_widens_to_14_days(self):
        self.add("newgrad", [1, 2, 3, 4, 8, 9, 10, 11, 12, 12, 13, 13])
        jobs, days = candidate_pool(self.db, {"job_type": "fulltime", "needs_sponsorship": False}, NOW)
        self.assertEqual((len(jobs), days), (12, 14))

    def test_candidate_pool_keeps_7_days_when_enough(self):
        self.add("newgrad", [1] * 10 + [9])
        jobs, days = candidate_pool(self.db, {"job_type": "both", "needs_sponsorship": False}, NOW)
        self.assertEqual((len(jobs), days), (10, 7))

    def test_candidate_pool_job_type(self):
        self.add("newgrad", [1, 2])
        self.add("intern", [1])
        jobs, _ = candidate_pool(self.db, {"job_type": "intern", "needs_sponsorship": False}, NOW)
        self.assertEqual({j["kind"] for j in jobs}, {"intern"})


if __name__ == "__main__":
    unittest.main()
