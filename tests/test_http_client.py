import gzip
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock

from jobmatch_data.http import Client


class FakeResponse:
    def __init__(self, body, headers):
        self.body, self.status, self.headers = body, 200, headers

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HttpClient(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        self.requests = []

        def fake_urlopen(request, timeout):
            self.requests.append(request)
            return FakeResponse(gzip.compress(b"[1]"), {"Content-Encoding": "gzip"})

        patcher = mock.patch("jobmatch_data.http.urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_gzip_body_is_decompressed(self):
        self.assertEqual(Client(self.tmp, delay=0).get("https://x/a.json"), [1])

    def test_archive_default_writes_decompressed_bytes(self):
        Client(self.tmp, delay=0).get("https://x/a.json")
        self.assertEqual((self.tmp / "raw" / (sha256(b"[1]").hexdigest() + ".json")).read_bytes(), b"[1]")

    def test_archive_false_writes_nothing(self):
        client = Client(self.tmp, delay=0, archive=False)
        client.get("https://x/a.json")
        self.assertFalse((self.tmp / "raw").exists())
        self.assertEqual(client.responses, [])

    def test_request_sends_accept_encoding(self):
        Client(self.tmp, delay=0).get("https://x/a.json")
        self.assertEqual(self.requests[0].get_header("Accept-encoding"), "gzip")


if __name__ == "__main__":
    unittest.main()
