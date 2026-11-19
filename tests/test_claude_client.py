import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from jobmatch_data.claude_client import DEFAULT_MODEL, ClaudeClient, claude_schema, make_claude_client

SCHEMA = {"type": "object", "properties": {
    "items": {"type": "array", "minItems": 1, "items": {
        "type": "object", "properties": {"score": {"type": "integer", "minimum": 0, "maximum": 100},
                                         "reason": {"type": "string", "maxLength": 300}}}}}}


class APIError(Exception):
    pass


class APIStatusError(APIError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.message, self.status_code = message, status_code


class AuthenticationError(APIStatusError):
    pass


class RateLimitError(APIStatusError):
    pass


class APIConnectionError(APIError):
    pass


def response(text='{"items": []}', stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           stop_reason=stop_reason, stop_details=stop_details, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=120, output_tokens=30))


def fake_sdk(reply=None, error=None):
    calls, inits = [], []

    def create(**kwargs):
        calls.append(kwargs)
        if error:
            raise error
        return reply or response()

    class Anthropic:
        def __init__(self, **kwargs):
            inits.append(kwargs)
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    sdk = SimpleNamespace(Anthropic=Anthropic, APIStatusError=APIStatusError, AuthenticationError=AuthenticationError,
                          RateLimitError=RateLimitError, APIConnectionError=APIConnectionError)
    return sdk, calls, inits


class ClaudeClientTests(unittest.TestCase):
    def test_generate_sends_structured_output_request(self):
        sdk, calls, _ = fake_sdk(response('{"items": [{"score": 80, "reason": "ok"}]}'))
        client = ClaudeClient(sdk=sdk)
        data, metadata = client.generate("SYSTEM", "PROMPT", SCHEMA)
        self.assertEqual(data, {"items": [{"score": 80, "reason": "ok"}]})
        self.assertEqual(metadata, {"provider": "claude", "model": "claude-opus-5-5",
                                    "usage": {"input_tokens": 120, "output_tokens": 30}})
        request = calls[0]
        self.assertEqual((request["model"], request["system"]), (DEFAULT_MODEL, "SYSTEM"))
        self.assertEqual(request["messages"], [{"role": "user", "content": "PROMPT"}])
        self.assertEqual(request["output_config"]["effort"], "medium")
        self.assertEqual(request["output_config"]["format"], {"type": "json_schema", "schema": claude_schema(SCHEMA)})
        self.assertEqual((request["betas"], request["fallbacks"]), (["server-side-fallback-2026-07-01"], "default"))
        self.assertEqual(request["max_tokens"], 16000)
        self.assertEqual((client.provider, client.compact), ("claude", True))

    def test_claude_schema_strips_unsupported_constraints(self):
        cleaned = claude_schema(SCHEMA)
        text = json.dumps(cleaned)
        for keyword in ("minimum", "maximum", "minItems", "maxLength"):
            self.assertNotIn(keyword, text)
        self.assertFalse(cleaned["additionalProperties"])
        item = cleaned["properties"]["items"]["items"]
        self.assertEqual((item["additionalProperties"], item["required"]), (False, ["score", "reason"]))
        self.assertIn("minimum", json.dumps(SCHEMA))  # the caller's schema is not modified

    def test_refusal_raises_runtime_error(self):
        details = SimpleNamespace(category="cyber", explanation="x")
        sdk, _, _ = fake_sdk(response("", stop_reason="refusal", stop_details=details))
        with self.assertRaisesRegex(RuntimeError, "declined"):
            ClaudeClient(sdk=sdk).generate("s", "p", SCHEMA)

    def test_truncated_or_invalid_output_raises_runtime_error(self):
        for reply in (response('{"items": [', stop_reason="max_tokens"), response("not json")):
            sdk, _, _ = fake_sdk(reply)
            with self.assertRaises(RuntimeError):
                ClaudeClient(sdk=sdk).generate("s", "p", SCHEMA)

    def test_sdk_errors_become_runtime_errors(self):
        for error, words in ((AuthenticationError("bad key", 401), "API key"),
                             (RateLimitError("slow down", 429), "rate limit"),
                             (APIStatusError("overloaded", 529), "529"),
                             (APIConnectionError("offline"), "reach")):
            sdk, _, _ = fake_sdk(error=error)
            with self.assertRaisesRegex(RuntimeError, words):
                ClaudeClient(sdk=sdk).generate("s", "p", SCHEMA)

    def test_api_key_and_model_are_passed(self):
        sdk, calls, inits = fake_sdk()
        client = ClaudeClient(model="claude-sonnet-5-5", api_key="test-key", sdk=sdk)
        client.generate("s", "p", SCHEMA)
        self.assertEqual(inits, [{"api_key": "test-key"}])
        self.assertEqual(calls[0]["model"], "claude-sonnet-5-5")


class MakeClaudeClient(unittest.TestCase):
    def test_missing_key_is_a_value_error(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
                make_claude_client(None, None)

    def test_missing_package_is_a_value_error(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), mock.patch.dict(sys.modules, {"anthropic": None}):
            with self.assertRaisesRegex(ValueError, "pip install anthropic"):
                make_claude_client(None, None)

    def test_make_client_routes_claude_provider(self):
        from jobmatch_data.resume_match import make_client
        args = SimpleNamespace(provider="claude", key_file=None, model=None)
        with mock.patch("jobmatch_data.claude_client.make_claude_client", return_value="claude") as factory:
            self.assertEqual(make_client(args), "claude")
        factory.assert_called_once_with(None, None)


if __name__ == "__main__":
    unittest.main()
