import unittest

from jobmatch_data.capabilities import AXES, resolve_capabilities

RESUME = {"R.1": "Built FastAPI services"}
JOB = {"J001.1": "3+ years building Python services"}


def items(**overrides):
    base = [{"axis": key, "required_level": 50, "candidate_level": 50, "requirement": "r", "assessment": "a",
             "resume_ref": "R.1", "job_evidence_ref": "J001.1"} for key, _ in AXES]
    base[0].update(overrides)
    return base


class ResolveCapabilities(unittest.TestCase):
    def test_level_without_reference_becomes_unknown(self):
        # Models sometimes give a level but no source; an unsupported level is shown as unknown, not as a score.
        resolved = resolve_capabilities(items(required_level=75, job_evidence_ref=None), RESUME, JOB)
        first = resolved[0]
        self.assertEqual((first["required_level"], first["job_quote"], first["coverage"]), (None, None, None))
        self.assertIn("cleared_required_level_without_job_evidence_ref", first["normalizations"])
        self.assertEqual(resolved[1]["required_level"], 50)

    def test_unknown_reference_is_still_rejected(self):
        with self.assertRaises(ValueError):
            resolve_capabilities(items(job_evidence_ref="J009.4"), RESUME, JOB)

    def test_out_of_range_level_is_still_rejected(self):
        with self.assertRaises(ValueError):
            resolve_capabilities(items(candidate_level=120), RESUME, JOB)


if __name__ == "__main__":
    unittest.main()
