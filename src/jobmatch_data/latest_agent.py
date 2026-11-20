"""ReAct ranking agent: search and read job descriptions by id, then submit the top matches."""
import json

from .agent_loop import Step, run_agent
from .latest_rankers import TECH_ONLY, UNTRUSTED, bm25_scores, jd_excerpt, keyword_rank, result
from .latest_rules import in_us

SYSTEM = (f"You rank recent US job postings for one candidate. {UNTRUSTED} {TECH_ONLY} "
          "Use only job ids shown to you (like J007). Respond with exactly one action per turn. "
          "Use search_jobs to find jobs whose descriptions match a query, and read_job to read a description "
          "before you rank a job highly. "
          "Submit exactly {n} distinct jobs, best first, each with a 0-100 score and a one-sentence reason. "
          "If no job in the catalog fits the candidate's field at all, submit an empty items list instead.")

ACTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["thought", "action", "query", "job_id", "items"],
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": ["search_jobs", "read_job", "submit"]},
        "query": {"type": "string"},
        "job_id": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["job_id", "score", "reason"],
            "properties": {"job_id": {"type": "string"},
                           "score": {"type": "integer", "minimum": 0, "maximum": 100},
                           "reason": {"type": "string"}}}},
    },
}
SEARCH_LIMIT = 15


def validate_submit(items, ids, n):
    if not isinstance(items, list):
        return ["items must be a list."]
    # An empty list is the agent declining: nothing in the catalog fits the candidate's field.
    errors = [] if len(items) in (0, n) else [f"Submit exactly {n} jobs, or none if nothing fits; you sent {len(items)}."]
    seen = set()
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            errors.append(f"Item {number} is not an object.")
            continue
        job_id, score, reason = item.get("job_id"), item.get("score"), item.get("reason")
        if job_id not in ids:
            errors.append(f"Item {number} has an unknown job id.")
        elif job_id in seen:
            errors.append(f"Item {number} is a duplicate job id {job_id}.")
        seen.add(job_id)
        if type(score) is not int or not 0 <= score <= 100:
            errors.append(f"Item {number} score must be an integer from 0 to 100.")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
            errors.append(f"Item {number} reason must be 1 to 300 characters.")
    return errors


def _line(job_id, job, locations=True):
    parts = [job_id, job["title"], job["company"]]
    if locations:
        parts.append("; ".join(x for x in job["locations"] if in_us(x)) or "US")
    return " | ".join(parts)


def agent_rank(pool, resume, client, n=10, max_steps=40, max_reads=30, max_tokens=250_000):
    ids = {f"J{i:03d}": job["key"] for i, job in enumerate(pool, 1)}
    jobs = {job_id: job for job_id, job in zip(ids, pool)}
    n = min(n, len(pool))
    resume = resume[:6000]
    descriptions = [job["description"] for job in pool]
    static = ("RESUME (untrusted data):\n<<<\n" + resume + "\n>>>\n\n"
              f"JOB CATALOG ({len(pool)} jobs; id | title | company | US locations):\n"
              + "\n".join(_line(job_id, job) for job_id, job in jobs.items())
              + "\n\nACTIONS:\n"
              f"- search_jobs(query): up to {SEARCH_LIMIT} jobs whose descriptions best match the query.\n"
              "- read_job(job_id): the job's description (long ones are shortened in the middle).\n"
              f"- submit(items): your final top {n}, best first.\n"
              f"Budgets: {max_steps} steps and {max_reads} read_job calls in total.\n")
    def search(action, reads_left):
        query = str(action.get("query") or "")[:300]
        scores = bm25_scores(query, descriptions)
        order = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: -scores[i])[:SEARCH_LIMIT]
        keys = list(jobs)
        observation = "\n".join(_line(keys[i], pool[i], False) for i in order) or "No matching jobs."
        return Step(f"search_jobs {json.dumps(query)}", observation, {"query": query})

    def read(action, reads_left):
        job_id = action.get("job_id")
        if job_id not in jobs:
            message = "Unknown job id. Use an id from the catalog, like J007."
            return Step("read_job (invalid id)", message, error=message)
        if reads_left <= 0:
            message = "Read budget exhausted. Rank with what you have and submit."
            return Step(f"read_job {job_id}", message, {"job_id": job_id}, message)
        observation = _line(job_id, jobs[job_id]) + "\n" + jd_excerpt(jobs[job_id]["description"])
        return Step(f"read_job {job_id}", observation, {"job_id": job_id}, is_read=True)

    def submit(action, reads_left):
        items = action.get("items")
        errors = validate_submit(items, ids, n)
        args = {"items": len(items) if isinstance(items, list) else 0}
        if errors:
            error = "; ".join(errors)
            return Step(f"submit ({args['items']} items)", "Submit rejected: " + error, args, error)
        return Step("submit", args=args, done=[{"key": ids[i["job_id"]], "score": i["score"],
                                                "reason": i["reason"].strip()} for i in items])

    matches, meta = run_agent(client, SYSTEM.format(n=n), static,
                              {"search_jobs": search, "read_job": read, "submit": submit}, ACTION_SCHEMA,
                              max_steps, max_reads, max_tokens)
    if meta["fallback"]:  # an LLM error or a spent budget: rank with keywords instead
        matches = keyword_rank(pool, resume, n)["matches"]
    return result(matches, meta["calls"], meta["input_tokens"], meta["output_tokens"], meta["trace"], meta["fallback"])
