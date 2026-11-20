import unittest

from jobmatch_data.matching import rank_profiles, restriction_evidence, skill_span


class MatchingTests(unittest.TestCase):
    def test_full_resume_experience_and_projects_affect_ranking(self):
        profiles = [dict(candidate_id="R01", headline="Data engineer", role_family="data",
                         skills=["Python", "SQL"], experience_text="distributed pipelines ingestion Kafka",
                         projects_text="pipelines ingestion Kafka", resume_page_texts=[
                             {"page": 1, "text": "Built Python and SQL distributed pipelines using Kafka."}])]
        base = dict(source="test", company="Acme", location=None, title="Data Engineer")
        jobs = [dict(base, source_id="1", url="https://example.com/1", description="Python SQL Kafka distributed pipelines ingestion"),
                dict(base, source_id="2", url="https://example.com/2", description="Python SQL Tableau dashboards visualization design"),
                dict(base, source_id="3", url="https://example.com/3", title="Sales Manager",
                     description="Kafka distributed pipelines ingestion Tableau dashboards visualization design")]
        result = rank_profiles(profiles, jobs)[0]["ranked_jobs"]
        self.assertEqual(result[0]["source_id"], "1")
        self.assertGreater(result[0]["score_components"]["experience_tfidf_cosine"], result[1]["score_components"]["experience_tfidf_cosine"])
        self.assertEqual(result[0]["resume_evidence"]["Python"]["page"], 1)

    def test_restrictions_are_evidenced_not_eligibility_decisions(self):
        text = "Applicants must be U.S. citizens and hold an active security clearance."
        flags = restriction_evidence(text)
        self.assertEqual(set(flags), {"citizenship", "clearance"})
        self.assertTrue(all(snippet in text for snippet in flags.values()))

    def test_skill_aliases_and_boundaries(self):
        self.assertIsNone(skill_span("JavaScript, PostgreSQL and cargo", "Java"))
        self.assertIsNone(skill_span("staging and Django", "Go"))
        self.assertIsNotNone(skill_span("Work with K8s and PostgreSQL", "Kubernetes"))
        self.assertIsNotNone(skill_span("Postgres databases", "PostgreSQL"))
        self.assertIsNone(skill_span("A developer with experience", "R"))

    def test_ranks_roles_and_uses_literal_evidence(self):
        profiles = [dict(candidate_id="C01", headline="Data engineer", role_family="data", skills=["Python", "SQL"])]
        base = dict(source="test", source_id="1", company="Acme", location=None, url="https://example.com/1")
        jobs = [dict(base, title="Data Engineer", description="Build data pipelines with Python and SQL."),
                dict(base, source_id="2", title="Sales Manager", url="https://example.com/2",
                     description="Use Python and SQL for sales analytics.")]
        result = rank_profiles(profiles, jobs)[0]["ranked_jobs"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source_id"], "1")
        self.assertEqual(set(result[0]["matched_skill_mentions"]), {"Python", "SQL"})
        for snippet in result[0]["job_evidence"].values():
            self.assertIn(snippet, jobs[0]["description"])

    def test_missing_profile_skill_is_not_a_claim_of_incompetence(self):
        profiles = [dict(candidate_id="C01", headline="Backend", role_family="backend", skills=["Python", "SQL"])]
        jobs = [dict(source="test", source_id="1", company="Acme", location=None,
                     url="https://example.com/1", title="Backend Engineer",
                     description="Python, SQL and Kubernetes.")]
        result = rank_profiles(profiles, jobs)[0]["ranked_jobs"][0]
        self.assertIn("Kubernetes", result["job_skill_mentions_not_evidenced_in_profile"])
        self.assertEqual(result["eligibility"], "not_assessed")

    def test_excludes_single_skill_hits_and_deduplicates_url(self):
        profiles = [dict(candidate_id="C01", headline="Data", role_family="data", skills=["Python", "SQL"])]
        base = dict(source="test", company="Acme", location=None, title="Data Engineer", url="https://example.com/1")
        jobs = [dict(base, source_id="1", description="Python and SQL"),
                dict(base, source_id="2", description="Python and SQL"),
                dict(base, source_id="3", description="Python only", url="https://example.com/3")]
        self.assertEqual(len(rank_profiles(profiles, jobs)[0]["ranked_jobs"]), 1)


if __name__ == "__main__":
    unittest.main()
