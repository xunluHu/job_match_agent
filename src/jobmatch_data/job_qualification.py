"""Conservative role and experience signals for job retrieval."""
import re
from datetime import date

from .matching import ROLE_PATTERNS


ROLE_TITLES = {
    "architecture": r"\b(?:solutions?|software|systems?|cloud|enterprise|ai|data) architect\b",
    "ml": r"\b(?:ai|ml|ai/ml|machine learning)\b",
    "mobile": r"\b(?:android|ios|mobile)\b",
    "full_stack": r"\bfull[ -]?stack\b",
    "frontend": r"\bfront[ -]?end\b|\b(?:web developer|ui engineer)\b",
    "backend": r"\bback[ -]?end\b",
    **{key: pattern for key, pattern in ROLE_PATTERNS.items() if key in ("data", "devops", "embedded")},
}
SENIORITY = re.compile(r"\b(intern(?:ship)?|entry[ -]level|junior|jr\.?|mid[ -]level|senior|sr\.?|staff|principal|lead)\b", re.I)
SUMMARY = re.compile(r"(?:professional |career )?(?:summary|profile|objective)\s*:?", re.I)
YEARS = re.compile(
    r"(?<![\w.])(\d{1,2})(?:\s*[-–]\s*\d{1,2})?\s*\+?\s*years?\s+(?:of\s+)?"
    r"(?:(?:relevant|related|professional|work|industry|hands-on|software development|technical)\s+)*experience\b", re.I
)
PRACTICE_YEARS = re.compile(r"(?<![\w.])(\d{1,2})(?:\s*[-–]\s*\d{1,2})?\s*\+?\s*years?\s+of\s+(?:building|developing|shipping|designing|implementing)\b", re.I)
WORK = re.compile(r"(?:(?:professional|work|employment|relevant) )?(?:experience|history)|internships?", re.I)
MONTHS = {name: month for month, name in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
MONTH = r"(?:Jan\w*|Feb\w*|Mar\w*|Apr\w*|May|Jun\w*|Jul\w*|Aug\w*|Sep\w*|Oct\w*|Nov\w*|Dec\w*)"
MONTH_DATE = rf"(?:{MONTH}\.?\s+\d{{4}}|(?:0?[1-9]|1[0-2])/\d{{4}})"
DATE_RANGE = re.compile(rf"(?P<start>{MONTH_DATE})\s*(?:[-–—]|\bto\b)\s*(?P<end>{MONTH_DATE}|present|current|now)\b", re.I)
OCCUPATION = re.compile(r"\b(?:engineer(?:ing)?|developer|analyst|scientist|architect|intern(?:ship)?|consultant)\b", re.I)
DEGREES = {
    "doctorate": r"\b(?:ph\.?d\.?|doctorate|doctoral)\b",
    "master": r"\b(?:master(?:'s|’s)?|m\.?tech|m\.s\.?|mba)\b",
    "bachelor": r"\b(?:bachelor(?:'s|’s)?|b\.?tech|b\.s\.?|b\.e\.?|bsc)\b",
    "associate": r"\bassociate(?:'s|’s)?\s+(?:of|degree)\b",
}


def role_family(title):
    return next((family for family, pattern in ROLE_TITLES.items() if re.search(pattern, title, re.I)), None)


def seniority(title):
    match = SENIORITY.search(title)
    if not match:
        return None
    value = match[0].lower().rstrip(".").replace(" ", "-")
    return {"sr": "senior", "jr": "junior", "internship": "intern", "entry-level": "junior"}.get(value, value)


def resume_sections(lines):
    sections = {"": []}
    heading = ""
    for line in lines:
        label = line.lstrip("# ").strip().rstrip(":")
        if line.startswith("#") or WORK.fullmatch(label) or SUMMARY.fullmatch(label) or label.lower() == "education":
            heading = label.lower()
            sections.setdefault(heading, [])
        else:
            sections[heading].append(line)
    return sections


def month_number(value):
    if "/" in value:
        month, year = map(int, value.split("/"))
    else:
        name, year = value.lower().split()
        month, year = MONTHS[name[:3]], int(year)
    return year * 12 + month


def experience_timeline(sections, as_of):
    current = as_of.year * 12 + as_of.month
    covered = {"professional": set(), "internship": set()}
    evidence = {"professional": [], "internship": []}
    roles = []
    for heading, lines in sections.items():
        if not WORK.fullmatch(heading):
            continue
        for index, line in enumerate(lines):
            ranges = list(DATE_RANGE.finditer(line))
            # Mixed entries on one OCR line cannot reliably attribute dates to a title.
            if len(ranges) != 1:
                continue
            for match in ranges:
                before = line[:match.start()].strip(" *|—–-")
                adjacent = [item for item in lines[max(0, index - 1):index] + lines[index + 1:index + 2]
                            if not item.startswith(("-", "* ", "•")) and len(item) <= 120
                            and OCCUPATION.search(item) and not DATE_RANGE.search(item)]
                context = before if OCCUPATION.search(before) else next(iter(adjacent), before)
                if re.search(r"\b(?:intern to|promoted|volunteer|personal project|academic project)\b", context, re.I):
                    continue
                kind = "internship" if re.search(r"\bintern(?:ship)?\b", f"{heading} {context}", re.I) else "professional"
                start = month_number(match["start"])
                end = current if match["end"].lower() in {"present", "current", "now"} else month_number(match["end"])
                if not 1900 * 12 < start < end <= current:
                    continue
                covered[kind].update(range(start, end))
                evidence[kind].append(line if len(line) <= 240 else match[0])
                if context in adjacent:
                    evidence[kind].append(context)
                if role_family(context):
                    roles.append((kind == "professional", end, context))
    months = {key: len(value) or None for key, value in covered.items()}
    latest_role = max(roles, default=(False, 0, ""), key=lambda item: item[:2])[2]
    return months, evidence, latest_role


def candidate_profile(resume, as_of=None):
    lines = [line.strip() for line in resume.splitlines() if line.strip()]
    start = next((i for i, line in enumerate(lines) if SUMMARY.fullmatch(line.lstrip("# "))), None)
    if start is not None:
        summary = []
        for line in lines[start + 1:]:
            if line.startswith("#"):
                break
            summary.append(line)
        focus = " ".join(summary)[:1500]
    else:
        focus = " ".join(lines[:2])[:550]
    intro = re.split(r"\b(?:with|experienced in|specializing in)\b|[.!?]\s", focus, maxsplit=1, flags=re.I)[0]
    sections = resume_sections(lines)
    months, timeline, latest_role = experience_timeline(sections, as_of or date.today())
    years = YEARS.search(focus) if start is not None else None
    if years and re.search(r"\b(?:internship|personal projects?|academic projects?)\b", focus[:years.end()], re.I):
        years = None
    education = [(level, line) for level, pattern in DEGREES.items() for line in sections.get("education", [])
                 if re.search(pattern, line, re.I)]
    level, degree_line = next(iter(education), (None, None))
    return {"role_family": role_family(intro) or role_family(latest_role),
            "seniority": seniority(intro) or seniority(latest_role),
            "role_evidence": intro if role_family(intro) else latest_role or None,
            "reported_years": int(years[1]) if years else None,
            "experience_evidence": years[0] if years else None,
            "professional_experience_months": months["professional"],
            "internship_experience_months": months["internship"],
            "experience_timeline_evidence": timeline,
            "education_level": level, "education_evidence": degree_line}, focus or latest_role


def job_profile(title, sections):
    requirements = []
    degree_requirements = []
    for label, passage in sections:
        for line in passage.splitlines():
            preferred = re.search(r"preferred|desired|nice|bonus|additional", f"{label} {line}", re.I)
            mandatory = re.search(r"qualifications|requirements|must|what you.*need", label, re.I)
            explicit_required = re.search(r"\b(?:minimum|required|must have|at least)\b", line, re.I)
            if preferred or mandatory or explicit_required:
                for level, pattern in DEGREES.items():
                    if re.search(pattern, line, re.I):
                        priority = "preferred" if preferred else "unclear" if re.search(
                            r"\b(?:or|equivalent)\b", line, re.I) else "required"
                        degree_requirements.append({"level": level, "priority": priority, "jd_quote": line})
                        break
            if re.search(r"preferred|desired|nice|bonus|additional|benefits", label, re.I):
                continue
            if re.search(r"\b(?:preferred|desired|bonus|or|equivalent|company|over \d+ years of experience serving)\b", line, re.I):
                continue
            match = YEARS.search(line) or (PRACTICE_YEARS.search(line) if mandatory else None)
            if match and (mandatory or explicit_required):
                requirements.append((int(match[1]), line))
    minimum, evidence = max(requirements, default=(None, None), key=lambda pair: pair[0])
    return {"role_family": role_family(title), "seniority": seniority(title),
            "min_years": minimum, "experience_evidence": evidence,
            "degree_requirements": degree_requirements[:3]}


def qualification(candidate, job):
    years, minimum = candidate["reported_years"], job["min_years"]
    months = max((m for m in (years * 12 if years is not None else None,
                              candidate.get("professional_experience_months")) if m is not None), default=None)
    if minimum is None:
        experience = "requirement_unknown"
    elif months is None:
        experience = "candidate_years_unknown"
    elif months < minimum * 12:
        experience = "gap"
    else:
        experience = "reported_years_meet_minimum"

    target, family = candidate["role_family"], job["role_family"]
    adjacent = (target in {"frontend", "backend", "full_stack"}
                and family in {"frontend", "backend", "full_stack"} and "full_stack" in (target, family))
    role = "match" if target and target == family else "adjacent" if adjacent else "unknown" if not target or not family else "different"
    advanced = {"senior", "staff", "principal", "lead"}
    title_review = (job["seniority"] in advanced or family == "architecture") and candidate["seniority"] not in advanced
    return dict(job, role_match=role, experience_status=experience, seniority_review=title_review)


def screening_order(screen):
    role_order = {"match": 0, "adjacent": 1, "unknown": 2, "different": 3}
    experience_order = {"reported_years_meet_minimum": 0, "requirement_unknown": 1,
                        "candidate_years_unknown": 2, "gap": 3}
    experience = screen["experience_status"]
    return experience == "gap", role_order[screen["role_match"]], screen["seniority_review"], experience_order[experience]
