import json
import tempfile
import time
import unittest
from pathlib import Path

from jobmatch_data.latest_eval import snapshot
from jobmatch_data.latest_rankers import candidate_pool
from jobmatch_data.latest_rules import listing_rows
from jobmatch_data.latest_store import save_jd, set_meta, sync_listings
from jobmatch_data.latest_tasks import (JUDGE_SCHEMA, STANDARD_RUBRIC, consistency, grade, load_archive_tasks, load_tasks, p95,
                                        read_rows, run, show_trace, summarize, to_markdown)
from jobmatch_data.resume_match import JobIndex
from jobmatch_data.storage import save
from tests.fakes import ScriptedClient, listing
from tests.test_archive_agent import Router
from tests.test_archive_agent import act as archive_act
from tests.test_existing_regression import JOBS
from tests.test_existing_regression import RESUME as ARCHIVE_RESUME

THEN = time.time() - 30 * 86400
RESUME = ("## Summary\nBackend engineer building Python services with FastAPI and PostgreSQL.\n"
          "## Experience\nSoftware Engineer, Example Co - Jan 2022 - Jan 2025\n"
          "- Built FastAPI services on PostgreSQL and deployed them with Docker and Kubernetes\n")
CHEF = "## Summary\nPastry chef with eight years in hotel kitchens: laminated doughs, plated desserts, " * 3


def act(action, query="", items=()):
    return {"thought": "my reasoning", "action": action, "query": query, "job_id": "", "items": list(items)}


def scripted_agent(prompt):
    if "Pastry chef" in prompt:
        return act("submit")  # declines
    if "(none yet)" in prompt:
        return act("search_jobs", query="FastAPI Python")
    return act("submit", items=[{"job_id": f"J{i:03d}", "score": 90 - i, "reason": "Fit."} for i in range(1, 11)])


def all_pass(prompt):
    count = prompt.split("CRITERIA:\n", 1)[1].count("\n") + 1
    return {"verdicts": [{"number": i, "passed": True, "why": "ok"} for i in range(1, count + 1)]}


def row(task_id, passed, repeat=0, kind="exact", method="agent"):
    return {"id": task_id, "type": kind, "edge": False, "method": method, "repeat": repeat, "passed": passed,
            "detail": "ok" if passed else "missed", "calls": 2, "input_tokens": 100, "output_tokens": 10,
            "seconds": 1.0 + repeat, "fallback": None, "violations": 0, "tool_errors": 0, "steps": 2, "judge_tokens": 0}


class LatestTasks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        db = self.tmp / "latest_jobs.sqlite3"
        items = [listing(f"k{i}", f"Role {i}", f"Co {i}", f"https://job-boards.greenhouse.io/co/jobs/{i}",
                         ["Seattle, WA"], 1 + i * 0.1, THEN) for i in range(12)]
        rows = listing_rows(items, "newgrad", THEN)
        sync_listings(db, rows, THEN)
        for i, r in enumerate(rows):
            save_jd(db, r["key"], f"Python FastAPI role {i}" if i < 3 else f"Java role {i}",
                    {"degree": [], "min_years": None, "visa": "not_mentioned"})
        set_meta(db, "last_refresh", str(THEN))
        self.snap = snapshot(db, self.tmp / "snapshots", THEN)
        profile = {"job_type": "fulltime", "needs_sponsorship": False}
        self.pool = candidate_pool(self.snap, profile, THEN)[0]
        keys = [job["key"] for job in self.pool]  # newest first: Role 0 is J001
        self.tasks = [
            {"id": "t01", "type": "exact", "question": RESUME, **profile, "expected": [keys[0]], "within": 3,
             "inject": keys[11], "forbidden": [keys[11]], "tags": ["edge"]},
            {"id": "t02", "type": "tool", "question": RESUME, **profile,
             "expected_call": {"name": "search_jobs", "query_any": ["fastapi"]}},
            {"id": "t03", "type": "rubric", "question": RESUME, **profile, "rubric": ["Python roles first."]},
            {"id": "t04", "type": "unanswerable", "question": CHEF, **profile, "rubric": ["declines"]},
        ]
        self.tasks_file = self.tmp / "tasks.jsonl"
        self.tasks_file.write_text("\n".join(json.dumps(t) for t in self.tasks) + "\n", encoding="utf-8")

    def test_run_grades_resumes_and_reports(self):
        tasks = load_tasks(self.tasks_file, self.snap, THEN)
        out = self.tmp / "results.jsonl"
        judge = ScriptedClient([all_pass] * 3)
        failed = run(tasks, self.snap, ["keyword", "agent"], 2, ScriptedClient([scripted_agent] * 20), judge, out,
                     workers=1, meta={"model": "fake", "judge_model": "fake-judge"})
        self.assertEqual(failed, 0)
        rows = {(r["id"], r["method"], r["repeat"]): r for r in read_rows(out)}
        self.assertEqual(len(rows), 4 * 3)
        self.assertTrue(rows["t01", "keyword", 0]["passed"])
        self.assertEqual(rows["t02", "keyword", 0]["detail"], "no tool calls (non-agentic method)")
        self.assertTrue(rows["t02", "agent", 1]["passed"])
        self.assertFalse(rows["t04", "keyword", 0]["passed"])
        self.assertEqual(rows["t04", "agent", 0]["detail"], "declined")
        self.assertTrue(rows["t03", "agent", 0]["passed"])
        self.assertEqual(len(judge.prompts), 3)
        self.assertIn(STANDARD_RUBRIC[1], judge.prompts[0][1])
        self.assertEqual(rows["t01", "agent", 0]["trace"][0]["thought"], "my reasoning")
        self.assertEqual(rows["t01", "agent", 0]["model"], "fake")

        # Rerunning the same file skips finished runs, so a client with no script is never called.
        self.assertEqual(run(tasks, self.snap, ["keyword", "agent"], 2, ScriptedClient([]), judge, out, workers=1), 0)
        self.assertEqual(len(read_rows(out)), 12)

        summary = summarize(read_rows(out))
        agent = next(m for m in summary["main"] if m["method"] == "agent")
        self.assertEqual((agent["runs"], agent["accuracy"], agent["avg_turns"]), (2, 1, 1.75))  # the chef task declines in 1 call
        keyword = next(m for m in summary["main"] if m["method"] == "keyword")
        self.assertEqual((keyword["accuracy"], keyword["accuracy_no_tool"]), (0.5, 2 / 3))
        tool = next(g for g in summary["by_type"] if g["group"] == "tool")
        self.assertEqual((tool["keyword"], tool["agent"]), (None, 1))
        self.assertEqual(summary["consistency"]["pass_pow_k"], 1)
        text = to_markdown(summary)
        for needle in ("pass@2", "pass^2", "p95 latency", "| tool | 1 | n/a | 100% |", "edge cases"):
            self.assertIn(needle, text)
        trace = show_trace(read_rows(out), "t02")
        self.assertIn("search_jobs", trace)
        self.assertIn("my reasoning", trace)

    def test_grade_rules(self):
        keys = [job["key"] for job in self.pool]
        ranked = {"matches": [{"key": k, "score": 50, "reason": "r"} for k in keys[:10]], "trace": []}
        exact = dict(self.tasks[0], within=10)
        self.assertEqual(grade(exact, ranked, self.pool, RESUME, None)[:2], (True, "expected job(s) in the top 10"))
        self.assertFalse(grade(dict(exact, expected=[keys[10]]), ranked, self.pool, RESUME, None)[0])
        self.assertIn("forbidden", grade(dict(exact, forbidden=[keys[1]]), ranked, self.pool, RESUME, None)[1])
        duplicate = {"matches": ranked["matches"] + ranked["matches"][:1], "trace": []}
        self.assertIn("duplicate", grade(exact, duplicate, self.pool, RESUME, None)[1])
        self.assertFalse(grade(exact, {"matches": [], "trace": []}, self.pool, RESUME, None)[0])
        wrong_first = dict(ranked, trace=[{"action": "read_job", "args": {"job_id": "J001"}, "error": None}])
        self.assertEqual(grade(self.tasks[1], wrong_first, self.pool, RESUME, None)[1], "first call was read_job")
        half = {"verdicts": [{"number": 1, "passed": False, "why": "Java first"}]}
        passed, detail, _ = grade(self.tasks[2], ranked, self.pool, RESUME, ScriptedClient([half]))
        self.assertFalse(passed)
        self.assertIn("Java first", detail)
        self.assertIn("no verdict", detail)

    def test_judge_sees_whole_description_and_fit_only_rule(self):
        long_job = dict(self.pool[0], description="A" * 2000 + " MIDDLE_MARKER " + "B" * 2000)
        ranked = {"matches": [{"key": long_job["key"], "score": 50, "reason": "r"}], "trace": []}
        judge = ScriptedClient([all_pass])
        grade(self.tasks[2], ranked, [long_job], RESUME, judge)
        prompt = judge.prompts[0][1]
        self.assertIn("MIDDLE_MARKER", prompt)
        self.assertIn("pay, benefits, hours, contract length, degree and seniority are not part of fit", prompt)

    def test_judge_explains_before_its_verdict(self):
        # A verdict written before its explanation sometimes contradicted it ("all reasons are grounded", yet failed).
        item = JUDGE_SCHEMA["properties"]["verdicts"]["items"]
        self.assertEqual(list(item["properties"]), ["number", "why", "passed"])
        self.assertEqual(item["required"], ["number", "why", "passed"])

    def test_load_tasks_rejects_bad_tasks(self):
        cases = [dict(self.tasks[0], expected=["not-a-key"]), dict(self.tasks[0], type="essay"),
                 dict(self.tasks[1], expected_call={"name": "read_job", "query_any": ["x"]}),
                 dict(self.tasks[0], question="too short"), {k: v for k, v in self.tasks[0].items() if k != "job_type"}]
        for task in cases:
            self.tmp.joinpath("bad.jsonl").write_text(json.dumps(task) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_tasks(self.tmp / "bad.jsonl", self.snap, THEN)
        self.tmp.joinpath("dup.jsonl").write_text(json.dumps(self.tasks[0]) + "\n" + json.dumps(self.tasks[0]) + "\n")
        with self.assertRaises(ValueError):
            load_tasks(self.tmp / "dup.jsonl", self.snap, THEN)

    def test_consistency_pass_at_k_and_flips(self):
        rows = ([row("a", True, k) for k in range(3)] + [row("b", p, k) for k, p in enumerate([True, False, True])]
                + [row("c", False, k) for k in range(3)])
        c = consistency(rows)
        self.assertEqual((c["k"], c["pass_at_k"], c["pass_pow_k"]), (3, 2 / 3, 1 / 3))
        self.assertEqual([(f["id"], f["outcomes"]) for f in c["flips"]], [("b", "✓✗✓")])
        self.assertEqual([(f["id"], f["failed"]) for f in c["failures"]], [("b", 1), ("c", 3)])

    def test_p95(self):
        self.assertEqual(p95([5.0]), 5.0)
        self.assertAlmostEqual(p95([float(x) for x in range(1, 101)]), 95.05)
        # A later row for the same run replaces an earlier one (a rerun after an LLM-error fallback).
        rows = [row("a", False), dict(row("a", True), seconds=9.0)]
        self.assertEqual(summarize(rows)["main"][0]["accuracy"], 1)


class ArchiveTasks(unittest.TestCase):
    """The same harness on Archive match: pipeline (classic RAG) against the agent, keys are source|source_id."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        save(JOBS, "test", self.tmp)
        self.snap = self.tmp / "jobs.sqlite3"
        self.tasks = [
            {"id": "a01", "type": "exact", "question": ARCHIVE_RESUME, "expected": ["test|1"]},
            {"id": "a02", "type": "tool", "question": ARCHIVE_RESUME,
             "expected_call": {"name": "search_jobs", "query_any": ["fastapi"]}},
            {"id": "a03", "type": "rubric", "question": ARCHIVE_RESUME, "rubric": ["Backend roles first."],
             "forbidden": ["test|2"], "within": 1},
        ]
        self.file = self.tmp / "archive_tasks.jsonl"
        self.file.write_text("".join(json.dumps(t) + "\n" for t in self.tasks), encoding="utf-8")

    def agent_script(self, runs):
        three = archive_act("submit", job_ids=["J001", "J002", "J003"])
        return [archive_act("search_jobs", query="FastAPI Python"), three] * runs

    def test_archive_run_grades_pipeline_and_agent(self):
        tasks = load_archive_tasks(self.file, JobIndex(self.snap))
        out = self.tmp / "results.jsonl"
        failed = run(tasks, self.snap, ["pipeline", "agent"], 2, Router(self.agent_script(6)),
                     ScriptedClient([all_pass] * 3), out, workers=1, system="archive")
        self.assertEqual(failed, 0)
        rows = {(r["id"], r["method"], r["repeat"]): r for r in read_rows(out)}
        self.assertEqual(len(rows), 3 * 3)
        self.assertEqual(rows["a01", "pipeline", 0]["detail"], "expected job(s) in the top 5")  # archive returns top_k = 5
        self.assertTrue(rows["a01", "agent", 1]["passed"])
        self.assertEqual(rows["a02", "pipeline", 0]["detail"], "no tool calls (non-agentic method)")
        self.assertEqual(rows["a02", "agent", 0]["detail"], "first search: 'FastAPI Python'")
        self.assertEqual((rows["a01", "pipeline", 0]["calls"], rows["a01", "agent", 0]["calls"]), (1, 3))
        self.assertEqual(rows["a01", "agent", 0]["ids"]["J001"], "test|1")
        self.assertEqual(rows["a01", "agent", 0]["system"], "archive")
        text = to_markdown(summarize(read_rows(out)))
        self.assertIn("| pipeline |", text)
        self.assertIn("archive", text)
        self.assertIn("search_jobs", show_trace(read_rows(out), "a02"))

    def test_scoring_failure_is_a_failed_answer(self):
        class BrokenScorer(Router):
            def generate(self, system, prompt, schema):
                if "action" not in schema.get("properties", {}):
                    raise ValueError("The model returned an unknown job reference.")
                return super().generate(system, prompt, schema)

        tasks = load_archive_tasks(self.file, JobIndex(self.snap))[:1]
        out = self.tmp / "broken.jsonl"
        self.assertEqual(run(tasks, self.snap, ["pipeline"], 1, BrokenScorer([]), None, out, workers=1,
                             system="archive"), 0)
        row = read_rows(out)[0]
        self.assertFalse(row["passed"])
        self.assertEqual(row["detail"], "no answer: The model returned an unknown job reference.")

    def test_archive_tasks_validate_keys(self):
        self.file.write_text(json.dumps(dict(self.tasks[0], expected=["test|99"])) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_archive_tasks(self.file, JobIndex(self.snap))

    def test_archive_unanswerable_agent_declines_pipeline_cannot(self):
        chef = "## Summary\nPastry chef with eight years in hotel kitchens: laminated doughs, plated desserts. " * 3
        self.file.write_text(json.dumps({"id": "a09", "type": "unanswerable", "question": chef,
                                         "rubric": ["declines"]}) + "\n", encoding="utf-8")
        tasks = load_archive_tasks(self.file, JobIndex(self.snap))
        out = self.tmp / "chef.jsonl"
        decline = [archive_act("search_jobs", query="pastry"), archive_act("submit", job_ids=[])]
        run(tasks, self.snap, ["agent"], 1, Router(decline), None, out, workers=1, system="archive")
        row = read_rows(out)[0]
        self.assertEqual((row["passed"], row["detail"], row["calls"]), (True, "declined", 2))


if __name__ == "__main__":
    unittest.main()
