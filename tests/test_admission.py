import copy
import unittest

from domain.admission import (
    APPROVE, OBSERVE, REJECT, decide_validation, evidence_hash,
)
from domain.errors import ForbiddenError, ValidationError
from tests.helpers import build_app, seed_releases, seed_tenants

import fixtures


class DecisionRecomputeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app, _ = build_app()
        cls.v1, _, _ = seed_releases(app)
        cls.app = app

    def test_passing_submission_approves_every_cell(self):
        decision, report = decide_validation(
            self.v1, fixtures.passing_validation(self.v1["id"])
        )
        self.assertEqual(decision, APPROVE)
        self.assertTrue(report["cells"])
        for cell in report["cells"]:
            self.assertEqual(cell["status"], "pass", cell)
            for check in cell["checks"]:
                self.assertEqual(check["status"], "pass", check)

    def test_metrics_are_recomputed_from_counts(self):
        _, report = decide_validation(
            self.v1, fixtures.passing_validation(self.v1["id"])
        )
        cell = next(c for c in report["cells"]
                    if c["condition"] == "appendicitis" and c["stratum"] == "emergency")
        expected_sens = cell["tp"] / cell["n_pos"]
        self.assertAlmostEqual(cell["sensitivity"]["point"], round(expected_sens, 3))
        self.assertAlmostEqual(
            cell["specificity"]["point"], round(cell["tn"] / cell["n_neg"], 3)
        )
        # CI 是服务端复算的，带上下界
        self.assertIsNotNone(cell["sensitivity"]["ci_low"])
        self.assertGreaterEqual(cell["sensitivity"]["ci_low"], 0.0)

    def test_emergency_subgroup_failure_cannot_be_masked(self):
        decision, report = decide_validation(
            self.v1, fixtures.emergency_subgroup_weak_validation(self.v1["id"])
        )
        self.assertEqual(decision, REJECT)
        failed = [
            c for c in report["cells"]
            if c["condition"] == "appendicitis" and c["stratum"] == "emergency"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["status"], "fail")
        sens_check = next(c for c in failed[0]["checks"] if c["metric"] == "sensitivity")
        self.assertEqual(sens_check["reason"], "point_below_minimum")
        # 报告里没有任何总平均字段可用于掩盖
        self.assertNotIn("mean", report)
        self.assertNotIn("average", report)

    def test_insufficient_sample_goes_to_observation(self):
        decision, report = decide_validation(
            self.v1, fixtures.insufficient_sample_validation(self.v1["id"])
        )
        self.assertEqual(decision, OBSERVE)
        hcc = next(c for c in report["cells"] if c["condition"] == "hcc")
        self.assertEqual(hcc["status"], "insufficient")
        self.assertTrue(any(k["reason"] == "sample_size" for k in hcc["checks"]))

    def test_protocol_not_supported_is_rejected(self):
        with self.assertRaises(ValidationError):
            decide_validation(
                self.v1, fixtures.protocol_mismatch_validation(self.v1["id"])
            )

    def test_missing_emergency_stratum_is_coverage_gap_reject(self):
        decision, report = decide_validation(
            self.v1, fixtures.coverage_gap_validation(self.v1["id"])
        )
        self.assertEqual(decision, REJECT)
        gaps = report["missing_cells"]
        self.assertTrue(gaps)
        self.assertTrue(all(g["stratum"] == "emergency" for g in gaps))
        self.assertTrue(all(g["reason"] == "coverage_gap" for g in gaps))

    def test_auc_missing_is_hard_fail(self):
        submission = fixtures.passing_validation(self.v1["id"])
        for cell in submission["cells"]:
            cell.pop("auc", None)
        decision, report = decide_validation(self.v1, submission)
        self.assertEqual(decision, REJECT)

    def test_inconsistent_counts_rejected(self):
        submission = fixtures.passing_validation(self.v1["id"])
        submission["cells"][0]["tp"] += 1  # tp+fn 不再等于 n_pos
        with self.assertRaises(ValidationError):
            decide_validation(self.v1, submission)

    def test_point_estimates_cannot_be_submitted(self):
        # 提交方无法夹带 sensitivity 字段让服务采信
        submission = copy.deepcopy(fixtures.passing_validation(self.v1["id"]))
        submission["cells"][0]["sensitivity"] = 0.999
        with self.assertRaises(ValidationError):
            decide_validation(self.v1, submission)

    def test_patient_level_fields_rejected(self):
        submission = fixtures.passing_validation(self.v1["id"])
        submission["patient_id"] = "P0001"
        with self.assertRaises(ValidationError):
            decide_validation(self.v1, submission)

    def test_case_level_list_rejected(self):
        submission = fixtures.passing_validation(self.v1["id"])
        submission["cases"] = [{"id": 1}]
        with self.assertRaises(ValidationError):
            decide_validation(self.v1, submission)

    def test_scope_protocol_violation_rejected(self):
        submission = fixtures.passing_validation(self.v1["id"])
        submission["scope_protocols"] = ["portal_venous"]
        with self.assertRaises(ValidationError):
            decide_validation(self.v1, submission)

    def test_evidence_hash_is_stable_and_bound_to_report(self):
        _, report = decide_validation(
            self.v1, fixtures.passing_validation(self.v1["id"])
        )
        digest = report["evidence_sha256"]
        again = evidence_hash({k: v for k, v in report.items() if k != "evidence_sha256"})
        self.assertEqual(digest, again)
        tampered = copy.deepcopy(report)
        tampered["cells"][0]["tp"] += 1
        self.assertNotEqual(
            digest,
            evidence_hash({k: v for k, v in tampered.items() if k != "evidence_sha256"}),
        )


class AdmissionServiceTest(unittest.TestCase):
    def setUp(self):
        self.app, _ = build_app()
        self.v1, _, _ = seed_releases(self.app)
        self.tokens = seed_tenants(self.app, "hosp-a", "hosp-b")

    def test_deployment_state_transitions(self):
        weak = self.app.submit_validation(
            "hosp-b", fixtures.emergency_subgroup_weak_validation(self.v1["id"])
        )
        self.assertEqual(weak["decision"], REJECT)
        dep_b = self.app.get_deployment("hosp-b", self.v1["id"])
        self.assertEqual(dep_b["state"], "rejected")

        ok = self.app.submit_validation(
            "hosp-a", fixtures.passing_validation(self.v1["id"])
        )
        dep_a = self.app.get_deployment("hosp-a", self.v1["id"])
        self.assertEqual(dep_a["state"], "approved")
        self.assertIn("appendicitis", dep_a["admitted_conditions"])

    def test_later_weak_validation_does_not_unapprove(self):
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v1["id"]))
        self.app.submit_validation(
            "hosp-a", fixtures.emergency_subgroup_weak_validation(self.v1["id"])
        )
        dep = self.app.get_deployment("hosp-a", self.v1["id"])
        self.assertEqual(dep["state"], "approved")

    def test_cross_tenant_read_forbidden(self):
        record = self.app.submit_validation(
            "hosp-a", fixtures.passing_validation(self.v1["id"])
        )
        with self.assertRaises(ForbiddenError):
            self.app.get_validation("hosp-b", record["id"])
        self.assertEqual(
            self.app.list_validations("hosp-b", self.v1["id"]), []
        )

    def test_list_only_own_records(self):
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v1["id"]))
        self.app.submit_validation(
            "hosp-b", fixtures.insufficient_sample_validation(self.v1["id"])
        )
        self.assertEqual(len(self.app.list_validations("hosp-a")), 1)
        self.assertEqual(len(self.app.list_validations("hosp-b")), 1)


if __name__ == "__main__":
    unittest.main()
