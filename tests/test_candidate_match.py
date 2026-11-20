import json
import unittest

from jobmatch_data.candidate_match import match_candidates
from jobmatch_data.capabilities import AXES


class FakeIndex:
    jobs = [dict(source="resume_library", source_id=f"R{i:02d}", title="Data Engineer", company="Resume library",
                 location=None, url=f"R{i:02d}", last_seen_at=None, retrieval_score=0.5,
                 description="Built Python and SQL pipelines for production analytics. " * 4) for i in range(1, 7)]

    def search(self, text, limit):
        return self.jobs[:limit]


class Client:
    def generate(self, system, prompt, schema):
        data = json.loads(prompt)
        matches = []
        for i, candidate in enumerate(data["candidates"][:data["return_at_most"]]):
            rr, jr = candidate["evidence"][0]["ref"], data["job"][0]["ref"]
            matches.append(dict(candidate_ref=candidate["candidate_ref"], fit_score=80-i, verdict="partial",
                reason="Relevant pipeline implementation experience.", gaps=[], uncertainties=["Eligibility not verified."],
                evidence=[dict(requirement="Build pipelines", resume_ref=rr, job_evidence_ref=jr)],
                capabilities=[dict(axis=axis, required_level=75, candidate_level=50, requirement="Own production delivery",
                    assessment="Hands-on pipeline work evidenced", resume_ref=rr, job_evidence_ref=jr) for axis, _ in AXES]))
        return {"resume_summary": "Role requires production data pipeline delivery.", "matches": matches}, {"model": "test-stub", "provider": "openai", "usage": {}}


class CandidateMatchTests(unittest.TestCase):
    def test_top5_survives_unknown_level_with_retained_reference(self):
        class UnknownClient(Client):
            calls = 0
            def generate(self, system, prompt, schema):
                self.calls += 1
                result, meta = super().generate(system, prompt, schema)
                for match in result["matches"]:
                    match["capabilities"][1]["candidate_level"] = None
                    match["capabilities"][2]["required_level"] = None
                return result, meta
        client = UnknownClient()
        result = match_candidates("Develop Python and SQL production pipelines. " * 5, FakeIndex(), client, 5, 6)
        self.assertEqual(len(result["matches"]), 5)
        self.assertEqual(client.calls, 1)
        for match in result["matches"]:
            self.assertIsNone(match["capabilities"][1]["resume_ref"])
            self.assertIsNone(match["capabilities"][2]["job_evidence_ref"])

    def test_job_returns_five_distinct_resumes_with_five_dimensions(self):
        job = "Develop Python and SQL production pipelines with independent ownership. " * 4
        result = match_candidates(job, FakeIndex(), Client(), 5, 6)
        self.assertEqual(result["direction"], "candidates")
        self.assertEqual(len(result["matches"]), 5)
        self.assertEqual(len({m["candidate_id"] for m in result["matches"]}), 5)
        self.assertTrue(all(len(m["capabilities"]) == 5 for m in result["matches"]))
        self.assertTrue(all("<svg" in m["radar_svg"] for m in result["matches"]))

    def test_candidate_cannot_borrow_another_resume_evidence(self):
        class BadClient(Client):
            def generate(self, system, prompt, schema):
                response, meta = super().generate(system, prompt, schema)
                response["matches"][0]["evidence"][0]["resume_ref"] = "R02.1"
                return response, meta
        with self.assertRaisesRegex(ValueError, "unknown resume"):
            match_candidates("Build Python and SQL production pipelines. " * 5, FakeIndex(), BadClient(), 5, 6)


if __name__ == "__main__":
    unittest.main()
