# Archive Match Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Archive match finds its shortlist with a ReAct agent (agentic RAG) and keeps the existing single scoring call, output format and pipeline fallback.

**Architecture:** A new `archive_agent.agent_match` loops over `search_jobs` / `read_job` / `submit` actions, using `SectionSearch.retrieve` with a model-written query. The submitted shortlist goes to `score_shortlist`, the scoring half of today's `match_resume`, moved into its own function unchanged. The web app and CLI default to the agent; `method: "pipeline"` keeps the old path.

**Tech Stack:** Python 3.11+ standard library, SQLite FTS5, existing `client.generate(system, prompt, schema)` interface, unittest.

**Spec:** `docs/superpowers/specs/2026-11-19-archive-agent-design.md`

## Global Constraints

- Output format unchanged: same `matches` items (evidence, capabilities, radar_svg, screening, retrieval_hit), same sort.
- Budgets: `max_steps=20`, `max_reads=12`, `max_tokens=150_000`; search shows the top 10 jobs.
- LLM error, token budget or step limit → `match_resume` (pipeline) result with `result["agent"]["fallback"]` set.
- `result["method"] = f"agent_bm25_then_{provider}"`; `result["agent"] = {"steps", "calls", "input_tokens", "output_tokens", "trace", "fallback"}`; `usage` stays the scoring call's.
- Untouched: `candidate_match`, New US jobs, scoring prompts/schemas/validation.
- `.gitignore` is a whitelist: add `!/src/jobmatch_data/archive_agent.py`.
- Run tests with `PYTHONPATH=src ../.venv/bin/python -m unittest discover -s tests -q`.

## Review Focus

- A search whose words are not in the index → observation "No matching jobs." and the loop continues (test in Task 2).
- `read_job` / `submit` with an id never shown → error observation, never reaches scoring (Task 2).
- The same posting found by two searches keeps one id (Task 2).
- Archive with fewer jobs than `top_k` → submit lower bound is `min(top_k, len(index.jobs))` (Task 2).
- An unknown `method` in `/api/match` → 400, not a silent pipeline run (Task 3).

---

### Task 1: Retrieval by query and a reusable scoring step

**Files:**
- Modify: `src/jobmatch_data/job_retrieval.py` (`SectionSearch.retrieve`)
- Modify: `src/jobmatch_data/resume_match.py` (`match_resume`, new `score_shortlist`, `JobIndex.retrieve`)
- Test: `tests/test_archive_agent.py` (new)

**Interfaces:**
- Produces: `SectionSearch.retrieve(resume, jobs, limit, query=None) -> (results, retrieval)`; with `query`, the weighted terms come from `query` (all indexed terms, top 12 by weight, no technical/focus filter), screening still from `candidate_profile(resume)`.
- Produces: `JobIndex.retrieve(resume, limit=12, query=None)` passing `query` through.
- Produces: `score_shortlist(resume, jobs, retrieval, index, client, top_k, started) -> dict` — the body of `match_resume` from `compact = ...` to `return result`, moved verbatim. `match_resume` keeps its signature and calls it.

- [ ] **Step 1: Write the failing tests** (reuse `JOBS`, `RESUME`, `FakeMatchClient` from `tests.test_existing_regression`)

```python
def test_retrieve_with_query_uses_query_terms(self):
    results, retrieval = self.index.retrieve(RESUME, 3, query="Airflow Snowflake ETL")
    self.assertEqual(results[0]["title"], "Data Engineer")
    self.assertIn("airflow", retrieval["query_terms"])
    self.assertEqual(retrieval["candidate_profile"], self.index.retrieve(RESUME, 3)[1]["candidate_profile"])

def test_retrieve_without_query_is_unchanged(self):
    self.assertEqual(self.index.retrieve(RESUME, 3), self.index.sections.retrieve(RESUME, self.index.jobs, 3))

def test_score_shortlist_matches_pipeline(self):
    jobs, retrieval = self.index.retrieve(clean_resume(RESUME), 3)
    result = score_shortlist(clean_resume(RESUME), jobs, retrieval, self.index, FakeMatchClient(), 3, time.monotonic())
    self.assertEqual([m["title"] for m in result["matches"]],
                     [m["title"] for m in match_resume(RESUME, self.index, FakeMatchClient(), 3, 3)["matches"]])
```

- [ ] **Step 2: Run** `PYTHONPATH=src ../.venv/bin/python -m unittest tests.test_archive_agent -q` — expect FAIL (`query` unexpected keyword / `score_shortlist` import error).
- [ ] **Step 3: Implement** the `query` parameter and move the scoring half into `score_shortlist`.
- [ ] **Step 4: Run** the full suite — expect all tests OK, including `test_existing_regression`.
- [ ] **Step 5: Commit** `refactor(archive): retrieval by query and a reusable scoring step`.

### Task 2: The agent loop

**Files:**
- Create: `src/jobmatch_data/archive_agent.py`
- Modify: `.gitignore` (whitelist line)
- Test: `tests/test_archive_agent.py`

**Interfaces:**
- Consumes: Task 1's `JobIndex.retrieve(resume, 10, query=q)`, `score_shortlist`, `match_resume`; `latest_rankers.usage_tokens`, `latest_rankers.jd_excerpt`; `resume_match.clean_resume`.
- Produces: `agent_match(resume, index, client, top_k=5, shortlist=7, max_steps=20, max_reads=12, max_tokens=150_000) -> dict` (same shape as `match_resume`, plus `agent`).
- Action JSON (schema `ACTION_SCHEMA`, all fields required): `thought: str`, `action: "search_jobs" | "read_job" | "submit"`, `query: str`, `job_id: str`, `job_ids: list[str]`.
- Ids: `J001`… assigned on first sight, keyed by `(source, source_id)`, stable across searches.
- Search observation line: `J007 | title | company | location | role <role_match>, JD min <min_years or ?> yr | <hit section>: <quote, 160 chars>`; empty → `No matching jobs.`
- Read observation: `J007 | title | company | location` + `jd_excerpt(description)`; reads older than the last step shortened to 200 chars in history (as `latest_agent`).
- Submit valid when `job_ids` is a list of distinct shown ids with `min(top_k, len(index.jobs)) <= len <= shortlist`; else observation `Submit rejected: <reasons>`.
- On valid submit: shortlist = shown jobs in submitted order, renumbered `job_ref` `J001…`; `retrieval = {"method": "agent_bm25", "candidate_profile", "searches": [{"query", "results"}], "reads": int, "steps": int, "query_terms": [...union...], "screened_jobs": <ids shown>, "duplicate_jobs": <sum>, "shortlist": [<same fields as pipeline's shortlist>]}`; then `score_shortlist(..., started)` and set `result["method"]`, `result["agent"]`.

- [ ] **Step 1: Write the failing tests** with a router client: requests whose `schema["properties"]` has `"action"` pop from a script; others go to `FakeMatchClient`.

```python
def test_happy_path(self):
    client = Router([act("search_jobs", query="python fastapi"), act("read_job", job_id="J001"),
                     act("submit", job_ids=["J001", "J002", "J003"])])
    r = agent_match(RESUME, self.index, client, top_k=3, shortlist=3)
    self.assertEqual(r["method"], "agent_bm25_then_fake")
    self.assertEqual((r["agent"]["steps"], r["agent"]["fallback"]), (3, None))
    self.assertEqual(r["retrieval"]["searches"][0]["query"], "python fastapi")
    self.assertEqual(len(r["matches"]), 3)

def test_bad_submit_is_sent_back(self):   # unknown id, then too few, then valid
    ... submit ["J999"], submit ["J001"], search, submit 3 valid ids
    self.assertIn("Submit rejected", client.agent_prompts[1]); self.assertIsNone(r["agent"]["fallback"])

def test_unknown_read_and_empty_search(self):
    ... read_job "J050" → "Unknown job id" in next prompt; search "zzqqxx" → "No matching jobs." in next prompt

def test_stable_ids(self):   # two searches that both hit Backend Engineer → same id in both observations

def test_llm_error_falls_back(self):
    r = agent_match(RESUME, self.index, Router([RuntimeError("down")]), top_k=3, shortlist=3)
    self.assertEqual(r["method"], "sqlite_bm25_parent_child_then_fake")
    self.assertTrue(r["agent"]["fallback"].startswith("llm_error"))

def test_step_limit_falls_back(self):   # max_steps=2, two searches → fallback "max_steps"

def test_lower_bound_capped_by_archive_size(self):   # top_k=5 on the 3-job archive, submit 3 ids → accepted
```

- [ ] **Step 2: Run** the agent tests — expect FAIL (`archive_agent` missing).
- [ ] **Step 3: Implement** `agent_match` and helpers in `archive_agent.py`; add the `.gitignore` line. System prompt states: untrusted data, technical fit only, use only shown ids, one action per turn, search with short specific queries for different parts of the resume, read before shortlisting, submit `lo`–`hi` ids best first.
- [ ] **Step 4: Run** the full suite — expect OK.
- [ ] **Step 5: Commit** `feat(archive): ReAct agent that builds the shortlist (agentic RAG)`.

### Task 3: Entry points and the page

**Files:**
- Modify: `src/jobmatch_data/webapp.py` (`/api/match`), `src/jobmatch_data/cli.py` (`match --method`), `src/jobmatch_data/resume_match.py` (`run_cli`), `src/jobmatch_data/match.html` (`retrievalDetails`, notice line), `README.md` (one paragraph under "Start the web app")
- Test: `tests/test_archive_agent.py`

**Interfaces:**
- Consumes: `agent_match` (Task 2), `match_resume`.
- Produces: `/api/match` accepts `"method": "agent" | "pipeline"` (default `"agent"`; anything else → 400 `Method must be agent or pipeline.`); CLI `--method {agent,pipeline}` default `agent`.
- Page: when `retrieval.method === "agent_bm25"`, the summary reads `Agent · <steps> steps · <n> jobs shortlisted`, the BM25 line becomes `The agent ran <searches> searches and read <reads> job descriptions before choosing the shortlist.`, and the terms line becomes `Agent searches · q1 | q2 | …`; the notice adds `agent <in+out> tokens` when `data.agent` exists; a pipeline fallback shows `Agent fell back to the standard search: <reason>`.

- [ ] **Step 1: Write the failing tests** (server from `make_server(self.index, client, 0, output_root=...)`, as in the regression test)

```python
def test_web_defaults_to_agent(self):
    status, body = call("/api/match", {"resume_text": RESUME, "top_k": 3})   # Router scripted to submit 3 ids
    self.assertEqual(status, 200); self.assertTrue(body["method"].startswith("agent_bm25"))

def test_web_pipeline_and_bad_method(self):
    self.assertTrue(call("/api/match", {"resume_text": RESUME, "top_k": 3, "method": "pipeline"})[1]["method"]
                    .startswith("sqlite_bm25"))
    self.assertEqual(call("/api/match", {"resume_text": RESUME, "top_k": 3, "method": "magic"})[0], 400)
```

- [ ] **Step 2: Run** — expect FAIL (default path is still the pipeline; `magic` accepted).
- [ ] **Step 3: Implement** the switch in `webapp.py` and `run_cli`, the `--method` flag, the page changes and the README paragraph.
- [ ] **Step 4: Run** the full suite — expect OK. Then start `serve` with the fake-free Claude key path only if the user asks; no paid call in this plan.
- [ ] **Step 5: Commit** `feat(archive): agent by default in the web app and CLI, pipeline on request`.
