import unittest

from domain.errors import ConflictError, ForbiddenError, StateError, ValidationError
from tests.helpers import activate, approve_twice, build_app, seed_releases, seed_tenants

import fixtures

REQUESTER = fixtures.PEOPLE["requester"]
REV1 = fixtures.PEOPLE["reviewer_1"]
REV2 = fixtures.PEOPLE["reviewer_2"]


class DualApprovalTest(unittest.TestCase):
    def setUp(self):
        self.app, _ = build_app()
        self.v1, _, _ = seed_releases(self.app)
        seed_tenants(self.app, "hosp-a")
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v1["id"]))

    def _request(self):
        return self.app.create_change("hosp-a", {
            "kind": "activate", "release_id": self.v1["id"],
            "requested_by": REQUESTER, "reason": "验证达标，申请上线",
        })

    def test_requires_two_distinct_approvers(self):
        change = self._request()
        self.assertEqual(change["status"], "pending")
        # 申请人不能审批
        with self.assertRaises(ForbiddenError):
            self.app.approve_change("hosp-a", change["id"], REQUESTER)
        # 第一人批准后仍未执行
        once = self.app.approve_change("hosp-a", change["id"], REV1)
        self.assertIsNotNone(once["first_approval"])
        self.assertIsNone(once["second_approval"])
        self.assertEqual(once["status"], "pending")
        # 同一人不能重复批准
        with self.assertRaises(ForbiddenError):
            self.app.approve_change("hosp-a", change["id"], REV1)
        # 第二人批准后执行
        executed = self.app.approve_change("hosp-a", change["id"], REV2)
        self.assertEqual(executed["status"], "executed")
        self.assertEqual(self.app.get_serving("hosp-a")["mode"], "active")

    def test_reason_required(self):
        with self.assertRaises(ValidationError):
            self.app.create_change("hosp-a", {
                "kind": "activate", "release_id": self.v1["id"],
                "requested_by": REQUESTER, "reason": "",
            })

    def test_reject_path(self):
        change = self._request()
        self.app.approve_change("hosp-a", change["id"], REV1)
        rejected = self.app.reject_change("hosp-a", change["id"], REV2, "材料不全")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.app.get_serving("hosp-a")["mode"], "inactive")
        with self.assertRaises(ConflictError):
            self.app.approve_change("hosp-a", change["id"], REV2)

    def test_executed_change_is_frozen_evidence(self):
        change = self._request()
        approve_twice(self.app, "hosp-a", change["id"])
        executed = self.app.list_changes("hosp-a")[0]
        frozen = executed["frozen_evidence"]
        self.assertEqual(frozen["effective_fingerprint"], self.v1["fingerprint"])
        self.assertEqual(frozen["first_approval"]["by"], REV1)
        self.assertEqual(frozen["second_approval"]["by"], REV2)
        # 证据引用绑定批准验证
        ref = executed["evidence_refs"][0]
        self.assertEqual(ref["release_id"], self.v1["id"])
        self.assertIn("evidence_sha256", ref)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.app, _ = build_app()
        self.v1, _, _ = seed_releases(self.app)
        seed_tenants(self.app, "hosp-a", "hosp-b")

    def test_observe_cannot_activate(self):
        self.app.submit_validation(
            "hosp-a", fixtures.insufficient_sample_validation(self.v1["id"])
        )
        with self.assertRaises(StateError):
            self.app.create_change("hosp-a", {
                "kind": "activate", "release_id": self.v1["id"],
                "requested_by": REQUESTER, "reason": "x",
            })

    def test_rejected_cannot_activate(self):
        self.app.submit_validation(
            "hosp-b", fixtures.emergency_subgroup_weak_validation(self.v1["id"])
        )
        with self.assertRaises(StateError):
            self.app.create_change("hosp-b", {
                "kind": "activate", "release_id": self.v1["id"],
                "requested_by": REQUESTER, "reason": "x",
            })

    def test_no_validation_no_activation(self):
        with self.assertRaises(Exception):
            self.app.create_change("hosp-a", {
                "kind": "activate", "release_id": self.v1["id"],
                "requested_by": REQUESTER, "reason": "x",
            })

    def test_cannot_activate_twice(self):
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v1["id"]))
        activate(self.app, "hosp-a", self.v1["id"])
        with self.assertRaises(StateError):
            self.app.create_change("hosp-a", {
                "kind": "activate", "release_id": self.v1["id"],
                "requested_by": REQUESTER, "reason": "x",
            })


class VersionChangeTest(unittest.TestCase):
    def setUp(self):
        self.app, _ = build_app()
        self.v1, self.v1_threshold, self.v2 = seed_releases(self.app)
        seed_tenants(self.app, "hosp-a")
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v1["id"]))
        activate(self.app, "hosp-a", self.v1["id"])

    def test_threshold_change_requires_threshold_only_diff(self):
        # v2 改了代码和权重，不能走 threshold_change
        with self.assertRaises(StateError):
            self.app.create_change("hosp-a", {
                "kind": "threshold_change", "release_id": self.v1["id"],
                "target_release_id": self.v2["id"],
                "requested_by": REQUESTER, "reason": "x",
            })

    def test_threshold_change_needs_target_validation(self):
        # 未对阈值版本做本院验证 → 缺少证据
        with self.assertRaises(StateError):
            self.app.create_change("hosp-a", {
                "kind": "threshold_change", "release_id": self.v1["id"],
                "target_release_id": self.v1_threshold["id"],
                "requested_by": REQUESTER, "reason": "x",
            })

    def test_threshold_change_executes_atomically(self):
        reval = fixtures.passing_validation(self.v1_threshold["id"])
        reval["dataset_id"] = "ds-threshold-042"
        self.app.submit_validation("hosp-a", reval)
        change = self.app.create_change("hosp-a", {
            "kind": "threshold_change", "release_id": self.v1["id"],
            "target_release_id": self.v1_threshold["id"],
            "requested_by": REQUESTER, "reason": "提高急诊灵敏度",
        })
        # 第一人批准不改变线上快照
        self.app.approve_change("hosp-a", change["id"], REV1)
        self.assertEqual(self.app.get_serving("hosp-a")["snapshot"]["threshold"], 0.5)
        self.app.approve_change("hosp-a", change["id"], REV2)
        serving = self.app.get_serving("hosp-a")
        self.assertEqual(serving["release_id"], self.v1_threshold["id"])
        self.assertEqual(serving["snapshot"]["threshold"], 0.42)
        self.assertEqual(serving["snapshot"]["weights_sha256"], self.v1["weights"]["sha256"])

    def test_weights_swap_requires_revalidation_under_new_weights(self):
        with self.assertRaises(StateError):
            self.app.create_change("hosp-a", {
                "kind": "weights_swap", "release_id": self.v1["id"],
                "target_release_id": self.v2["id"],
                "requested_by": REQUESTER, "reason": "x",
            })
        # 用 v2 本院重新验证通过后才允许
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v2["id"]))
        change = self.app.create_change("hosp-a", {
            "kind": "weights_swap", "release_id": self.v1["id"],
            "target_release_id": self.v2["id"],
            "requested_by": REQUESTER, "reason": "升级权重",
        })
        approve_twice(self.app, "hosp-a", change["id"])
        self.assertEqual(self.app.get_serving("hosp-a")["release_id"], self.v2["id"])

    def test_rollback_to_never_active_version_rejected(self):
        # v1_threshold 从未上线，不能作为回滚目标
        reval = fixtures.passing_validation(self.v1_threshold["id"])
        reval["dataset_id"] = "ds-threshold-042"
        self.app.submit_validation("hosp-a", reval)
        with self.assertRaises(StateError):
            self.app.create_change("hosp-a", {
                "kind": "rollback", "release_id": self.v1["id"],
                "target_release_id": self.v1_threshold["id"],
                "requested_by": REQUESTER, "reason": "x",
            })

    def test_rollback_after_swap_uses_historical_evidence(self):
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v2["id"]))
        swap = self.app.create_change("hosp-a", {
            "kind": "weights_swap", "release_id": self.v1["id"],
            "target_release_id": self.v2["id"],
            "requested_by": REQUESTER, "reason": "升级",
        })
        approve_twice(self.app, "hosp-a", swap["id"])
        # v2 出问题，回滚到曾上线的 v1
        rollback = self.app.create_change("hosp-a", {
            "kind": "rollback", "release_id": self.v2["id"],
            "target_release_id": self.v1["id"],
            "requested_by": REQUESTER, "reason": "v2 急诊漏报升高，回滚",
        })
        approve_twice(self.app, "hosp-a", rollback["id"])
        serving = self.app.get_serving("hosp-a")
        self.assertEqual(serving["release_id"], self.v1["id"])
        self.assertEqual(serving["mode"], "active")
        ref = rollback["evidence_refs"][0]
        self.assertEqual(ref["label"], "rollback_evidence")
        self.assertEqual(ref["release_id"], self.v1["id"])


    def test_retire_offline_version_keeps_online_untouched(self):
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v2["id"]))
        swap = self.app.create_change("hosp-a", {
            "kind": "weights_swap", "release_id": self.v1["id"],
            "target_release_id": self.v2["id"],
            "requested_by": REQUESTER, "reason": "升级",
        })
        approve_twice(self.app, "hosp-a", swap["id"])
        # 此时 v2 在线，退役旧版 v1
        retire = self.app.create_change("hosp-a", {
            "kind": "retire", "release_id": self.v1["id"],
            "requested_by": REQUESTER, "reason": "旧版本淘汰",
        })
        approve_twice(self.app, "hosp-a", retire["id"])
        serving = self.app.get_serving("hosp-a")
        self.assertEqual(serving["release_id"], self.v2["id"])
        self.assertEqual(serving["mode"], "active")
        self.assertEqual(
            self.app.get_deployment("hosp-a", self.v1["id"])["state"], "retired"
        )
        # 退役版本不得再作为回滚目标
        with self.assertRaises(StateError):
            self.app.create_change("hosp-a", {
                "kind": "rollback", "release_id": self.v2["id"],
                "target_release_id": self.v1["id"],
                "requested_by": REQUESTER, "reason": "回滚已退役版本",
            })


class CrossTenantApprovalTest(unittest.TestCase):
    def setUp(self):
        self.app, _ = build_app()
        self.v1, _, _ = seed_releases(self.app)
        seed_tenants(self.app, "hosp-a", "hosp-b")
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v1["id"]))

    def test_other_tenant_cannot_see_or_act(self):
        change = self.app.create_change("hosp-a", {
            "kind": "activate", "release_id": self.v1["id"],
            "requested_by": REQUESTER, "reason": "x",
        })
        with self.assertRaises(ForbiddenError):
            self.app.approve_change("hosp-b", change["id"], REV1)
        self.assertEqual(self.app.list_changes("hosp-b"), [])


if __name__ == "__main__":
    unittest.main()
