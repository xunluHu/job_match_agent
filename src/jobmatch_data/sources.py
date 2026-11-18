"""Adapters for public ATS boards and Simplify's historical listing files."""

import hashlib
import html
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urlsplit


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "li", "div", "h1", "h2", "h3"):
            self.parts.append("\n")


def plain(value):
    parser = TextParser()
    parser.feed(html.unescape(value or ""))
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()


def simplify(client, repo, ref="dev"):
    url = f"https://raw.githubusercontent.com/SimplifyJobs/{quote(repo, safe='')}/{quote(ref, safe='')}/.github/scripts/listings.json"
    payload = client.get(url)
    if not isinstance(payload, list):
        raise ValueError("Simplify listings must be an array")
    records = []
    for item in payload:
        records.append({
            "id": str(item["id"]), "title": item["title"],
            "company": item["company_name"], "url": item["url"],
            "location": "; ".join(item.get("locations") or []),
            "posted_at": timestamp(item.get("date_posted")),
            "source_updated_at": timestamp(item.get("date_updated")),
            "active": item.get("active"), "description": None,
            "category": item.get("category"),
            "provenance": {"url": url, "repository": repo, "ref": ref,
                           "date_semantics": "Aggregator date_posted; not verified employer publication date"},
            "original": item,
        })
    return records


def discover_boards(records):
    boards = {}
    for item in records:
        if item.get("active") is not True:
            continue
        parsed = urlsplit(item["url"])
        parts = [part for part in parsed.path.split("/") if part]
        vendor = None
        board = None
        host = (parsed.hostname or "").lower()
        if host in ("boards.greenhouse.io", "job-boards.greenhouse.io") and parts:
            vendor, board = "greenhouse", parts[0]
        elif host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
            vendor, board = "lever", parts[0]
        elif host == "jobs.ashbyhq.com" and parts:
            vendor, board = "ashby", parts[0]
        if vendor and board:
            region = "eu" if host == "jobs.eu.lever.co" else "global"
            boards[(vendor, region, board)] = {
                "vendor": vendor, "board": board, "company": item["company"],
                "region": region,
            }
    return sorted(boards.values(), key=lambda x: (x["vendor"], x["board"].lower()))


def ats(client, vendor, board, company, region="global"):
    token = quote(board, safe="")
    if vendor == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        items = client.get(url)["jobs"]
    elif vendor == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
        items = client.get(url)["jobs"]
    elif vendor == "lever":
        host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
        base = f"https://{host}/v0/postings/{token}"
        items = []
        seen = set()
        for page in range(1000):
            url = base + "?" + urlencode({"mode": "json", "skip": len(items), "limit": 100})
            batch = client.get(url)
            if not isinstance(batch, list):
                raise ValueError("Lever response must be an array")
            ids = {item["id"] for item in batch}
            if ids & seen:
                raise ValueError("Lever returned overlapping pages; refusing an incomplete board")
            seen.update(ids)
            items.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ValueError("Lever exceeded pagination safety limit")
        url = base
    else:
        raise ValueError(f"Unsupported vendor: {vendor}")
    result = []
    for item in items:
        if vendor == "ashby" and item.get("isListed") is False:
            continue
        extra = {}
        if vendor == "greenhouse":
            title, link = item["title"], item["absolute_url"]
            location = (item.get("location") or {}).get("name")
            description = plain(item.get("content"))
            posted = None  # updated_at must never be treated as a publication date
            extra["source_updated_at"] = timestamp(item.get("updated_at"))
        elif vendor == "lever":
            title, link = item["text"], item["hostedUrl"]
            categories = item.get("categories") or {}
            location = categories.get("location")
            sections = [item.get("descriptionPlain") or plain(item.get("description"))]
            sections += [part.get("text", "") + "\n" + plain(part.get("content")) for part in item.get("lists", [])]
            sections.append(item.get("additionalPlain") or plain(item.get("additional")))
            description = "\n\n".join(part for part in sections if part)
            posted = None  # public API does not document a publication timestamp
            extra.update(employment_type=categories.get("commitment"),
                         workplace_type=item.get("workplaceType"), salary=item.get("salaryRange"))
        else:
            title, link = item["title"], item["jobUrl"]
            location = item.get("location")
            description = item.get("descriptionPlain") or plain(item.get("descriptionHtml"))
            posted = timestamp(item.get("publishedAt"))
            extra.update(employment_type=item.get("employmentType"),
                         workplace_type=item.get("workplaceType"), salary=item.get("compensation"))
        result.append({
            "id": str(item.get("id") or hashlib.sha256(link.encode()).hexdigest()),
            "title": title, "company": company, "url": link,
            "location": location, "description": description, "posted_at": posted,
            "active": True, "board": board,
            "provenance": {"url": url, "vendor": vendor, "board": board, "region": region},
            "original": item, **extra,
        })
    return result
