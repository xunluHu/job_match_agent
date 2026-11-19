"""Claude client with the same generate() interface as the OpenAI and Gemini clients.

Needs the optional `anthropic` package (pip install anthropic). Nothing else in JobMatch imports it.
"""
import json
import os

from .openai_client import strict_schema

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Claude's JSON outputs reject these keywords; callers validate scores and lengths in code.
UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
               "minLength", "maxLength", "minItems", "maxItems", "uniqueItems"}


def claude_schema(schema):
    def clean(node):
        node = {k: v for k, v in node.items() if k not in UNSUPPORTED}
        if "properties" in node:
            node["properties"] = {name: clean(child) for name, child in node["properties"].items()}
        if isinstance(node.get("items"), dict):
            node["items"] = clean(node["items"])
        for key in ("anyOf", "allOf"):
            if key in node:
                node[key] = [clean(child) for child in node[key]]
        return node
    return strict_schema(clean(schema))


class ClaudeClient:
    provider = "claude"
    compact = True

    def __init__(self, model=None, api_key=None, max_tokens=16000, effort="medium", sdk=None):
        if sdk is None:
            try:
                import anthropic as sdk
            except ImportError:
                raise ValueError("Claude needs the anthropic package. Run: pip install anthropic") from None
        self.sdk = sdk
        self.client = sdk.Anthropic(api_key=api_key) if api_key else sdk.Anthropic()
        self.model = model or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.effort = effort

    def generate(self, system, prompt, schema):
        sdk = self.sdk
        try:
            # fallbacks="default": if a safety classifier declines, the API retries on a suitable model in the same call.
            response = self.client.beta.messages.create(
                model=self.model, max_tokens=self.max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": claude_schema(schema)}},
                betas=[FALLBACK_BETA], fallbacks="default")
        except sdk.AuthenticationError as exc:
            raise RuntimeError("Claude rejected the API key. Check ANTHROPIC_API_KEY.") from exc
        except sdk.RateLimitError as exc:
            raise RuntimeError("Claude rate limit reached. Try again shortly.") from exc
        except sdk.APIStatusError as exc:
            raise RuntimeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except sdk.APIConnectionError as exc:
            raise RuntimeError("Could not reach the Claude API. Check the network connection.") from exc
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise RuntimeError(f"Claude declined this request ({category or 'no category'}).")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude's reply was cut off at the output limit.")
        text = next((block.text for block in response.content if block.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            raise RuntimeError("Claude's reply was not valid JSON.") from None
        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        return data, {"provider": "claude", "model": response.model, "usage": usage}


def make_claude_client(key_file, model):
    api_key = key_file.read_text(encoding="utf-8").strip() if key_file else None
    if not api_key and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise ValueError("Set ANTHROPIC_API_KEY or pass --key-file to use Claude.")
    return ClaudeClient(model=model, api_key=api_key)
