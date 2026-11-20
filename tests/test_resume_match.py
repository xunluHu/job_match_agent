import io
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from jobmatch_data.gemini import Config, GeminiClient, load_config
from jobmatch_data.job_retrieval import SectionSearch
from jobmatch_data.job_qualification import candidate_profile
from jobmatch_data.resume_match import (JobIndex, clean_resume, evidence_entries, match_resume,
                                       resolve_evidence_references, validate_result)


RESUME = "Experience: Built Python and SQL data pipelines for operational analytics. " * 4
JOB = dict(job_ref="J001", source="test", source_id="1", title="Data Engineer", company="Acme",
           description="Build Python and SQL data pipelines for business analytics.",
           location="Remote", url="https://example.com/1", retrieval_score=0.5, last_seen_at="2026-11-16")


def response():
    return {"resume_summary": "具备数据管道项目经验。", "matches": [dict(
        job_ref="J001", fit_score=78, verdict="partial", reason="技术经历相关，其他资格待核实。",
        evidence=[dict(requirement="数据管道开发", resume_quote="Built Python and SQL data pipelines",
                       job_quote="Build Python and SQL data pipelines")], gaps=[], uncertainties=["工作许可未知"]) ]}


class ResumeMatchTests(unittest.TestCase):
    def test_profile_uses_work_dates_and_excludes_overlapping_time_and_projects(self):
        resume = """## Experience
Acme — January 2024 – July 2024
Backend Engineer
- Built an API.
Beta — April 2024 – January 2025
Backend Developer
- Built an internal tool.
Gamma — June 2023 – August 2023
Software Engineering Intern
- Wrote a dashboard.
## Projects
Personal project — Jan 2020 – Present
## Education
Bachelor of Science in Computer Science
"""
        profile, _ = candidate_profile(resume, date(2025, 2, 1))
        self.assertEqual(profile["role_family"], "backend")
        self.assertEqual(profile["professional_experience_months"], 12)
        self.assertEqual(profile["internship_experience_months"], 2)
        self.assertEqual(profile["education_level"], "bachelor")

    def test_screening_prioritizes_role_and_reported_years_and_merges_copies(self):
        def job(identifier, title, description, location="Seattle"):
            return dict(source="test", source_id=identifier, title=title, company="Acme",
                        location=location, url=f"https://example.com/{identifier}", description=description)

        duty = "Build frontend applications with React, TypeScript, Redux and Jest."
        jobs = [job("1", "Senior Frontend Engineer", duty + "\nMinimum Qualifications\n5+ years of experience"),
                job("2", "Frontend Engineer", duty + "\nRequirements\n1–3 years of experience"),
                job("3", "Frontend Engineer", duty + "\nRequirements\n1–3 years of experience"),
                job("4", "Frontend Engineer", duty + "\nRequirements\n1–3 years of experience", "Remote"),
                job("5", "AI Full Stack Engineer", duty + "\nPreferred Qualifications\n5 years of experience")]
        index = SectionSearch(jobs)
        self.addCleanup(index.conn.close)
        matches, info = index.retrieve(
            "## Summary\nFrontend Engineer with 2+ years of experience using React, TypeScript, Redux and Jest.\n"
            "## Projects\nOpenAI OpenAI OpenAI integrations.", jobs, 5)
        self.assertEqual([item["source_id"] for item in matches], ["2", "4", "5", "1"])
        self.assertEqual(info["duplicate_jobs"], 1)
        self.assertNotIn("openai", info["query_terms"])
        self.assertEqual(matches[0]["screening"]["min_years"], 1)
        self.assertIsNone(matches[2]["screening"]["min_years"])
        self.assertEqual(matches[3]["screening"]["experience_status"], "gap")

    def test_bm25_retrieves_sections_and_scores_the_complete_parent_job(self):
        duty = "Build CUDA kernels for GPU inference."
        qualification = "Maintain production diagnostics across the on-call rotation."
        description = ("## Responsibilities\n" + duty + "\n\n"
                       "## Preferred Qualifications\nCUDA optimization is helpful.\n\n"
                       "## Requirements\n" + qualification)
        resume = "Built CUDA inference kernels with C++ and profiled GPU throughput. " * 4
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp") as temporary:
            database = Path(temporary) / "jobs.sqlite3"
            conn = sqlite3.connect(database)
            with conn:
                conn.execute("""CREATE TABLE jobs (source, source_id, title, company,
                    location, url, description, last_seen_at)""")
                conn.executemany("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
                    ("test", "1", "GPU Engineer", "Acme", "Remote", "https://example.com/1", description, "2026-11-16"),
                    ("test", "2", "Another job", "Acme", "Remote", "https://example.com/2", "Validating metadata for archives.", "2026-11-16"),
                ])
            conn.close()
            index = JobIndex(database)
            self.addCleanup(index.sections.conn.close)
            hits, retrieval = index.retrieve("CUDA", 20)
            self.assertEqual(retrieval["query_terms"], ["cuda"])
            self.assertEqual(retrieval["matched_passages"], 2)
            self.assertEqual(retrieval["matched_jobs"], 1)
            self.assertEqual(len(hits), 1)
            context = self

            class Client:
                def generate(self, system, prompt, schema):
                    data = json.loads(prompt)
                    entries = data["jobs"][0]["evidence"]
                    by_text = {entry["text"]: entry["ref"] for entry in entries}
                    context.assertIn(duty, by_text)
                    context.assertIn(qualification, by_text)
                    resume_ref = data["resume"][0]["ref"]
                    match = response()["matches"][0]
                    match["evidence"] = [dict(requirement="GPU inference", resume_ref=resume_ref,
                                              job_evidence_ref=by_text[duty])]
                    match["capabilities"] = [dict(
                        axis=axis, required_level=50, candidate_level=50, requirement="GPU inference",
                        assessment="Related project", resume_ref=resume_ref, job_evidence_ref=by_text[qualification])
                        for axis in ("technical", "experience", "delivery", "systems", "ownership")]
                    return {"resume_summary": "CUDA projects", "matches": [match]}, {"model": "test"}

            report = match_resume(resume, index, Client(), 1, 2)
            match = report["matches"][0]
            self.assertEqual(report["context_mode"], "full_jobs_resume_excerpts")
            self.assertEqual(match["retrieval_hit"]["section"], "Responsibilities")
            self.assertEqual(match["retrieval_hit"]["quote"], duty)
            self.assertEqual(match["evidence"][0]["job_quote"], duty)
            self.assertEqual(match["capabilities"][0]["job_quote"], qualification)

    def test_compact_model_selects_refs_and_server_supplies_exact_quotes(self):
        class Index:
            jobs = [JOB]
            def search(self, resume, limit):
                return [JOB]
        class Client:
            compact = True
            calls = 0
            def generate(self, system, prompt, schema):
                self.calls += 1
                data = json.loads(prompt)
                result = response()
                result["matches"][0]["evidence"] = [dict(
                    requirement="数据管道", resume_ref=data["resume"][0]["ref"],
                    job_evidence_ref=data["jobs"][0]["evidence"][0]["ref"])]
                self.assertion = "resume_quote" not in schema["properties"]["matches"]["items"]["properties"]["evidence"]["items"]["properties"]
                result["matches"][0]["capabilities"] = [dict(axis=axis, required_level=None, candidate_level=None,
                    requirement="Not established", assessment="Insufficient evidence", resume_ref=None, job_evidence_ref=None)
                    for axis in ("technical", "experience", "delivery", "systems", "ownership")]
                return result, {"model": "test-stub", "provider": "openai", "usage": {}}
        client = Client()
        report = match_resume(RESUME, Index(), client, 1, 1)
        ev = report["matches"][0]["evidence"][0]
        self.assertIn(ev["resume_quote"], RESUME)
        self.assertEqual(ev["job_quote"], JOB["description"])
        self.assertEqual(client.calls, 1)
        self.assertTrue(client.assertion)
        self.assertEqual(report["evidence_mode"], "server_resolved_references")

    def test_rejects_refs_from_wrong_job_and_unknown_resume(self):
        resume_entries = evidence_entries(RESUME, "R")
        jobs = {"J001": evidence_entries(JOB["description"], "J001"),
                "J002": evidence_entries("A different job requires Java and Spring Boot.", "J002")}
        result = response()
        result["matches"][0]["evidence"] = [dict(requirement="test", resume_ref="R.1", job_evidence_ref="J002.1")]
        with self.assertRaisesRegex(ValueError, "different job"):
            resolve_evidence_references(result, resume_entries, jobs)
        result["matches"][0]["evidence"][0].update(resume_ref="R.999", job_evidence_ref="J001.1")
        with self.assertRaisesRegex(ValueError, "unknown resume"):
            resolve_evidence_references(result, resume_entries, jobs)

    def test_numbered_evidence_never_joins_disjoint_source_lines(self):
        original = "Built Python pipelines.\nUnrelated middle paragraph.\nDeployed SQL dashboards."
        entries = evidence_entries(original, "R", chunk_size=20)
        self.assertTrue(all(entry["text"] in original for entry in entries))
        self.assertEqual(len({entry["ref"] for entry in entries}), len(entries))

    def test_model_cannot_invent_jobs_or_quotes(self):
        result = response()
        result["matches"][0]["job_ref"] = "invented"
        with self.assertRaisesRegex(ValueError, "job reference"):
            validate_result(result, RESUME, [JOB], 5)
        result = response()
        result["matches"][0]["evidence"][0]["resume_quote"] = "Ten years of leadership experience"
        with self.assertRaisesRegex(ValueError, "source"):
            validate_result(result, RESUME, [JOB], 5)

    def test_verified_metadata_comes_from_database(self):
        result = response()
        result["matches"][0]["url"] = "https://attacker.invalid"
        verified = validate_result(result, RESUME, [JOB], 5)
        self.assertEqual(verified["matches"][0]["url"], JOB["url"])
        self.assertEqual(verified["matches"][0]["eligibility"], "not_verified")

    def test_duplicate_results_and_noninteger_scores_are_rejected(self):
        result = response()
        result["matches"] *= 2
        with self.assertRaises(ValueError):
            validate_result(result, RESUME, [JOB], 5)
        result = response()
        result["matches"][0]["fit_score"] = True
        with self.assertRaises(ValueError):
            validate_result(result, RESUME, [JOB], 5)

    def test_one_model_call_uses_complete_description(self):
        class Index:
            jobs = [JOB]
            def search(self, resume, limit):
                return [JOB]
        class Client:
            calls = 0
            def generate(self, system, prompt, schema):
                self.calls += 1
                assert JOB["description"] in prompt
                assert "untrusted" in system
                return response(), {"model": "test-stub", "usage": {}}
        client = Client()
        report = match_resume(RESUME, Index(), client, 1, 1)
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(report["matches"]), 1)

    def test_markdown_metadata_and_contact_are_not_sent(self):
        text = "unused name <!-- resume-content:start -->" + RESUME + " contact@example.org <!-- resume-content:end -->unused links"
        result = clean_resume(text)
        self.assertNotIn("unused", result)
        self.assertNotIn("contact@example.org", result)

    def test_resources_key_is_loaded_without_exposing_in_repr(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp") as temporary:
            root = Path(temporary)
            resources = root / "output/resources"
            resources.mkdir(parents=True)
            (resources / "gemini_key.txt").write_text("GEMINI_API_KEY=test-private-key", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                config = load_config(root)
            self.assertEqual(config.api_key, "test-private-key")
            self.assertNotIn("test-private-key", repr(config))

    def test_http_errors_do_not_print_secret_response(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 403, "secret-value", {}, io.BytesIO(b"secret-value"))
        client = GeminiClient(Config("secret-value", "test-model"), opener=opener)
        with self.assertRaises(RuntimeError) as caught:
            client.generate("system", "prompt", {})
        self.assertNotIn("secret-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
