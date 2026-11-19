"""Shared ranking helpers for the latest-jobs feature, the keyword baseline and result presentation.

A ranker returns a RankResult dict:
{"matches": [{"key", "score", "reason"}], "calls", "input_tokens", "output_tokens", "trace", "fallback"}
"""
import math
from collections import Counter

from .job_retrieval import terms
from .latest_rules import in_us, matched_skills
from .latest_store import JOB_TYPES, pool
from .matching import ALIASES, skill_span


def jd_excerpt(text):
    return text if len(text) <= 2500 else text[:1800] + "\n...\n" + text[-700:]


def usage_tokens(metadata):
    usage = metadata.get("usage") or {}
    if "promptTokenCount" in usage or "candidatesTokenCount" in usage:
        return (usage.get("promptTokenCount") or 0,
                (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0))
    return usage.get("input_tokens") or 0, usage.get("output_tokens") or 0


def bm25_scores(query, docs, k1=1.5, b=0.75):
    tokens = [list(terms(doc)) for doc in docs]
    if not tokens:
        return []
    avgdl = sum(map(len, tokens)) / len(tokens) or 1
    df = Counter(term for doc in tokens for term in set(doc))
    scores = []
    for doc in tokens:
        tf = Counter(doc)
        score = 0.0
        for term in set(terms(query)):
            if tf[term]:
                idf = math.log(1 + (len(tokens) - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(doc) / avgdl))
        scores.append(score)
    return scores


def result(matches, calls=0, input_tokens=0, output_tokens=0, trace=None, fallback=None):
    return {"matches": matches, "calls": calls, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "trace": trace or [], "fallback": fallback}


def keyword_rank(pool, resume, n=10):
    if not pool:
        return result([])
    have = {skill for skill in ALIASES if skill_span(resume, skill)}
    job_skills = [{skill for skill in have if skill_span(job["description"], skill)} for job in pool]
    df = Counter(skill for skills in job_skills for skill in skills)
    idf = {skill: math.log((len(pool) + 1) / (df[skill] + 1)) + 1 for skill in have}
    total = sum(idf.values())
    bm25 = bm25_scores(resume, [job["description"] for job in pool])
    top = max(bm25) or 1
    scored = []
    for job, skills, text_score in zip(pool, job_skills, bm25):
        overlap = sum(idf[s] for s in skills) / total if total else 0
        names = [s for s in ALIASES if s in skills]
        scored.append((round(50 * overlap + 50 * text_score / top), job["posted"], job["key"],
                       "Matched skills: " + (", ".join(names) if names else "none")))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return result([{"key": key, "score": score, "reason": reason} for score, _, key, reason in scored[:n]])


def candidate_pool(db, profile, now):
    kinds = JOB_TYPES[profile["job_type"]]
    jobs = pool(db, kinds, 7, now, profile["needs_sponsorship"])
    if len(jobs) >= 10:
        return jobs, 7
    return pool(db, kinds, 14, now, profile["needs_sponsorship"]), 14


def present(matches, pool, resume):
    by_key = {job["key"]: job for job in pool}
    items = []
    for match in matches:
        job = by_key.get(match["key"])
        if job is None:
            continue
        items.append({"rank": len(items) + 1, "key": job["key"], "title": job["title"], "company": job["company"],
                      "locations": [x for x in job["locations"] if in_us(x)], "posted": job["posted"],
                      "url": job["url"], "degree": job["degree"], "min_years": job["min_years"], "visa": job["visa"],
                      "skills": matched_skills(resume, job["description"]), "score": match["score"],
                      "reason": match["reason"]})
    return items
