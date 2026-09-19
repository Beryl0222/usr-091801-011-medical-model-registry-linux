import unittest

from domain.errors import ValidationError
from domain.privacy import assert_no_patient_fields

import fixtures
from tests.helpers import build_app, seed_releases, activate, seed_tenants


class PrivacyGuardTest(unittest.TestCase):
    def test_plain_aggregate_passes(self):
        assert_no_patient_fields({"tp": 10, "fp": 2, "note": "ok"})

    def test_model_name_not_flagged(self):
        assert_no_patient_fields({"model_name": "abd-ct"})

    def test_patient_key_at_top(self):
        with self.assertRaises(ValidationError):
            assert_no_patient_fields({"patient_id": "P1", "n": 1})

    def test_nested_patient_key(self):
        with self.assertRaises(ValidationError):
            assert_no_patient_fields({"cell": {"mrn": "123"}})

    def test_unknown_list_rejected_but_allowlisted_passes(self):
        with self.assertRaises(ValidationError):
            assert_no_patient_fields({"items": [1, 2]})
        assert_no_patient_fields({"items": [1, 2]}, list_allowlist=frozenset({"items."}))

    def test_patient_field_inside_allowlisted_list_still_rejected(self):
        with self.assertRaises(ValidationError):
            assert_no_patient_fields(
                {"cells": [{"tp": 1, "patient_id": "P1"}]},
                list_allowlist=frozenset({"cells."}),
            )

    def test_cases_key_rejected_even_in_feedback_row(self):
        with self.assertRaises(ValidationError):
            assert_no_patient_fields(
                {"feedback": [{"condition": "hcc", "records": [1]}]},
                list_allowlist=frozenset({"feedback."}),
            )


class PrivacyEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.app, _ = build_app()
        self.v1, _, _ = seed_releases(self.app)
        seed_tenants(self.app, "hosp-a")
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v1["id"]))
        activate(self.app, "hosp-a", self.v1["id"])

    def test_patient_field_inside_cell_rejected(self):
        submission = fixtures.passing_validation(self.v1["id"])
        submission["cells"][0]["patient_mrn"] = "M001"
        from domain.errors import ValidationError as VE
        with self.assertRaises(VE):
            self.app.submit_validation("hosp-a", submission)

    def test_patient_field_inside_feedback_row_rejected(self):
        batches = fixtures.drift_feedback_batches(self.v1["id"])
        batches[0]["feedback"][0]["accession_no"] = "ACC1"
        from domain.errors import ValidationError as VE
        with self.assertRaises(VE):
            self.app.record_feedback("hosp-a", batches[0])


if __name__ == "__main__":
    unittest.main()
