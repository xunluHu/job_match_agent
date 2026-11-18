"""Small OpenAI Responses client, with bounded output and no automatic paid retries."""
import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass
class Config:
    api_key: str = field(repr=False)
    model: str = "gpt-6-luna"


def load_config(root, key_file=None, model=None):
    key = os.environ.get("OPENAI_API_KEY")
    files = [Path(key_file)] if key_file else [Path(root) / "output/resources/openai.txt",
                                               Path(root) / "resources/openai.txt", Path(root) / ".env"]
    if not key:
        for path in files:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8-sig")
            if path.suffix == ".json":
                value = json.loads(text)
                key = value.get("OPENAI_API_KEY") or value.get("api_key")
            if not key:
                match = re.search(r"sk-[A-Za-z0-9_-]{20,}", text)
                key = match[0] if match else None
            if key:
                break
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Set OPENAI_API_KEY or add a key to output/resources/openai.txt.")
    return Config(key.strip(), model or os.environ.get("OPENAI_MODEL", "gpt-6-luna"))


def strict_schema(schema):
    schema = copy.deepcopy(schema)
    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                value["additionalProperties"] = False
                value["required"] = list(value.get("properties", {}))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema)
    return schema


class OpenAIClient:
    provider = "openai"
    compact = True

    def __init__(self, config, max_output_tokens=4000, opener=urlopen):
        if not 200 <= max_output_tokens <= 6000:
            raise ValueError("max-output-tokens must be between 200 and 6000.")
        self.config = config
        self.max_output_tokens = max_output_tokens
        self.opener = opener

    def generate(self, system, prompt, schema):
        payload = {"model": self.config.model, "instructions": system, "input": prompt,
                   "max_output_tokens": self.max_output_tokens, "store": False,
                   "text": {"format": {"type": "json_schema", "name": "resume_match", "strict": True,
                                       "schema": strict_schema(schema)}}}
        if self.config.model.startswith(("gpt-6-", "gpt-6.1-", "gpt-5.4-")):
            payload["reasoning"] = {"effort": "none"}
        request = Request("https://api.openai.com/v1/responses", data=json.dumps(payload, ensure_ascii=False).encode(),
                          headers={"Authorization": "Bearer " + self.config.api_key, "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=60) as response:
                data = json.loads(response.read())
        except HTTPError as exc:
            detail = ""
            try:
                detail = str(json.loads(exc.read()).get("error", {}).get("message", "")).replace(self.config.api_key, "[redacted]")[:350]
            except (ValueError, AttributeError):
                pass
            raise RuntimeError(f"OpenAI HTTP {exc.code}" + (f": {detail}" if detail else "")) from None
        except (URLError, TimeoutError):
            raise RuntimeError("OpenAI connection failed or exceeded 60 seconds. No automatic retry was made.") from None
        if data.get("status") != "completed":
            raise RuntimeError("OpenAI output is incomplete, possibly due to the output token limit.")
        text = "".join(part.get("text", "") for item in data.get("output", [])
                       if item.get("type") == "message" for part in item.get("content", [])
                       if part.get("type") == "output_text")
        try:
            result = json.loads(text)
        except ValueError:
            raise RuntimeError("OpenAI did not return valid structured output.") from None
        return result, {"provider": "openai", "model": data.get("model", self.config.model),
                        "usage": data.get("usage", {}), "max_output_tokens": self.max_output_tokens}
