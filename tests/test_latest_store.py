import tempfile
import unittest
from pathlib import Path

from jobmatch_data.latest_rules import listing_rows
from jobmatch_data.latest_store import (JOB_TYPES, counts, load_profile, pending_jd, pool, save_jd, save_profile,
                                        sync_listings)
from tests.fakes import listing

NOW = 1_800_000_000
GH = "https://job-boards.greenhouse.io/acme/jobs/{}"
NO_FIELDS = {"degree": [], "min_years": None, "visa": "not_mentioned"}


def rows_of(*items, kind="newgrad"):
    return listing_rows(list(items), kind, NOW)


class LatestStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        self.db = self.tmp / "latest_jobs.sqlite3"
        self.rows = rows_of(
            listing("gh", "SWE", "Acme", GH.format(1), ["Seattle, WA"], 1, NOW),
            listing("mixed", "Data Engineer", "Beta", GH.format(2), ["Toronto, ON", "Seattle, WA"], 2, NOW),
            listing("ms", "SWE", "Microsoft", "https://apply.careers.microsoft.com/job/1", ["Redmond, WA"], 3, NOW))

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_job_types(self):
        self.assertEqual(JOB_TYPES, {"intern": {"intern"}, "fulltime": {"newgrad"}, "both": {"intern", "newgrad"}})

    def test_sync_inserts_and_marks_status(self):
        self.assertEqual(sync_listings(self.db, self.rows, NOW)["new"], 3)
        self.assertEqual({r["key"] for r in pending_jd(self.db)}, {"gh", "mixed"})  # "ms" is unsupported

    def test_resync_deactivates_missing_and_keeps_jd(self):
        sync_listings(self.db, self.rows, NOW)
        save_jd(self.db, "gh", "JD text", NO_FIELDS)
        report = sync_listings(self.db, [r for r in self.rows if r["key"] != "mixed"], NOW + 60)
        self.assertEqual((report["deactivated"], report["seen"], report["new"]), (1, 2, 0))
        self.assertEqual(pool(self.db, {"newgrad"}, 7, NOW + 60, False)[0]["description"], "JD text")
        self.assertEqual({r["key"] for r in pending_jd(self.db)}, set())  # inactive rows are not fetched

    def test_unavailable_jd_leaves_pending(self):
        sync_listings(self.db, self.rows, NOW)
        save_jd(self.db, "mixed", None, NO_FIELDS)
        self.assertEqual({r["key"] for r in pending_jd(self.db)}, {"gh"})

    def test_prune_old_rows(self):
        sync_listings(self.db, self.rows, NOW)
        self.assertEqual(sync_listings(self.db, self.rows, NOW + 26 * 86400)["pruned"], 0)  # ages 27-29 days
        self.assertEqual(sync_listings(self.db, self.rows, NOW + 32 * 86400)["pruned"], 3)

    def test_pool_filters(self):
        rows = rows_of(
            listing("new", "A", "A", GH.format(1), ["Austin, TX"], 1, NOW),
            listing("old", "B", "B", GH.format(2), ["Austin, TX"], 10, NOW),
            listing("nojd", "C", "C", GH.format(3), ["Austin, TX"], 1, NOW),
            listing("nosp", "D", "D", GH.format(4), ["Austin, TX"], 1, NOW),
            listing("cit", "E", "E", GH.format(5), ["Austin, TX"], 1, NOW))
        rows += rows_of(listing("int", "F", "F", GH.format(6), ["Austin, TX"], 1, NOW), kind="intern")
        sync_listings(self.db, rows, NOW)
        for key in ("new", "old", "int"):
            save_jd(self.db, key, "jd " + key, NO_FIELDS)
        save_jd(self.db, "nosp", "jd", dict(NO_FIELDS, visa="no_sponsorship"))
        save_jd(self.db, "cit", "jd", dict(NO_FIELDS, visa="citizenship_required"))
        keys = lambda kinds, days, sponsor: sorted(j["key"] for j in pool(self.db, kinds, days, NOW, sponsor))
        self.assertEqual(keys({"newgrad"}, 7, False), ["cit", "new", "nosp"])
        self.assertEqual(keys({"newgrad"}, 14, False), ["cit", "new", "nosp", "old"])
        self.assertEqual(keys({"newgrad"}, 7, True), ["new"])
        self.assertEqual(keys({"intern"}, 7, False), ["int"])
        self.assertEqual(keys({"intern", "newgrad"}, 7, True), ["int", "new"])
        job = next(j for j in pool(self.db, {"newgrad"}, 7, NOW, False) if j["key"] == "new")
        self.assertEqual((job["locations"], job["degree"], job["min_years"], job["visa"], job["kind"]),
                         (["Austin, TX"], [], None, "not_mentioned", "newgrad"))

    def test_pool_dedups_company_title(self):
        rows = rows_of(listing("a", "Software Engineer", "Acme", GH.format(1), ["Austin, TX"], 3, NOW),
                       listing("b", "software engineer", "ACME", GH.format(2), ["Seattle, WA"], 1, NOW))
        sync_listings(self.db, rows, NOW)
        save_jd(self.db, "a", "jd", NO_FIELDS)
        save_jd(self.db, "b", "jd", NO_FIELDS)
        self.assertEqual([j["key"] for j in pool(self.db, {"newgrad"}, 7, NOW, False)], ["b"])

    def test_counts(self):
        sync_listings(self.db, self.rows, NOW)
        save_jd(self.db, "gh", "JD text", NO_FIELDS)
        self.assertEqual(counts(self.db, 7, NOW), {"total": 3, "with_jd": 1, "pending": 1, "no_jd": 1})

    def test_profile_roundtrip_and_validation(self):
        self.assertIsNone(load_profile(self.tmp))
        save_profile(self.tmp, "resume text", "intern", True, NOW)
        self.assertEqual(load_profile(self.tmp),
                         {"resume": "resume text", "job_type": "intern", "needs_sponsorship": True, "updated_at": NOW})
        for bad in [("x", "parttime", False), ("x", "intern", "yes"), ("", "intern", False),
                    ("x" * 80_001, "intern", False), (None, "intern", False)]:
            with self.assertRaises(ValueError):
                save_profile(self.tmp, *bad, NOW)


if __name__ == "__main__":
    unittest.main()
