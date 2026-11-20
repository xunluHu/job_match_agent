import tempfile
import unittest
import sqlite3
import csv
from pathlib import Path

from jobmatch_data.sources import ats, discover_boards, simplify
from jobmatch_data.storage import connect, save
from jobmatch_data.pipeline import export_data


class FakeClient:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return next(self.payloads)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (Path(__file__).resolve().parents[1] / "tmp").mkdir(exist_ok=True)

    def test_simplify_keeps_inactive_and_date_semantics(self):
        client = FakeClient([[dict(id="1", title="Engineer", company_name="Acme",
                                   url="https://example.com/job", active=False,
                                   date_posted=1704067200, date_updated=1704153600)]])
        row = simplify(client, "New-Grad-Positions")[0]
        self.assertFalse(row["active"])
        self.assertEqual(row["posted_at"], "2024-01-01T00:00:00+00:00")
        self.assertIn("Aggregator", row["provenance"]["date_semantics"])
        self.assertIsNone(row["description"])

    def test_greenhouse_update_is_not_publication(self):
        client = FakeClient([{"jobs": [dict(id=10, title="Engineer", absolute_url="https://example.com/10",
                                           updated_at="2024-01-02T00:00:00Z", content="&lt;p&gt;Build apps&lt;/p&gt;")]}])
        row = ats(client, "greenhouse", "acme", "Acme")[0]
        self.assertIsNone(row["posted_at"])
        self.assertEqual(row["description"], "Build apps")
        self.assertEqual(row["source_updated_at"], "2024-01-02T00:00:00+00:00")

    def test_ashby_id_fallback_and_unlisted_exclusion(self):
        row = dict(title="Engineer", jobUrl="https://jobs.ashbyhq.com/acme/1",
                   publishedAt="2024-01-01T00:00:00Z", descriptionPlain="Build apps")
        result = ats(FakeClient([{"jobs": [row, dict(row, isListed=False)]}]), "ashby", "acme", "Acme")
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["id"])
        self.assertEqual(result[0]["posted_at"], "2024-01-01T00:00:00+00:00")

    def test_lever_pagination_and_description_sections(self):
        page = [dict(id=str(i), text="Engineer", hostedUrl=f"https://example.com/{i}",
                     descriptionPlain="Opening", lists=[{"text": "Requirements", "content": "<li>Python</li>"}],
                     additionalPlain="Benefits") for i in range(100)]
        client = FakeClient([page, []])
        result = ats(client, "lever", "acme", "Acme", "eu")
        self.assertEqual(len(result), 100)
        self.assertIn("skip=100", client.urls[1])
        self.assertIn("api.eu.lever.co", client.urls[0])
        self.assertIn("Python", result[0]["description"])
        self.assertIn("Benefits", result[0]["description"])
        self.assertIsNone(result[0]["posted_at"])

    def test_discovery_exact_host_and_eu(self):
        rows = [dict(url="https://jobs.eu.lever.co/acme/1", company="Acme", active=True),
                dict(url="https://jobs.lever.co.attacker.test/wrong/1", company="Other", active=True)]
        self.assertEqual(discover_boards(rows), [dict(vendor="lever", board="acme", company="Acme", region="eu")])

    def test_upsert_preserves_first_seen_and_versions(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp") as tmp:
            root = Path(tmp)
            row = dict(id="1", title="Engineer", company="Acme", url="https://example.com/1", active=True)
            save([row], "test", root, "2024-01-01T00:00:00+00:00")
            save([row], "test", root, "2024-01-02T00:00:00+00:00")
            save([dict(row, active=False)], "test", root, "2024-01-03T00:00:00+00:00")
            conn = connect(root)
            try:
                self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM job_versions").fetchone()[0], 2)
                state = conn.execute("SELECT first_seen_at,last_seen_at,active FROM jobs").fetchone()
                self.assertEqual(state, ("2024-01-01T00:00:00+00:00", "2024-01-03T00:00:00+00:00", 0))
            finally:
                conn.close()

    def test_invalid_batch_does_not_partially_import(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp") as tmp:
            root = Path(tmp)
            row = dict(id="1", title="Engineer", company="Acme", url="https://example.com/1")
            with self.assertRaises(ValueError):
                save([row, dict(row, id="2", active="false")], "test", root)
            self.assertFalse((root / "jobs.sqlite3").exists())

    def test_migrate_original_schema_and_export(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp") as tmp:
            root = Path(tmp)
            conn = sqlite3.connect(root / "jobs.sqlite3")
            conn.execute("""CREATE TABLE jobs (source TEXT, source_id TEXT, title TEXT,
                company TEXT, location TEXT, url TEXT, posted_at TEXT, description TEXT,
                first_seen_at TEXT, last_seen_at TEXT, raw_json TEXT,
                PRIMARY KEY(source, source_id))""")
            conn.close()
            save([dict(id="1", title="=Example", company="Acme", url="https://example.com/1")], "test", root)
            export_data(root)
            with (root / "exports/jobs.csv").open(encoding="utf-8-sig", newline="") as file:
                self.assertEqual(next(csv.DictReader(file))["title"], "'=Example")


if __name__ == "__main__":
    unittest.main()
