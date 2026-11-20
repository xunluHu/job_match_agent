import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from jobmatch_data.archive_agent import SYSTEM, agent_match
from jobmatch_data.resume_match import JobIndex, clean_resume, match_resume, score_shortlist
from jobmatch_data.storage import save
from jobmatch_data.webapp import make_server
from tests.test_existing_regression import JOBS, RESUME, FakeMatchClient


def act(action, query="", job_id="", job_ids=()):
    return {"thought": "t", "action": action, "query": query, "job_id": job_id, "job_ids": list(job_ids)}


THREE = act("submit", job_ids=["J001", "J002", "J003"])


def agent_system(lo, hi):
    return SYSTEM.format(lo=lo, hi=hi)


class Router:
    """Agent steps come from a script; the final scoring call goes to the regression suite's fake scorer."""
    provider = "fake"
    compact = True

    def __init__(self, script):
        self.script = list(script)
        self.agent_prompts = []
        self.scorer = FakeMatchClient()

    def generate(self, system, prompt, schema):
        if "action" not in schema.get("properties", {}):
            return self.scorer.generate(system, prompt, schema)
        self.agent_prompts.append(prompt)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, {"provider": "fake", "model": "fake", "usage": {"input_tokens": 100, "output_tokens": 10}}


class FlakyScorer(FakeMatchClient):
    """The first `bad` replies drop one match (the wrong count); later replies are valid."""

    def __init__(self, bad):
        self.bad, self.calls = bad, 0

    def generate(self, system, prompt, schema):
        self.calls += 1
        reply, _ = super().generate(system, prompt, schema)
        if self.calls <= self.bad:
            reply = dict(reply, matches=reply["matches"][:-1])
        return reply, {"provider": "fake", "model": "fake", "usage": {"input_tokens": 100, "output_tokens": 10}}


class ArchiveBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        save(JOBS, "test", self.root)
        self.index = JobIndex(self.root / "jobs.sqlite3")


class RetrievalAndScoring(ArchiveBase):
    def test_retrieve_with_query_uses_query_terms(self):
        results, retrieval = self.index.retrieve(RESUME, 3, query="Airflow Snowflake ETL")
        self.assertEqual(results[0]["title"], "Data Engineer")
        self.assertIn("airflow", retrieval["query_terms"])
        self.assertEqual(retrieval["candidate_profile"], self.index.retrieve(RESUME, 3)[1]["candidate_profile"])

    def test_query_results_follow_relevance_not_screening(self):
        # All three jobs mention Python; the backend resume screens Backend Engineer first, but the query is about data.
        results, _ = self.index.retrieve(RESUME, 3, query="Airflow Snowflake Python")
        self.assertEqual(results[0]["title"], "Data Engineer")
        self.assertEqual(self.index.retrieve(RESUME, 3, query="FastAPI Kubernetes Python")[0][0]["title"],
                         "Backend Engineer")

    def test_retrieve_without_query_is_unchanged(self):
        self.assertEqual(self.index.retrieve(RESUME, 3), self.index.sections.retrieve(RESUME, self.index.jobs, 3))

    def test_score_shortlist_matches_pipeline(self):
        resume = clean_resume(RESUME)
        jobs, retrieval = self.index.retrieve(resume, 3)
        result = score_shortlist(resume, jobs, retrieval, self.index, FakeMatchClient(), 3, time.monotonic())
        self.assertEqual([m["title"] for m in result["matches"]],
                         [m["title"] for m in match_resume(RESUME, self.index, FakeMatchClient(), 3, 3)["matches"]])
        self.assertEqual(result["method"], "sqlite_bm25_parent_child_then_fake")


    def test_invalid_scoring_reply_is_retried_once(self):
        client = FlakyScorer(bad=1)
        result = match_resume(RESUME, self.index, client, 3, 3)
        self.assertEqual((client.calls, len(result["matches"]), result["scoring_calls"]), (2, 3, 2))
        self.assertEqual(result["usage"], {"input_tokens": 200, "output_tokens": 20})  # both calls are counted

    def test_second_invalid_scoring_reply_still_fails(self):
        client = FlakyScorer(bad=2)
        with self.assertRaisesRegex(ValueError, "requested number of matches"):
            match_resume(RESUME, self.index, client, 3, 3)
        self.assertEqual(client.calls, 2)


class ArchiveAgent(ArchiveBase):
    def run_agent(self, script, **kwargs):
        client = Router(script)
        return agent_match(RESUME, self.index, client, **{"top_k": 3, "shortlist": 3, **kwargs}), client

    def test_happy_path(self):
        r, client = self.run_agent([act("search_jobs", query="python fastapi"), act("read_job", job_id="J001"), THREE])
        self.assertEqual(r["method"], "agent_bm25_then_fake")
        self.assertEqual((r["agent"]["steps"], r["agent"]["fallback"], r["agent"]["input_tokens"]), (3, None, 300))
        self.assertEqual(r["retrieval"]["method"], "agent_bm25")
        self.assertEqual(r["retrieval"]["searches"][0]["query"], "python fastapi")
        self.assertEqual(len(r["matches"]), 3)
        self.assertIn("Requirements", client.agent_prompts[2])  # the read_job observation carried the description
        self.assertEqual(r["agent"]["trace"][0]["thought"], "t")

    def test_bad_submit_is_sent_back(self):
        r, client = self.run_agent([act("search_jobs", query="python"), act("submit", job_ids=["J999"]),
                                    act("submit", job_ids=["J001"]), THREE])
        self.assertIn("Submit rejected", client.agent_prompts[2])
        self.assertIn("Unknown job ids: J999", client.agent_prompts[2])
        self.assertIn("Submit 3 to 3 jobs, or none if nothing fits; you sent 1.", client.agent_prompts[3])
        self.assertIsNone(r["agent"]["fallback"])
        self.assertEqual(sum(1 for t in r["agent"]["trace"] if t["error"]), 2)

    def test_unknown_read_and_empty_search(self):
        r, client = self.run_agent([act("read_job", job_id="J050"), act("search_jobs", query="zzqqxx"),
                                    act("search_jobs", query="python"), THREE])
        self.assertIn("Unknown job id", client.agent_prompts[1])
        self.assertIn("No matching jobs.", client.agent_prompts[2])
        self.assertIsNone(r["agent"]["fallback"])

    def test_stable_ids(self):
        r, client = self.run_agent([act("search_jobs", query="fastapi"), act("search_jobs", query="python"), THREE])
        self.assertIn("J001 | Backend Engineer | Acme", client.agent_prompts[1])
        self.assertIn("J001 | Backend Engineer | Acme", client.agent_prompts[2].split("search_jobs \"python\"")[1])
        self.assertEqual(r["retrieval"]["screened_jobs"], 3)

    def test_llm_error_falls_back(self):
        r, _ = self.run_agent([RuntimeError("down")])
        self.assertEqual(r["method"], "sqlite_bm25_parent_child_then_fake")
        self.assertTrue(r["agent"]["fallback"].startswith("llm_error"))
        self.assertEqual(len(r["matches"]), 3)

    def test_step_limit_falls_back(self):
        r, client = self.run_agent([act("search_jobs", query="python"), act("search_jobs", query="fastapi")], max_steps=2)
        self.assertEqual(r["agent"]["fallback"], "max_steps")
        self.assertIn("last step", client.agent_prompts[1])

    def test_token_budget_falls_back(self):
        r, _ = self.run_agent([act("search_jobs", query="python"), act("search_jobs", query="fastapi")], max_tokens=150)
        self.assertEqual(r["agent"]["fallback"], "token_budget")

    def test_empty_submit_declines_without_scoring(self):
        client = Router([act("search_jobs", query="pastry"), act("submit", job_ids=[])])
        client.scorer = None  # a scoring call would fail: declining must not score anything
        r = agent_match("Pastry chef with eight years in hotel kitchens. " * 5, self.index, client, top_k=3, shortlist=3)
        self.assertEqual(r["matches"], [])
        self.assertEqual((r["method"], r["agent"]["fallback"], r["agent"]["steps"]), ("agent_bm25_then_fake", None, 2))
        self.assertEqual(r["retrieval"]["shortlist"], [])
        self.assertIn("no archive job", r["resume_summary"].lower())
        self.assertIn("submit an empty job_ids list", client.agent_prompts[0] + agent_system(3, 3))

    def test_lower_bound_capped_by_archive_size(self):
        r, client = self.run_agent([act("search_jobs", query="python"), THREE], top_k=5, shortlist=7)
        self.assertIsNone(r["agent"]["fallback"])
        self.assertIn("3 to 7", client.agent_prompts[0])


class ArchiveWeb(ArchiveBase):
    def call(self, client, body):
        server = make_server(self.index, client, 0, output_root=self.root / "matches")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/match",
                                         data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_web_defaults_to_agent(self):
        status, body = self.call(Router([act("search_jobs", query="python"), THREE]), {"resume_text": RESUME, "top_k": 3})
        self.assertEqual(status, 200)
        self.assertTrue(body["method"].startswith("agent_bm25"))
        self.assertEqual(body["agent"]["steps"], 2)

    def test_web_pipeline_on_request(self):
        status, body = self.call(Router([]), {"resume_text": RESUME, "top_k": 3, "method": "pipeline"})
        self.assertEqual(status, 200)
        self.assertTrue(body["method"].startswith("sqlite_bm25"))

    def test_web_rejects_unknown_method(self):
        status, body = self.call(Router([]), {"resume_text": RESUME, "top_k": 3, "method": "magic"})
        self.assertEqual((status, body["error"]), (400, "Method must be agent or pipeline."))


if __name__ == "__main__":
    unittest.main()
