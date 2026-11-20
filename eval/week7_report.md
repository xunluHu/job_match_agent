# Week 7 — An eval harness for the JobMatch agent

Team: _Name 1 (NetID)_ · _Name 2 (NetID)_

## What we evaluate

**System.** JobMatch "New US jobs" ranks recent US new-grad and internship postings for one resume. The **agent** is a ReAct loop that calls `search_jobs`, `read_job` and `submit` (the top 10, or nothing when no job fits). All runs use one frozen snapshot of 953 postings; each task's candidate pool holds 31–135 of them.

**Non-agentic baselines.**
- **keyword**: BM25 plus skill overlap, with no model call.
- **score_all**: the model scores every posting in batches of 10. There is no loop and there are no tools.

**Models.** All three methods use `claude-haiku-5-5`. Rubric tasks are graded by `claude-sonnet-5-5`.

**Files.** `eval/tasks.jsonl` (50 tasks) and `src/jobmatch_data/latest_tasks.py`, the evaluation script. One command runs every method on every task:

```
python -m jobmatch_data latest-tasks run data/latest_eval/snapshots/20261119-1813.sqlite3 --provider claude --key-file KEY
```

## Tasks (lecture format: `id`, `type`, `question`, `expected` / `expected_call` / `rubric`)

| type | n | the question | passes when |
|---|---|---|---|
| exact | 24 | a resume aimed at a known posting | every expected posting is in the top *k* (k = 3, 5 or 10 per task) |
| tool | 8 | a resume with one clear core skill | the first call is `search_jobs` with a query containing the core skill |
| rubric | 13 | a resume with an open-ended fit | the judge says yes to every task criterion and to the standard rubric: Correct, Grounded, Complete, In policy |
| unanswerable | 5 | a pastry chef, cosmetologist, dental hygienist, ballet dancer or funeral director | the method returns no jobs |

Every task also fails on results that are invalid, duplicated or outside the US, and on any "forbidden" posting that reaches the top *k*.

There are 6 edge cases:
- a Chinese-language resume
- a sponsorship filter that removes the obvious match
- a prompt injection inside a job description
- a prompt injection inside a resume
- a resume longer than 6,000 characters
- a near-empty resume

## Results

| method | runs | accuracy (all 50) | accuracy (no tool tasks, 42) | avg turns | avg latency (s) | p95 latency (s) | avg tokens (in / out) |
|---|---|---|---|---|---|---|---|
| keyword | 1 | 36% | 43% | 0.0 | 0.0 | 0.0 | 0 |
| score_all | 1 | 44% | 52% | 8.1 | 35.9 | 58.9 | 71.9k (66.0k / 5.9k) |
| **agent** | 3 | **75%** (74% / 74% / 76%) | **70%** | 9.4 | 22.9 | 37.7 | 54.8k (51.4k / 3.4k) |

| task type | keyword | score_all | agent (3 runs) |
|---|---|---|---|
| exact (24) | 67% | 92% | 100% |
| tool (8) | n/a | n/a | 100% |
| rubric (13) | 15% | 0% | 3% |
| unanswerable (5) | 0% | 0% | 100% |
| edge cases (6) | 50% | 83% | 89% |

**Agent consistency over 3 runs.**
- **pass@3 = 76%**: at least 1 of 3 runs passes.
- **pass^3 = 74%**: all 3 runs pass.
- Only one task changes outcome across runs: **t42** (the near-empty resume, ✗ ✗ ✓).

The agent made no invalid tool calls and broke no constraints in any of its 150 runs, and it never fell back to keyword ranking.

**Noise.** With 50 tasks, one standard error on the agent's accuracy is about ±6 points.

### Why the rubric tasks fail

Passing needs a "yes" on every criterion, as the lecture's standard rubric says. Counting which criteria failed shows where the methods differ:

| failed criterion | keyword (13 runs) | score_all (13) | agent (39) |
|---|---|---|---|
| task's own criteria (the ranking itself) | 9 (69%) | 8 (62%) | 12 (31%) |
| Correct (a clear mismatch in the top 3) | 8 (62%) | 2 (15%) | 14 (36%) |
| Grounded (reasons claim things not in the resume or job text) | 3 (23%) | 13 (100%) | 38 (97%) |

The agent's rankings meet the task criteria more often than either baseline. But its one-sentence reasons almost always over-claim. For example, a reason may say a job asks for a skill its description never mentions. The keyword baseline only lists matched skills, so its reasons are grounded, but its rankings are poor. The next step is to have the agent quote the requirement each reason relies on.

### Edge cases

- Neither prompt injection moved the injected posting into the top 5, for any method.
- On the Chinese resume, keyword failed and both model-based methods passed.
- The long-resume task did not isolate truncation as intended. Its first line already mentions an "embedded storage internship", so the model methods found the SSD roles anyway. We will rewrite this task in the 200-task version.

### Grading notes

- **Judge calibration (4-task pilot, before the full run).** The judge failed postings for pay, benefits and contract length. It also failed reasons it could not check in middle-truncated descriptions. Before the full run we limited "Correct" to technical fit and gave the judge full descriptions. One judge, stronger than the agent's model, grades every method.
- **Tool tasks.** These only apply to the agent: the baselines make no tool calls. They count as failures in "all 50" and are left out of "no tool tasks".
- **Cost.** The full run cost about $5: about $1.0 for the agent, $0.5 for score_all, and the rest for the judge.

## Failure trace: t45, agent run 0

```
python -m jobmatch_data latest-tasks trace RESULTS.jsonl t45
```

**The task.** t45 is a rubric task. The candidate is a front-end developer who builds React apps and SVG/Canvas graphics. In run 0 the agent failed the "Correct" and "Grounded" criteria.

**What went wrong, step by step.**
1. In step 1 the agent folded "SVG Canvas graphics" into its first search. BM25 matched "graphics" and "interactive" in Blizzard's Animation & Cinematic Tools internship.
2. In step 2 the agent spent its first read on that posting. The description asks for Python and DCC (Maya) pipeline tooling, which the resume does not show. The agent did not change its view.
3. When it submitted in step 12, it ranked Blizzard #3 with a score of 66. The reason credits the candidate with a "graphics and interactive rendering background", an association drawn from the word "graphics" rather than from a requirement.
4. It never read Quest Analytics' internship, whose description asks for React front-end work. It ranked that job #7 from its "C# – ASP.NET" title alone.
5. Its read of Kira Learning was cut off by the 2,500-character description excerpt before the React requirements. The agent noted this in its thought but ranked the job anyway.

**Root causes.**
- A search that mixed design words with the core skill.
- Ranking unread jobs by title.
- Excerpts that hide the middle of long descriptions.
- Having to submit exactly 10 jobs from a pool with about four strong front-end fits, which pushes weak fits into the top 3.

**Fixes to try.** One search per skill cluster, reasons that must quote the matched requirement, and letting the agent return fewer than 10 jobs.

## Also prepared (not run for this assignment)

We also turned the archive matcher, which did one BM25 search and one model call (classic RAG), into an agent. We prepared 20 tasks for it in `eval/archive_tasks.jsonl`. They run with `latest-tasks run ... --system archive` and compare that single-search pipeline against the archive agent.

A free retrieval check is already telling: for 5 of the 12 exact archive tasks, the pipeline's single search never retrieves the right posting. Agent-style queries such as "SLAM 3D reconstruction" do retrieve it.
