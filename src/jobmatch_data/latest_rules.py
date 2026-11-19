"""Pure rules for the latest-jobs feature: US filter, job URL parsing, listing rows and extracted fields."""
import re
from typing import NamedTuple
from urllib.parse import quote, urlsplit

from .job_qualification import job_profile
from .job_retrieval import sections
from .matching import ALIASES, skill_span

# Ported from job-apply jobs.py. State codes are case-sensitive and must follow a comma ("DE-Berlin" is Germany).
US_CODE = re.compile(r",\s*(A[LKZR]|C[AOT]|DE|DC|FL|GA|HI|I[DLNA]|K[SY]|LA|M[EDAINSOT]|N[EVHJMYCD]|O[HKR]|PA|RI"
                     r"|S[CD]|T[NX]|UT|V[TA]|W[AVIY])\b|\b(US|USA)\b")
CA_PROVINCE = re.compile(r"\b(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b")  # the CA in "Toronto, ON, CA" is Canada
US_NAME = re.compile(
    r"\b(united states|u\.s\.|sf|south sf|nyc|san francisco|new york|seattle|bay area|silicon valley|los angeles"
    r"|boston|chicago|austin|alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida"
    r"|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts"
    r"|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico"
    r"|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota"
    r"|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming)\b", re.I)
NO_SPONSOR = re.compile(
    r"\b(unable|not able|cannot|can not|will not|won't|do not|does not|don't|doesn't|not)\s+(to\s+)?"
    r"(provide\s+|offer\s+)?(visa\s+)?sponsor"
    r"|without\s+(the\s+need\s+for\s+)?(visa\s+)?sponsorship"
    r"|\bno\s+(visa\s+)?sponsorship"
    r"|not\s+eligible\s+for\s+(visa\s+)?sponsorship"
    r"|sponsorship\s+(is\s+)?not\s+(available|provided|offered)", re.I)
CITIZEN = re.compile(
    r"u\.?s\.?\s+citizen(ship)?\s+(is\s+)?required|must\s+be\s+a\s+u\.?s\.?\s+citizen|security\s+clearance"
    r"|\bts/sci\b|green\s+card\s+holders?\s+only"
    r"|\bitar\b|export\s+control|u\.?s\.?\s+persons?\b|must\s+be\s+a\s+\(i\)\s+u\.?s\.?\s+citizen", re.I)


def in_us(location):
    # ponytail: place names and state codes only; "Remote" or "Hybrid" without a place counts as non-US.
    return not CA_PROVINCE.search(location) and bool(US_CODE.search(location) or US_NAME.search(location))


class JobRef(NamedTuple):
    platform: str
    api_url: str
    jid: str


def parse_job_url(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    path = [p for p in parts.path.split("/") if p]
    if parts.scheme not in ("http", "https"):
        return None
    if (host == "greenhouse.io" or host.endswith(".greenhouse.io")) and len(path) >= 3 \
            and path[0] != "embed" and path[1] == "jobs" and path[2].isdigit():
        co, jid = quote(path[0]), path[2]
        return JobRef("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{co}/jobs/{jid}", jid)
    if host == "jobs.lever.co" and len(path) >= 2:
        co, jid = quote(path[0]), quote(path[1])
        return JobRef("lever", f"https://api.lever.co/v0/postings/{co}/{jid}", jid)
    if host == "jobs.ashbyhq.com" and len(path) >= 2:
        co, jid = quote(path[0]), path[1]
        return JobRef("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{co}", jid)
    return None


def listing_rows(listings, kind, now, days=14):
    cutoff = now - days * 86400
    rows = []
    for item in listings:
        locations = [str(x) for x in item.get("locations") or []]
        if not (item.get("active") and item.get("is_visible", True)) or (item.get("date_posted") or 0) < cutoff:
            continue
        if not any(in_us(x) for x in locations):
            continue
        ref = parse_job_url(item.get("url") or "")
        rows.append({"key": str(item["id"]), "company": item.get("company_name") or "", "title": item.get("title") or "",
                     "url": item.get("url") or "", "locations": locations, "posted": int(item["date_posted"]),
                     "kind": kind, "category": item.get("category"), "sponsorship": item.get("sponsorship"),
                     "platform": ref.platform if ref else None, "api_url": ref.api_url if ref else None,
                     "jid": ref.jid if ref else None})
    return rows


def visa_note(jd, sponsorship):
    if sponsorship == "U.S. Citizenship is Required" or CITIZEN.search(jd or ""):
        return "citizenship_required"
    if sponsorship == "Does Not Offer Sponsorship" or NO_SPONSOR.search(jd or ""):
        return "no_sponsorship"
    return "not_mentioned"


def job_fields(title, jd, sponsorship):
    profile = job_profile(title, list(sections(jd)))
    strength = {"required": 2, "preferred": 1}
    degree = {}  # one entry per level, keeping the strongest priority, in first-seen order
    for d in profile["degree_requirements"]:
        if strength.get(d["priority"], 0) >= strength.get(degree.get(d["level"], ""), 0):
            degree[d["level"]] = d["priority"]
    return {"degree": [{"level": level, "priority": priority} for level, priority in degree.items()],
            "min_years": profile["min_years"], "visa": visa_note(jd, sponsorship)}


def matched_skills(resume, jd):
    return [skill for skill in ALIASES if skill_span(resume, skill) and skill_span(jd, skill)]
