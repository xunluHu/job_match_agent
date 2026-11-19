import unittest
import urllib.error

from jobmatch_data.latest_fetch import LISTINGS_URL, download_listings, extract_jd, fetch_jd
from tests.fakes import FakeHttp

BOARD = "https://api.ashbyhq.com/posting-api/job-board/acme"


class LatestFetch(unittest.TestCase):
    def test_download_listings(self):
        url = LISTINGS_URL.format(repo="New-Grad-Positions", ref="dev")
        self.assertEqual(download_listings(FakeHttp({url: [{"id": 1}]}), "New-Grad-Positions"), [{"id": 1}])
        for bad in ({}, []):
            with self.assertRaises(ValueError):
                download_listings(FakeHttp({url: bad}), "New-Grad-Positions")

    def test_extract_greenhouse(self):
        self.assertEqual(extract_jd("greenhouse", {"content": "&lt;p&gt;Build &lt;b&gt;APIs&lt;/b&gt;&lt;/p&gt;"}, "1"),
                         "Build APIs")
        self.assertIsNone(extract_jd("greenhouse", {"content": ""}, "1"))

    def test_extract_non_object_payload(self):
        for platform in ("greenhouse", "lever", "ashby"):
            self.assertIsNone(extract_jd(platform, ["unexpected"], "1"))

    def test_extract_lever(self):
        text = extract_jd("lever", {"descriptionPlain": "Intro",
                                    "lists": [{"text": "Requirements", "content": "<li>Python</li>"}],
                                    "additionalPlain": "Benefits"}, "u")
        self.assertEqual(text, "Intro\n\nRequirements\nPython\n\nBenefits")

    def test_extract_ashby(self):
        board = {"jobs": [{"id": "a", "descriptionPlain": "A text"}, {"id": "b", "descriptionHtml": "<p>B</p>"}]}
        self.assertEqual(extract_jd("ashby", board, "b"), "B")
        self.assertEqual(extract_jd("ashby", board, "a"), "A text")
        self.assertIsNone(extract_jd("ashby", board, "zzz"))

    def test_ashby_board_fetched_once(self):
        board = {"jobs": [{"id": "a", "descriptionPlain": "A text"}, {"id": "b", "descriptionPlain": "B text"}]}
        http, cache = FakeHttp({BOARD: board}), {}
        texts = [fetch_jd(http, {"platform": "ashby", "api_url": BOARD, "jid": jid}, cache) for jid in ("a", "b")]
        self.assertEqual(texts, ["A text", "B text"])
        self.assertEqual(len(http.calls), 1)

    def test_fetch_errors_propagate(self):
        url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1"
        error = urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            fetch_jd(FakeHttp({url: error}), {"platform": "greenhouse", "api_url": url, "jid": "1"}, {})


if __name__ == "__main__":
    unittest.main()
