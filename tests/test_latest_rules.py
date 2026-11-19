import unittest

from jobmatch_data.latest_rules import (JobRef, in_us, job_fields, listing_rows, matched_skills,
                                        parse_job_url, visa_note)
from tests.fakes import listing

NOW = 1_800_000_000


class LatestRules(unittest.TestCase):
    def test_in_us(self):
        for loc, want in [("San Francisco, CA", True), ("NYC", True), ("Remote in USA", True),
                          ("Toronto, ON, Canada", False), ("Vancouver, BC", False), ("London, UK", False),
                          ("Remote", False)]:
            self.assertEqual(in_us(loc), want, loc)

    def test_parse_job_url(self):
        U = "0a1b2c3d-0000-1111-2222-333344445555"
        self.assertEqual(parse_job_url("https://job-boards.greenhouse.io/affirm/jobs/7727320003"),
                         JobRef("greenhouse", "https://boards-api.greenhouse.io/v1/boards/affirm/jobs/7727320003",
                                "7727320003"))
        self.assertEqual(parse_job_url(f"https://jobs.lever.co/acme/{U}"),
                         JobRef("lever", f"https://api.lever.co/v0/postings/acme/{U}", U))
        self.assertEqual(parse_job_url(f"https://jobs.ashbyhq.com/openai/{U}?src=x"),
                         JobRef("ashby", "https://api.ashbyhq.com/posting-api/job-board/openai", U))
        self.assertIsNone(parse_job_url("https://apply.careers.microsoft.com/job/1"))
        self.assertIsNone(parse_job_url("https://evil.example/greenhouse.io/x/jobs/1"))
        self.assertIsNone(parse_job_url("https://boards.greenhouse.io/embed/job_app?for=x&token=1"))

    def test_listing_rows_filters(self):
        gh = "https://job-boards.greenhouse.io/acme/jobs/1"
        items = [
            listing("gh", "SWE", "Acme", gh, ["Seattle, WA"], 1, NOW),
            listing("mixed", "SWE", "Beta", gh, ["Toronto, ON", "Seattle, WA"], 2, NOW),
            listing("ms", "SWE", "Microsoft", "https://apply.careers.microsoft.com/job/1", ["Redmond, WA"], 3, NOW),
            listing("inactive", "SWE", "C", gh, ["Austin, TX"], 1, NOW, active=False),
            listing("hidden", "SWE", "D", gh, ["Austin, TX"], 1, NOW, visible=False),
            listing("old", "SWE", "E", gh, ["Austin, TX"], 20, NOW),
            listing("canada", "SWE", "F", gh, ["Toronto, ON"], 1, NOW),
        ]
        rows = listing_rows(items, "newgrad", NOW)
        self.assertEqual(sorted(r["key"] for r in rows), ["gh", "mixed", "ms"])
        self.assertIsNone(next(r for r in rows if r["key"] == "ms")["platform"])
        mixed = next(r for r in rows if r["key"] == "mixed")
        self.assertEqual(mixed["locations"], ["Toronto, ON", "Seattle, WA"])
        self.assertEqual((mixed["kind"], mixed["platform"], mixed["company"]), ("newgrad", "greenhouse", "Beta"))

    def test_visa_note(self):
        self.assertEqual(visa_note("We are unable to sponsor visas.", "Other"), "no_sponsorship")
        self.assertEqual(visa_note("Must be a U.S. citizen.", None), "citizenship_required")
        self.assertEqual(visa_note("Python and SQL", "Other"), "not_mentioned")
        self.assertEqual(visa_note("", "Does Not Offer Sponsorship"), "no_sponsorship")
        self.assertEqual(visa_note("", "U.S. Citizenship is Required"), "citizenship_required")

    def test_job_fields(self):
        jd = "Requirements:\nBachelor's degree in Computer Science required\n3+ years of experience building services"
        self.assertEqual(job_fields("Backend Engineer", jd, "Other"),
                         {"degree": [{"level": "bachelor", "priority": "required"}], "min_years": 3,
                          "visa": "not_mentioned"})
        # Known limit of job_profile: lines containing "or" are skipped.
        self.assertIsNone(job_fields("X", "Requirements:\n5+ years of experience with Python or Go", None)["min_years"])

    def test_job_fields_dedups_degree_levels(self):
        jd = ("Requirements:\nMaster's degree preferred\nBachelor's degree in Computer Science required\n"
              "Master's degree in Computer Science required")
        self.assertEqual(job_fields("SWE", jd, None)["degree"],
                         [{"level": "master", "priority": "required"}, {"level": "bachelor", "priority": "required"}])

    def test_matched_skills(self):
        self.assertEqual(matched_skills("Built FastAPI services on PostgreSQL with Docker",
                                        "We use Python, FastAPI, Docker and Kubernetes"), ["FastAPI", "Docker"])


if __name__ == "__main__":
    unittest.main()
