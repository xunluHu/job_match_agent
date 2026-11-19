"""Test doubles for the latest-jobs feature: a scripted LLM, a routed HTTP client and data builders."""
import urllib.error


class ScriptedClient:
    """Pops one scripted item per generate() call: a callable gets the prompt, an Exception is raised, a dict is returned."""
    provider = "fake"
    compact = True

    def __init__(self, script):
        self.script = list(script)
        self.prompts = []

    def generate(self, system, prompt, schema):
        self.prompts.append((system, prompt, schema))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(prompt)
        usage = {"input_tokens": len(system + prompt) // 4, "output_tokens": 20}
        return item, {"provider": "fake", "model": "fake", "usage": usage}


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if url not in self.routes:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        payload = self.routes[url]
        if isinstance(payload, Exception):
            raise payload
        return payload


def listing(id, title, company, url, locations, days_ago, now, active=True, visible=True, sponsorship="Other"):
    return {"id": id, "title": title, "company_name": company, "url": url, "locations": list(locations),
            "date_posted": int(now - days_ago * 86400), "active": active, "is_visible": visible,
            "sponsorship": sponsorship, "category": "Software", "terms": []}


def pool_job(key, title, company, description, posted, kind="newgrad", locations=("Seattle, WA",),
             url="https://example.com/j"):
    return {"key": key, "title": title, "company": company, "description": description, "posted": posted,
            "kind": kind, "locations": list(locations), "url": url, "degree": [], "min_years": None,
            "visa": "not_mentioned"}
