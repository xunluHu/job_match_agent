import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from jobmatch_data.latest_fetch import LISTINGS_URL
from jobmatch_data.latest_refresh import LISTS, Refresher, refresh
from jobmatch_data.latest_store import counts, get_meta, pending_jd, pool
from tests.fakes import FakeHttp, listing

NOW = 1_800_000_000
U = "0a1b2c3d-0000-1111-2222-333344445555"
NG = LISTINGS_URL.format(repo="New-Grad-Positions", ref="dev")
IN = LISTINGS_URL.format(repo="Summer2027-Internships", ref="dev")
GH_API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/{}"
LEVER_API = f"https://api.lever.co/v0/postings/beta/{U}"
JD = "Requirements:\nBachelor's degree required\nPython and SQL"


def newgrad(extra=()):
    return [listing("gh1", "Backend Engineer", "Acme", "https://job-boards.greenhouse.io/acme/jobs/1", ["Seattle, WA"], 1, NOW),
            listing("ms", "SWE", "Microsoft", "https://apply.careers.microsoft.com/job/1", ["Redmond, WA"], 1, NOW),
            *extra]


def routes(**overrides):
    base = {NG: newgrad(),
            IN: [listing("lv", "ML Intern", "Beta", f"https://jobs.lever.co/beta/{U}", ["Austin, TX"], 2, NOW)],
            GH_API.format(1): {"content": JD},
            GH_API.format(2): {"content": JD},
            LEVER_API: {"descriptionPlain": JD}}
    base.update(overrides)
    return base


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class LatestRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp_dir.name) / "latest_jobs.sqlite3"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_lists(self):
        self.assertEqual(LISTS, (("New-Grad-Positions", "newgrad"), ("Summer2027-Internships", "intern")))

    def test_first_refresh_fetches_jds(self):
        report = refresh(self.db, FakeHttp(routes()), now=NOW)
        self.assertEqual((report["new"], report["jd_ok"], report["jd_failed"]), (3, 2, 0))
        self.assertEqual(get_meta(self.db, "last_refresh"), str(NOW))
        jobs = pool(self.db, {"newgrad", "intern"}, 7, NOW, False)
        self.assertEqual(sorted(j["key"] for j in jobs), ["gh1", "lv"])
        self.assertEqual(jobs[0]["degree"], [{"level": "bachelor", "priority": "required"}])

    def test_second_refresh_fetches_only_new_jds(self):
        refresh(self.db, FakeHttp(routes()), now=NOW)
        extra = listing("gh2", "Data Engineer", "Acme", "https://job-boards.greenhouse.io/acme/jobs/2", ["Austin, TX"], 0, NOW)
        http = FakeHttp(routes(**{NG: newgrad([extra])}))
        report = refresh(self.db, http, now=NOW + 60)
        jd_calls = [url for url in http.calls if url not in (NG, IN)]
        self.assertEqual(jd_calls, [GH_API.format(2)])
        self.assertEqual((report["new"], report["seen"], report["jd_ok"]), (1, 3, 1))

    def test_jd_403_marks_unavailable(self):
        url = GH_API.format(1)
        report = refresh(self.db, FakeHttp(routes(**{url: HTTPError(url, 403, "Forbidden", {}, None)})), now=NOW)
        self.assertEqual((report["jd_ok"], report["jd_failed"]), (1, 1))
        self.assertEqual(counts(self.db, 7, NOW)["no_jd"], 2)  # Microsoft (unsupported) plus the 403
        self.assertEqual(pending_jd(self.db), [])

    def test_jd_429_stays_pending(self):
        url = GH_API.format(1)
        report = refresh(self.db, FakeHttp(routes(**{url: HTTPError(url, 429, "Too Many", {}, None)})), now=NOW)
        self.assertEqual((report["jd_ok"], report["jd_failed"]), (1, 1))
        self.assertEqual([r["key"] for r in pending_jd(self.db)], ["gh1"])

    def test_download_failure_keeps_previous_data(self):
        refresh(self.db, FakeHttp(routes()), now=NOW)
        with self.assertRaises(URLError):
            refresh(self.db, FakeHttp(routes(**{IN: URLError("offline")})), now=NOW + 60)
        self.assertEqual(len(pool(self.db, {"newgrad", "intern"}, 7, NOW + 60, False)), 2)
        self.assertEqual(get_meta(self.db, "last_refresh"), str(NOW))

    def test_empty_listing_is_an_error(self):
        refresh(self.db, FakeHttp(routes()), now=NOW)
        with self.assertRaises(ValueError):
            refresh(self.db, FakeHttp(routes(**{NG: []})), now=NOW + 60)
        self.assertEqual(counts(self.db, 7, NOW + 60)["total"], 3)

    def test_refresher_status_and_trigger(self):
        release, started = threading.Event(), threading.Event()

        class BlockingHttp(FakeHttp):
            def get(self, url):
                if not started.is_set():
                    started.set()
                    release.wait(5)
                return super().get(url)

        refresher = Refresher(self.db, lambda: BlockingHttp(routes()), interval=3600)
        self.assertEqual(refresher.status(), {"running": False, "last_error": None, "last_refresh": None})
        refresher.start()
        self.assertTrue(started.wait(5))
        self.assertTrue(refresher.status()["running"])
        self.assertFalse(refresher.trigger())
        release.set()
        self.assertTrue(wait_for(lambda: not refresher.status()["running"] and refresher.status()["last_refresh"]))
        self.assertIsNone(refresher.status()["last_error"])

    @mock.patch("jobmatch_data.latest_refresh.traceback.print_exc")
    def test_refresher_records_and_clears_error(self, _print_exc):
        clients = [FakeHttp({}), FakeHttp(routes())]  # first: listings 404, second: works
        refresher = Refresher(self.db, lambda: clients.pop(0), interval=3600)
        refresher.start()
        self.assertTrue(wait_for(lambda: refresher.status()["last_error"]))
        self.assertTrue(wait_for(refresher.trigger))
        self.assertTrue(wait_for(lambda: refresher.status()["last_refresh"] and not refresher.status()["last_error"]))


if __name__ == "__main__":
    unittest.main()
