import unittest

from domain.errors import AuthError, ConflictError, ValidationError
from tests.helpers import build_app, seed_releases

import fixtures


class ReleaseRegistryTest(unittest.TestCase):
    def setUp(self):
        self.app, _ = build_app()
        self.v1, self.v1_threshold, self.v2 = seed_releases(self.app)

    def test_fingerprint_changes_with_each_component(self):
        self.assertNotEqual(self.v1["fingerprint"], self.v1_threshold["fingerprint"])
        self.assertNotEqual(self.v1["fingerprint"], self.v2["fingerprint"])
        # 权重摘要差异体现在 v2
        self.assertNotEqual(self.v1["weights"]["sha256"], self.v2["weights"]["sha256"])

    def test_identical_payload_is_conflict(self):
        with self.assertRaises(ConflictError):
            self.app.register_release(fixtures.DEMO_RELEASE_V1)

    def test_threshold_only_diff_is_distinct_release(self):
        self.assertEqual(self.v1["code"], self.v1_threshold["code"])
        self.assertEqual(self.v1["weights"], self.v1_threshold["weights"])
        self.assertNotEqual(self.v1["threshold"], self.v1_threshold["threshold"])

    def test_weights_digest_required(self):
        bad = {**fixtures.DEMO_RELEASE_V1, "weights": {"uri": "s3://x", "sha256": "abc"}}
        with self.assertRaises(ValidationError):
            self.app.register_release(bad)

    def test_commit_must_look_like_git_rev(self):
        bad = {**fixtures.DEMO_RELEASE_V1,
               "code": {"repo": "r", "commit": "no-branch-name"}}
        with self.assertRaises(ValidationError):
            self.app.register_release(bad)

    def test_unknown_condition_rejected(self):
        bad = {**fixtures.DEMO_RELEASE_V1, "conditions": ["hcc", "not_a_disease"]}
        with self.assertRaises(ValidationError):
            self.app.register_release(bad)

    def test_threshold_bounds(self):
        for value in (0.0, 1.0, -0.1, 2.0):
            bad = {**fixtures.DEMO_RELEASE_V1, "threshold": value}
            with self.assertRaises(ValidationError):
                self.app.register_release(bad)

    def test_contraindication_validation(self):
        bad = {**fixtures.DEMO_RELEASE_V1, "contraindications": ["freeform-note"]}
        with self.assertRaises(ValidationError):
            self.app.register_release(bad)
        bad2 = {**fixtures.DEMO_RELEASE_V1, "contraindications": ["condition:unknown_xyz"]}
        with self.assertRaises(ValidationError):
            self.app.register_release(bad2)

    def test_releases_are_immutable_records(self):
        fetched = self.app.get_release(self.v1["id"])
        self.assertEqual(fetched["fingerprint"], self.v1["fingerprint"])
        with self.assertRaises(KeyError):
            _ = fetched["nonexistent"]


class TenantAuthTest(unittest.TestCase):
    def setUp(self):
        self.app, _ = build_app()

    def test_token_roundtrip(self):
        self.app.register_tenant("h1", "一院", "secret-token-123456")
        self.assertEqual(self.app.authenticate("secret-token-123456"), "h1")

    def test_bad_token(self):
        self.app.register_tenant("h1", "一院", "secret-token-123456")
        with self.assertRaises(AuthError):
            self.app.authenticate("secret-token-wrong")
        with self.assertRaises(AuthError):
            self.app.authenticate("")

    def test_duplicate_tenant(self):
        self.app.register_tenant("h1", "一院", "secret-token-123456")
        with self.assertRaises(ConflictError):
            self.app.register_tenant("h1", "一院", "secret-token-abcdef")

    def test_token_too_short(self):
        with self.assertRaises(ValidationError):
            self.app.register_tenant("h2", "二院", "short")


if __name__ == "__main__":
    unittest.main()
