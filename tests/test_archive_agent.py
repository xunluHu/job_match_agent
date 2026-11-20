import tempfile
import time
import unittest
from pathlib import Path

from jobmatch_data.resume_match import JobIndex, clean_resume, match_resume, score_shortlist
from jobmatch_data.storage import save
from tests.test_existing_regression import JOBS, RESUME, FakeMatchClient


class ArchiveBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        save(JOBS, "test", self.root)
        self.index = JobIndex(self.root / "jobs.sqlite3")


class RetrievalAndScoring(ArchiveBase):
    def test_retrieve_with_query_uses_query_terms(self):
        results, retrieval = self.index.retrieve(RESUME, 3, query="Airflow Snowflake ETL")
        self.assertEqual(results[0]["title"], "Data Engineer")
        self.assertIn("airflow", retrieval["query_terms"])
        self.assertEqual(retrieval["candidate_profile"], self.index.retrieve(RESUME, 3)[1]["candidate_profile"])

    def test_retrieve_without_query_is_unchanged(self):
        self.assertEqual(self.index.retrieve(RESUME, 3), self.index.sections.retrieve(RESUME, self.index.jobs, 3))

    def test_score_shortlist_matches_pipeline(self):
        resume = clean_resume(RESUME)
        jobs, retrieval = self.index.retrieve(resume, 3)
        result = score_shortlist(resume, jobs, retrieval, self.index, FakeMatchClient(), 3, time.monotonic())
        self.assertEqual([m["title"] for m in result["matches"]],
                         [m["title"] for m in match_resume(RESUME, self.index, FakeMatchClient(), 3, 3)["matches"]])
        self.assertEqual(result["method"], "sqlite_bm25_parent_child_then_fake")


if __name__ == "__main__":
    unittest.main()
