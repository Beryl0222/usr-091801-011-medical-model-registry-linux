"""领域规则测试：准入复算、漂移降级、双人审批、调用核对与机构隔离。"""

import json
import unittest
from pathlib import Path

from registry import api
from registry.checklist import load_checklist
from registry.engine import run_scenario
from registry.store import DomainError, RegistryStore

ROOT = Path(__file__).resolve().parent
SCENARIO_PATH = ROOT / "data" / "simulated_scenario.json"

RELEASE_FIELDS = {
    "model_name": "abdomen-ct-open",
    "model_version": "1.3.0",
    "code_ref": "github://example/abdomen-ct@abc123",
    "weights_hash": "sha256:weights-v1",
    "runtime_env": "python3.11/onnxruntime1.16/cuda12.2",
    "thresholds": {"default": 0.5},
    "applicable_organs": ["肝", "胰", "脾"],
    "scan_protocols": ["增强CT"],
    "contraindications": ["儿童腹部扫描"],
}


def make_record(disease="肝血管瘤", subgroup="急诊", sample_size=50,
                sensitivity=0.9, specificity=0.88, auc=0.9, ci_halfwidth=0.05):
    def ci(value):
        return [round(max(0.0, value - ci_halfwidth), 4), round(min(1.0, value + ci_halfwidth), 4)]

    return {
        "disease": disease,
        "subgroup": subgroup,
        "sample_size": sample_size,
        "sensitivity": sensitivity,
        "specificity": specificity,
        "auc": auc,
        "sensitivity_ci": ci(sensitivity),
        "specificity_ci": ci(specificity),
        "auc_ci": ci(auc),
    }


def passing_records():
    return [
        make_record(disease="肝血管瘤", subgroup="急诊"),
        make_record(disease="肝血管瘤", subgroup="非急诊", sample_size=120),
    ]


class ReleaseIdentityTest(unittest.TestCase):
    def setUp(self):
        self.store = RegistryStore()

    def test_same_content_same_identity(self):
        first, created_first = self.store.register_release(actor="t", **RELEASE_FIELDS)
        second, created_second = self.store.register_release(actor="t", **RELEASE_FIELDS)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["release_id"], second["release_id"])

    def test_different_weights_different_identity(self):
        base, _ = self.store.register_release(actor="t", **RELEASE_FIELDS)
        swapped, _ = self.store.register_release(
            actor="t", **{**RELEASE_FIELDS, "weights_hash": "sha256:weights-v2"}
        )
        self.assertNotEqual(base["release_id"], swapped["release_id"])

    def test_missing_identity_field_rejected(self):
        fields = {key: value for key, value in RELEASE_FIELDS.items() if key != "weights_hash"}
        with self.assertRaises(DomainError) as ctx:
            self.store.register_release(actor="t", **fields)
        self.assertEqual(ctx.exception.status, 400)


class AdmissionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checklist = load_checklist()

    def setUp(self):
        self.store = RegistryStore()
        self.store.add_hospital(hospital_id="H1", name="医院一")
        self.release, _ = self.store.register_release(actor="t", **RELEASE_FIELDS)
        self.release_id = self.release["release_id"]

    def submit(self, records):
        return self.store.submit_validation(
            self.checklist, "H1", self.release_id, records, actor="test"
        )

    def test_all_pass_approved(self):
        batch = self.submit(passing_records())
        self.assertEqual(batch["decision"], "approved")
        key = f"H1|{self.release_id}"
        self.assertEqual(self.store.deployments[key]["status"], "approved")
        self.assertEqual(self.store.current["H1"], self.release_id)

    def test_borderline_goes_observation(self):
        records = passing_records()
        records[0]["sensitivity"] = 0.83  # 阈值 0.85，临界带 0.03 内
        records[0]["sensitivity_ci"] = [0.78, 0.88]
        batch = self.submit(records)
        self.assertEqual(batch["decision"], "observation")

    def test_hard_failure_rejected_and_no_deployment(self):
        records = passing_records()
        records[0]["sensitivity"] = 0.5
        records[0]["sensitivity_ci"] = [0.4, 0.6]
        batch = self.submit(records)
        self.assertEqual(batch["decision"], "rejected")
        self.assertNotIn(f"H1|{self.release_id}", self.store.deployments)
        self.assertNotIn("H1", self.store.current)

    def test_average_cannot_mask_subgroup(self):
        # 非急诊亚组接近满分，急诊亚组严重不达标：总平均再高也只能驳回。
        records = [
            make_record(subgroup="急诊", sensitivity=0.6, specificity=0.99, auc=0.99),
            make_record(subgroup="非急诊", sample_size=500, sensitivity=0.99,
                        specificity=0.99, auc=0.99),
        ]
        records[0]["sensitivity_ci"] = [0.5, 0.7]
        batch = self.submit(records)
        self.assertEqual(batch["decision"], "rejected")

    def test_missing_required_subgroup_blocks_approval(self):
        batch = self.submit([make_record(subgroup="急诊")])
        self.assertEqual(batch["decision"], "observation")
        kinds = {failure["kind"] for failure in batch["failures"]}
        self.assertIn("missing_subgroup", kinds)

    def test_ci_lower_bound_shortfall_goes_observation(self):
        record = make_record(sensitivity=0.9, ci_halfwidth=0.25)
        record["sensitivity_ci"] = [0.65, 0.98]  # 下限低于清单要求的 0.70
        batch = self.submit([record, make_record(subgroup="非急诊")])
        self.assertEqual(batch["decision"], "observation")
        kinds = {failure["kind"] for failure in batch["failures"]}
        self.assertIn("sensitivity_ci_lower", kinds)

    def test_small_sample_goes_observation(self):
        records = [make_record(sample_size=10), make_record(subgroup="非急诊")]
        batch = self.submit(records)
        self.assertEqual(batch["decision"], "observation")
        kinds = {failure["kind"] for failure in batch["failures"]}
        self.assertIn("sample_size", kinds)

    def test_patient_level_fields_rejected(self):
        record = {**make_record(), "patient_id": "P-123"}
        with self.assertRaises(DomainError) as ctx:
            self.submit([record])
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("患者级", ctx.exception.message)

    def test_unknown_field_rejected(self):
        record = {**make_record(), "notes": "extra"}
        with self.assertRaises(DomainError) as ctx:
            self.submit([record])
        self.assertEqual(ctx.exception.status, 400)

    def test_ci_must_contain_point_estimate(self):
        record = make_record(sensitivity=0.9)
        record["sensitivity_ci"] = [0.5, 0.8]
        with self.assertRaises(DomainError):
            self.submit([record, make_record(subgroup="非急诊")])


class DriftTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checklist = load_checklist()

    def setUp(self):
        self.store = RegistryStore()
        self.store.add_hospital(hospital_id="H1", name="医院一")
        self.release, _ = self.store.register_release(actor="t", **RELEASE_FIELDS)
        self.release_id = self.release["release_id"]
        self.store.submit_validation(
            self.checklist, "H1", self.release_id, passing_records(), actor="test"
        )

    def feed(self, kinds):
        events = [
            {"hospital_id": "H1", "release_id": self.release_id, "kind": kind}
            for kind in kinds
        ]
        return self.store.add_feedback(self.checklist, events)

    def deployment_status(self):
        return self.store.deployments[f"H1|{self.release_id}"]["status"]

    def test_below_threshold_no_action(self):
        actions = self.feed(["adopt"] * 18 + ["override"] * 2)
        self.assertEqual(actions, [])
        self.assertEqual(self.deployment_status(), "approved")

    def test_min_window_not_reached_no_action(self):
        actions = self.feed(["miss"] * 5)
        self.assertEqual(actions, [])

    def test_breach_downgrades_to_advisory_and_opens_rereview(self):
        self.feed(["adopt"] * 20)
        actions = self.feed(["miss"] * 3 + ["adopt"] * 17)
        self.assertTrue(actions)
        self.assertEqual(actions[0]["action"], "downgraded_to_advisory_only")
        self.assertEqual(self.deployment_status(), "advisory_only")
        open_reviews = [r for r in self.store.rereviews if r["status"] == "open"]
        self.assertEqual(len(open_reviews), 1)

    def test_continued_breach_no_duplicate_downgrade_or_rereview(self):
        self.feed(["adopt"] * 20)
        self.feed(["miss"] * 3 + ["adopt"] * 17)
        actions = self.feed(["miss"] * 3)
        self.assertTrue(all(a["action"] == "recorded" for a in actions))
        self.assertEqual(len(self.store.rereviews), 1)

    def test_override_rate_breach(self):
        self.feed(["adopt"] * 20)
        actions = self.feed(["override"] * 6 + ["adopt"] * 14)
        self.assertTrue(actions)
        self.assertEqual(actions[0]["breaches"][0]["kind"], "override_rate")


class ChangeRequestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checklist = load_checklist()

    def setUp(self):
        self.store = RegistryStore()
        self.store.add_hospital(hospital_id="H1", name="医院一")
        self.release, _ = self.store.register_release(actor="t", **RELEASE_FIELDS)
        self.release_id = self.release["release_id"]
        self.store.submit_validation(
            self.checklist, "H1", self.release_id, passing_records(), actor="test"
        )

    def create(self, kind="adjust_threshold", payload=None, evidence=("val-1",), requester="eng.a"):
        if payload is None:
            payload = {"new_thresholds": {"default": 0.45}}
        return self.store.create_change_request(
            "H1", kind, payload, list(evidence), requester
        )

    def test_evidence_required(self):
        with self.assertRaises(DomainError) as ctx:
            self.store.create_change_request(
                "H1", "adjust_threshold", {"new_thresholds": {"default": 0.45}}, [], "eng.a"
            )
        self.assertEqual(ctx.exception.status, 400)

    def test_requester_cannot_approve(self):
        request = self.create()
        with self.assertRaises(DomainError) as ctx:
            self.store.approve_change_request(request["request_id"], "eng.a")
        self.assertEqual(ctx.exception.status, 403)

    def test_same_approver_cannot_approve_twice(self):
        request = self.create()
        self.store.approve_change_request(request["request_id"], "rev.1")
        with self.assertRaises(DomainError) as ctx:
            self.store.approve_change_request(request["request_id"], "rev.1")
        self.assertEqual(ctx.exception.status, 409)

    def test_two_approvals_apply_threshold_change(self):
        request = self.create()
        self.store.approve_change_request(request["request_id"], "rev.1")
        request = self.store.approve_change_request(request["request_id"], "rev.2")
        self.assertEqual(request["status"], "applied")
        new_id = request["applied"]["new_release_id"]
        self.assertNotEqual(new_id, self.release_id)
        self.assertEqual(self.store.releases[new_id]["thresholds"], {"default": 0.45})
        self.assertEqual(self.store.current["H1"], new_id)
        # 新版本必须重新本地验证，先进入观察。
        self.assertEqual(self.store.deployments[f"H1|{new_id}"]["status"], "observation")

    def test_replace_weights_creates_new_release(self):
        request = self.create(
            kind="replace_weights", payload={"new_weights_hash": "sha256:weights-v2"}
        )
        self.store.approve_change_request(request["request_id"], "rev.1")
        request = self.store.approve_change_request(request["request_id"], "rev.2")
        new_id = request["applied"]["new_release_id"]
        self.assertEqual(self.store.releases[new_id]["weights_hash"], "sha256:weights-v2")
        self.assertEqual(self.store.releases[new_id]["code_ref"], RELEASE_FIELDS["code_ref"])

    def test_rollback_restores_previous_release_and_status(self):
        request = self.create(
            kind="replace_weights", payload={"new_weights_hash": "sha256:weights-v2"}
        )
        self.store.approve_change_request(request["request_id"], "rev.1")
        self.store.approve_change_request(request["request_id"], "rev.2")
        rollback = self.create(
            kind="rollback", payload={"target_release_id": self.release_id}
        )
        self.store.approve_change_request(rollback["request_id"], "rev.1")
        rollback = self.store.approve_change_request(rollback["request_id"], "rev.2")
        self.assertEqual(rollback["status"], "applied")
        self.assertEqual(self.store.current["H1"], self.release_id)
        self.assertEqual(
            self.store.deployments[f"H1|{self.release_id}"]["status"], "approved"
        )

    def test_rollback_requires_existing_deployment(self):
        other, _ = self.store.register_release(
            actor="t", **{**RELEASE_FIELDS, "weights_hash": "sha256:weights-v9"}
        )
        with self.assertRaises(DomainError) as ctx:
            self.create(kind="rollback", payload={"target_release_id": other["release_id"]})
        self.assertEqual(ctx.exception.status, 409)

    def test_applied_change_closes_referenced_rereview(self):
        # 先制造漂移，开启复审。
        events = [{"hospital_id": "H1", "release_id": self.release_id, "kind": "adopt"}] * 20
        self.store.add_feedback(self.checklist, events)
        events = [
            {"hospital_id": "H1", "release_id": self.release_id, "kind": kind}
            for kind in ["miss"] * 3 + ["adopt"] * 17
        ]
        actions = self.store.add_feedback(self.checklist, events)
        report_id = actions[0]["report_id"]
        request = self.create(evidence=(report_id,))
        self.store.approve_change_request(request["request_id"], "rev.1")
        request = self.store.approve_change_request(request["request_id"], "rev.2")
        self.assertEqual(request["applied"]["closed_rereviews"], ["rr-1"])
        self.assertEqual(self.store.rereviews[0]["status"], "closed")


class VerifyCallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checklist = load_checklist()

    def setUp(self):
        self.store = RegistryStore()
        self.store.add_hospital(hospital_id="H1", name="医院一")
        self.store.add_hospital(hospital_id="H2", name="医院二")
        self.release, _ = self.store.register_release(actor="t", **RELEASE_FIELDS)
        self.release_id = self.release["release_id"]
        self.store.submit_validation(
            self.checklist, "H1", self.release_id, passing_records(), actor="test"
        )

    def test_approved_hit_full(self):
        audit = self.store.verify_call("H1", self.release_id)
        self.assertEqual(audit["result"], "hit")
        self.assertEqual(audit["detail"], {"mode": "full"})

    def test_advisory_only_hit_with_mode(self):
        events = [
            {"hospital_id": "H1", "release_id": self.release_id, "kind": kind}
            for kind in ["adopt"] * 20 + ["miss"] * 3 + ["adopt"] * 17
        ]
        self.store.add_feedback(self.checklist, events)
        audit = self.store.verify_call("H1", self.release_id)
        self.assertEqual(audit["result"], "hit")
        self.assertEqual(audit["detail"], {"mode": "advisory_only"})

    def test_observation_status_not_authorized(self):
        self.store.set_deployment_status(
            "H1", self.release_id, "observation", evidence_ref="val-1", actor="op"
        )
        audit = self.store.verify_call("H1", self.release_id)
        self.assertEqual(audit["result"], "miss")
        self.assertEqual(audit["detail"]["reason"], "status_not_authorized")

    def test_wrong_version_miss(self):
        other, _ = self.store.register_release(
            actor="t", **{**RELEASE_FIELDS, "weights_hash": "sha256:weights-v2"}
        )
        audit = self.store.verify_call("H1", other["release_id"])
        self.assertEqual(audit["result"], "miss")
        self.assertEqual(audit["detail"]["reason"], "wrong_version")
        self.assertEqual(audit["detail"]["expected_release_id"], self.release_id)

    def test_unknown_release_miss(self):
        audit = self.store.verify_call("H1", "rel-does-not-exist")
        self.assertEqual(audit["detail"]["reason"], "unknown_release")

    def test_unknown_hospital_miss(self):
        audit = self.store.verify_call("H9", self.release_id)
        self.assertEqual(audit["detail"]["reason"], "unknown_hospital")

    def test_hospital_without_validation_miss(self):
        audit = self.store.verify_call("H2", self.release_id)
        self.assertEqual(audit["detail"]["reason"], "no_approved_release")

    def test_audits_are_recorded(self):
        self.store.verify_call("H1", self.release_id)
        self.store.verify_call("H1", "rel-does-not-exist")
        self.assertEqual(len(self.store.call_audits), 2)


class ApiScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checklist = load_checklist()

    def setUp(self):
        self.store = RegistryStore()
        self.release, _ = self.store.register_release(actor="t", **RELEASE_FIELDS)
        self.store.add_hospital(hospital_id="H1", name="医院一")
        self.store.add_hospital(hospital_id="H2", name="医院二")
        self.store.submit_validation(
            self.checklist, "H1", self.release["release_id"], passing_records(), actor="test"
        )

    def dispatch(self, method, path, query=None, headers=None, body=None):
        return api.dispatch(
            method, path, query or {}, headers or {}, body, self.store, self.checklist
        )

    def test_cross_hospital_read_denied(self):
        status, payload = self.dispatch(
            "GET", "/validations", {"hospital_id": "H1"}, {"x-hospital-id": "H2"}
        )
        self.assertEqual(status, 403)

    def test_own_hospital_read_allowed(self):
        status, payload = self.dispatch(
            "GET", "/validations", {"hospital_id": "H1"}, {"x-hospital-id": "H1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["validations"]), 1)

    def test_operator_can_read_across_hospitals(self):
        status, _ = self.dispatch(
            "GET", "/validations", {"hospital_id": "H1"},
            {"x-hospital-id": "H2", "x-role": "operator"},
        )
        self.assertEqual(status, 200)

    def test_cross_hospital_submit_denied(self):
        status, _ = self.dispatch(
            "POST", "/validations", {},
            {"x-hospital-id": "H2"},
            {"hospital_id": "H1", "release_id": self.release["release_id"],
             "records": passing_records()},
        )
        self.assertEqual(status, 403)

    def test_patient_level_payload_rejected_via_api(self):
        record = {**make_record(), "patient_id": "P-1"}
        status, payload = self.dispatch(
            "POST", "/validations", {},
            {"x-hospital-id": "H1"},
            {"hospital_id": "H1", "release_id": self.release["release_id"],
             "records": [record]},
        )
        self.assertEqual(status, 400)
        self.assertIn("患者级", payload["error"])

    def test_manual_status_requires_operator(self):
        status, _ = self.dispatch(
            "POST", "/deployments/status", {},
            {"x-hospital-id": "H1"},
            {"hospital_id": "H1", "release_id": self.release["release_id"],
             "status": "suspended", "evidence_ref": "val-1"},
        )
        self.assertEqual(status, 403)

    def test_unknown_route_404(self):
        status, _ = self.dispatch("GET", "/nope")
        self.assertEqual(status, 404)


class ScenarioReplayTest(unittest.TestCase):
    """用仓库中的模拟指标复算准入、回放漂移、核对调用。"""

    @classmethod
    def setUpClass(cls):
        cls.checklist = load_checklist()
        cls.scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))

    def replay(self, include):
        return run_scenario(RegistryStore(), self.checklist, self.scenario, include=include)

    def test_evaluate_recomputes_admission(self):
        report = self.replay(("validation", "change_request", "approve"))
        decisions = {
            (d["hospital_id"], d["release_ref"]): d["decision"]
            for d in report["decisions"]
        }
        self.assertEqual(decisions[("HOSP-A", "R1")], "approved")
        self.assertEqual(decisions[("HOSP-B", "R1")], "observation")
        self.assertEqual(decisions[("HOSP-C", "R1")], "rejected")
        self.assertEqual(decisions[("HOSP-A", "R1B")], "approved")

    def test_drift_replay_downgrades_once(self):
        report = self.replay(("validation", "feedback", "change_request", "approve"))
        actions = report["drift_actions"]
        self.assertTrue(actions)
        self.assertEqual(actions[0]["action"], "downgraded_to_advisory_only")
        self.assertTrue(all(a["action"] == "recorded" for a in actions[1:]))
        self.assertEqual(len({a["rereview_id"] for a in actions}), 1)

    def test_verify_calls_full_timeline(self):
        report = self.replay(
            ("validation", "feedback", "change_request", "approve", "call")
        )
        results = [(c["result"], c["detail"]) for c in report["call_results"]]
        expected = [
            ("hit", {"mode": "full"}),
            ("miss", {"reason": "status_not_authorized", "status": "observation"}),
            ("miss", {"reason": "no_approved_release"}),
            ("hit", {"mode": "advisory_only"}),
            ("miss", None),  # wrong_version，expected_release_id 为内容哈希，单独断言
            ("miss", {"reason": "status_not_authorized", "status": "observation"}),
            ("hit", {"mode": "full"}),
            ("miss", {"reason": "unknown_release"}),
        ]
        self.assertEqual(len(results), len(expected))
        for (result, detail), (want_result, want_detail) in zip(results, expected):
            self.assertEqual(result, want_result)
            if want_detail is not None:
                self.assertEqual(detail, want_detail)
        self.assertEqual(results[4][1]["reason"], "wrong_version")


class SnapshotTest(unittest.TestCase):
    def test_snapshot_roundtrip(self):
        checklist = load_checklist()
        store = RegistryStore()
        store.add_hospital(hospital_id="H1", name="医院一")
        release, _ = store.register_release(actor="t", **RELEASE_FIELDS)
        store.submit_validation(
            checklist, "H1", release["release_id"], passing_records(), actor="test"
        )
        clone = RegistryStore.from_dict(store.to_dict())
        self.assertEqual(clone.to_dict(), store.to_dict())
        audit = clone.verify_call("H1", release["release_id"])
        self.assertEqual(audit["result"], "hit")


if __name__ == "__main__":
    unittest.main()
