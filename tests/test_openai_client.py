import io
import json
import unittest
from urllib.error import HTTPError

from jobmatch_data.openai_client import Config, OpenAIClient, strict_schema
from jobmatch_data.resume_match import SCHEMA, relevant_excerpt


class OpenAIClientTests(unittest.TestCase):
    def test_responses_request_is_bounded_and_uses_auth_header(self):
        captured = []
        def opener(request, timeout):
            captured.append(request)
            return io.BytesIO(json.dumps({"status": "completed", "model": "gpt-4.1-nano", "usage": {"input_tokens": 20, "output_tokens": 5},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}]}).encode())
        result, meta = OpenAIClient(Config("test-private-key"), opener=opener).generate("s", "p", {"type": "object", "properties": {"ok": {"type": "boolean"}}})
        payload = json.loads(captured[0].data)
        self.assertEqual(captured[0].full_url, "https://api.openai.com/v1/responses")
        self.assertNotIn("test-private-key", captured[0].full_url)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["max_output_tokens"], 4000)
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(meta["usage"]["input_tokens"], 20)
        self.assertTrue(result["ok"])

    def test_strict_schema_closes_nested_objects(self):
        schema = strict_schema(SCHEMA)
        self.assertFalse(schema["additionalProperties"])
        evidence = schema["properties"]["matches"]["items"]["properties"]["evidence"]["items"]
        self.assertFalse(evidence["additionalProperties"])
        self.assertEqual(set(evidence["required"]), set(evidence["properties"]))

    def test_auth_failure_is_not_retried_or_exposed(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":{"message":"expired test-private-key"}}'))
        with self.assertRaises(RuntimeError) as caught:
            OpenAIClient(Config("test-private-key"), opener=opener).generate("s", "p", SCHEMA)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("test-private-key", str(caught.exception))

    def test_excerpt_is_bounded_and_preserves_evidence(self):
        text = "Unrelated introduction.\nBuilt Python SQL pipelines for reporting.\nOther irrelevant sentences."
        excerpt = relevant_excerpt(text, "Python SQL pipelines", 50)
        self.assertLessEqual(len(excerpt), 50)
        self.assertIn("Built Python SQL pipelines for reporting.", excerpt)
        self.assertTrue(all(line in text for line in excerpt.splitlines()))

    def test_incomplete_output_is_not_success(self):
        def opener(request, timeout):
            return io.BytesIO(b'{"status":"incomplete","output":[]}')
        with self.assertRaises(RuntimeError):
            OpenAIClient(Config("test"), opener=opener).generate("s", "p", SCHEMA)


if __name__ == "__main__":
    unittest.main()
