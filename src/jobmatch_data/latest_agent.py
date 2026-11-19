"""ReAct ranking agent: search and read job descriptions by id, then submit the top matches."""
import json

from .latest_rankers import TECH_ONLY, UNTRUSTED, bm25_scores, jd_excerpt, keyword_rank, result, usage_tokens
from .latest_rules import in_us

SYSTEM = (f"You rank recent US job postings for one candidate. {UNTRUSTED} {TECH_ONLY} "
          "Use only job ids shown to you (like J007). Respond with exactly one action per turn. "
          "Use search_jobs to find jobs whose descriptions match a query, and read_job to read a description "
          "before you rank a job highly. "
          "Submit exactly {n} distinct jobs, best first, each with a 0-100 score and a one-sentence reason.")

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
OLD_READ_CHARS = 200


def validate_submit(items, ids, n):
    if not isinstance(items, list):
        return ["items must be a list."]
    errors = [] if len(items) == n else [f"Submit exactly {n} jobs; you sent {len(items)}."]
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
    history, trace = [], []
    calls = input_tokens = output_tokens = reads = 0

    def fall_back(reason):
        ranked = keyword_rank(pool, resume, n)
        return result(ranked["matches"], calls, input_tokens, output_tokens, trace, reason)

    for step in range(1, max_steps + 1):
        rendered = []
        for index, (header, observation, is_read) in enumerate(history):
            if is_read and index < len(history) - 1 and len(observation) > OLD_READ_CHARS:
                observation = observation[:OLD_READ_CHARS] + " …(truncated)"
            rendered.append(f"{header}\nObservation: {observation}")
        prompt = (static + "\nHISTORY:\n" + ("\n\n".join(rendered) if rendered else "(none yet)")
                  + f"\n\nSteps left: {max_steps - step + 1}, reads left: {max(max_reads - reads, 0)}")
        if step == max_steps:
            prompt += "\nThis is your last step: submit now."
        try:
            action, metadata = client.generate(SYSTEM.format(n=n), prompt, ACTION_SCHEMA)
        except Exception as exc:  # any client failure (HTTP, network, bad JSON) ends the run with keyword ranking
            return fall_back(f"llm_error: {type(exc).__name__}: {exc}")
        calls += 1
        used_in, used_out = usage_tokens(metadata)
        input_tokens += used_in
        output_tokens += used_out
        kind = action.get("action") if isinstance(action, dict) else None
        error, is_read, args = None, False, {}
        thought = str(action.get("thought") or "")[:500] if isinstance(action, dict) else ""
        if kind == "search_jobs":
            query = str(action.get("query") or "")[:300]
            args = {"query": query}
            scores = bm25_scores(query, descriptions)
            order = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: -scores[i])[:SEARCH_LIMIT]
            keys = list(jobs)
            observation = "\n".join(_line(keys[i], pool[i], False) for i in order) or "No matching jobs."
            header = f"Step {step}: search_jobs {json.dumps(query)}"
        elif kind == "read_job":
            job_id = action.get("job_id")
            if job_id not in jobs:
                error = observation = "Unknown job id. Use an id from the catalog, like J007."
                header = f"Step {step}: read_job (invalid id)"
            elif reads >= max_reads:
                error = observation = "Read budget exhausted. Rank with what you have and submit."
                header = f"Step {step}: read_job {job_id}"
                args = {"job_id": job_id}
            else:
                reads += 1
                is_read = True
                args = {"job_id": job_id}
                observation = _line(job_id, jobs[job_id]) + "\n" + jd_excerpt(jobs[job_id]["description"])
                header = f"Step {step}: read_job {job_id}"
        elif kind == "submit":
            items = action.get("items")
            errors = validate_submit(items, ids, n)
            args = {"items": len(items) if isinstance(items, list) else 0}
            if not errors:
                trace.append({"step": step, "action": kind, "args": args, "error": None})
                matches = [{"key": ids[i["job_id"]], "score": i["score"], "reason": i["reason"].strip()} for i in items]
                return result(matches, calls, input_tokens, output_tokens, trace, None)
            error = "; ".join(errors)
            observation = "Submit rejected: " + error
            header = f"Step {step}: submit ({args['items']} items)"
        else:
            error = observation = "Unknown action. Use search_jobs, read_job or submit."
            header = f"Step {step}: invalid action"
        trace.append({"step": step, "action": kind, "args": args, "error": error})
        history.append((f"Thought: {thought}\n{header}" if thought else header, observation, is_read))
        if input_tokens + output_tokens > max_tokens:
            return fall_back("token_budget")
    return fall_back("max_steps")
