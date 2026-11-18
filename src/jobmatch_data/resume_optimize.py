"""JD-specific advice and a source-preserving Markdown resume."""
import re

from .job_retrieval import sections, terms
from .matching import ALIASES, skill_span
from .resume_match import clean_resume


ACTION = re.compile(r"\b(?:built|developed|designed|implemented|deployed|optimized|engineered|"
                    r"maintained|led|created|automated|integrated|delivered|managed|wrote|used|"
                    r"pioneered|architected|leveraged|fixed|shipped|improved|launched|build|develop|"
                    r"design|implement|deploy|maintain)\b|开发|实现|设计|部署", re.I)


def optimize_resume(original, job):
    resume = clean_resume(original)
    # Only offer complete, unchanged lines for reuse in the downloadable resume.
    safe_lines = {line.strip() for line in resume.splitlines()}
    candidates = list(dict.fromkeys(
        line for line in original.splitlines()
        if line.strip() in safe_lines and 35 <= len(line) <= 500
        and not line.lstrip().startswith(("#", "|", "["))
    ))
    experiences = [(line, set(terms(line))) for line in candidates if ACTION.search(line)]
    ranked = []
    for section, passage in sections(job["description"]):
        if re.search(r"benefits|about", section, re.I):
            continue
        for requirement in passage.splitlines():
            skills = [skill for skill in ALIASES if skill_span(requirement, skill)]
            words = set(terms(requirement))
            if not skills and not ACTION.search(requirement):
                continue
            best = None
            for line, resume_words in experiences:
                shared_skills = [skill for skill in skills if skill_span(line, skill)]
                shared_words = words & resume_words
                if shared_skills or len(shared_words) >= 3:
                    score = (len(shared_skills), len(shared_words))
                    if best is None or score > best[0]:
                        best = (score, line, shared_skills)
            if best:
                score, line, shared_skills = best
                topic = ", ".join(shared_skills[:3]) or "this responsibility"
                advice = f"Highlight this example for {topic}. Clarify your contribution and add an outcome if you can verify it."
                ranked.append((score, requirement, advice, line))
            elif skills:
                named = ", ".join(skills[:3])
                advice = (f"The resume does not show a concrete work example for {named}. "
                          "Add a specific example only if it reflects your actual experience.")
                ranked.append(((0, 0), requirement, advice, None))
    suggestions, highlights = [], []
    seen = set()
    for _, requirement, advice, line in sorted(ranked, key=lambda item: item[0], reverse=True):
        key = line or advice
        if key in seen:
            continue
        seen.add(key)
        suggestions.append({"jd_requirement": requirement, "advice": advice, "resume_evidence": line})
        if line:
            highlight = re.sub(r"^\s*(?:[-*+•]|\d+\.)\s+", "", line.strip())
            if highlight not in highlights:
                highlights.append(highlight)
        if len(suggestions) == 4:
            break
    optimized = None
    if highlights:
        lines = original.splitlines(keepends=True)
        # Keep the name and contact header before the new highlights.
        insert_at = next((i for i, line in enumerate(lines) if re.match(r"^\s*#{2,}\s", line)), len(lines))
        if insert_at == len(lines):
            insert_at = next((i for i, line in enumerate(lines) if line.strip().rstrip(":").lower() in
                              {"summary", "experience", "professional experience", "education", "skills"}), len(lines))
        block = "\n\n## Relevant highlights\n\n" + "\n".join("- " + line for line in highlights) + "\n\n"
        optimized = "".join(lines[:insert_at]) + block + "".join(lines[insert_at:])
    return {"suggestions": suggestions, "optimized_markdown": optimized}
