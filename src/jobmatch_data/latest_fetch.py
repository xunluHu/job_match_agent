"""Fetchers for the Simplify listing files and per-job descriptions."""
from urllib.parse import quote

from .sources import plain

LISTINGS_URL = "https://raw.githubusercontent.com/SimplifyJobs/{repo}/{ref}/.github/scripts/listings.json"


def download_listings(client, repo, ref="dev"):
    payload = client.get(LISTINGS_URL.format(repo=quote(repo, safe=""), ref=quote(ref, safe="")))
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"Simplify {repo} returned no listings.")
    return payload


def extract_jd(platform, payload, jid):
    if not isinstance(payload, dict):
        return None
    if platform == "greenhouse":
        text = plain(payload.get("content"))
    elif platform == "lever":
        parts = [payload.get("descriptionPlain") or ""]
        parts += [(item.get("text") or "") + "\n" + plain(item.get("content")) for item in payload.get("lists") or []]
        parts.append(payload.get("additionalPlain") or "")
        text = "\n\n".join(p.strip() for p in parts if p.strip())
    elif platform == "ashby":
        job = next((j for j in payload.get("jobs") or [] if j.get("id") == jid), None)
        text = job and (job.get("descriptionPlain") or plain(job.get("descriptionHtml")))
    elif platform == "workday":
        info = payload.get("jobPostingInfo")
        text = plain(info.get("jobDescription")) if isinstance(info, dict) else None
    else:
        return None
    return (text or "").strip() or None


def fetch_jd(client, job, board_cache):
    url = job["api_url"]
    if job["platform"] == "ashby":
        if url not in board_cache:
            board_cache[url] = client.get(url)
        payload = board_cache[url]
    else:
        payload = client.get(url)
    return extract_jd(job["platform"], payload, job["jid"])
