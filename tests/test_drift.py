import unittest

from domain.errors import StateError, ValidationError
from tests.helpers import activate, approve_twice, build_app, seed_releases, seed_tenants

import fixtures

REQUESTER = fixtures.PEOPLE["requester"]
REV1 = fixtures.PEOPLE["reviewer_1"]
REV2 = fixtures.PEOPLE["reviewer_2"]


def _feedback_row(condition, stratum, accepted, rejected, corrected, confirmed):
    return {
        "condition": condition,
        "stratum": stratum,
        "model_positive_accepted": accepted,
        "model_positive_rejected": rejected,
        "model_negative_corrected": corrected,
        "model_negative_confirmed": confirmed,
    }


class DriftReplayTest(unittest.TestCase):
    def setUp(self):
        self.app, self.clock = build_app()
        self.v1, self.v1_threshold, _ = seed_releases(self.app)
        seed_tenants(self.app, "hosp-a")
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v1["id"]))
        activate(self.app, "hosp-a", self.v1["id"])

    def _feed(self, batches):
        events = []
        for batch in batches:
            result = self.app.record_feedback("hosp-a", batch)
            if result["drift_event"]:
                events.append(result["drift_event"])
        return events

    def test_full_timeline_degrades_then_reviews_then_resume(self):
        events = self._feed(fixtures.drift_feedback_batches(self.v1["id"]))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["action"], "degrade_to_advisory_and_review")
        serving = self.app.get_serving("hosp-a")
        self.assertEqual(serving["mode"], "advisory")
        self.assertEqual(serving["review_status"], "review_triggered")
        self.assertTrue(
            all(b["condition"] == "appendicitis" and b["stratum"] == "emergency"
                for b in event["breaches"])
        )

        reviews = self.app.list_reviews("hosp-a", status="open")
        self.assertEqual(len(reviews), 1)

        # 降级期间不得直接 active：resume 前需要重新验证
        with self.assertRaises(StateError):
            self.app.create_change("hosp-a", {
                "kind": "resume", "release_id": self.v1["id"],
                "requested_by": REQUESTER, "reason": "未重新验证直接恢复",
            })
        self.clock.advance(3600)
        reval = self.app.submit_validation(
            "hosp-a", fixtures.revalidation_after_drift(self.v1["id"])
        )
        self.assertEqual(reval["decision"], "approve")
        resume = self.app.create_change("hosp-a", {
            "kind": "resume", "release_id": self.v1["id"],
            "requested_by": REQUESTER, "reason": "排查并重新验证达标",
        })
        approve_twice(self.app, "hosp-a", resume["id"])
        self.assertEqual(self.app.get_serving("hosp-a")["mode"], "active")
        self.assertEqual(self.app.list_reviews("hosp-a", status="open"), [])

        # 窗口已重置：历史越限批次不再继续触发降级
        signals = self.app.drift_signals("hosp-a", self.v1["id"])
        self.assertTrue(all(s["window_counts"]["accepted"] == 0 for s in signals))

    def test_no_breach_before_final_batch(self):
        batches = fixtures.drift_feedback_batches(self.v1["id"])
        self._feed(batches[:-1])
        self.assertEqual(self.app.get_serving("hosp-a")["mode"], "active")
        self.assertEqual(self.app.drift_events("hosp-a"), [])

    def test_duplicate_batch_not_double_counted(self):
        batches = fixtures.drift_feedback_batches(self.v1["id"])
        self.app.record_feedback("hosp-a", batches[0])
        with self.assertRaises(ValidationError):
            self.app.record_feedback("hosp-a", batches[0])

    def test_window_expires_old_batches(self):
        # 自定义策略：窗口只保留 1 天
        app, clock = build_app(drift_policy={
            "window_batches": 5,
            "window_max_age_days": 1,
            "routine": {"miss_rate_max": 0.1, "override_rate_max": 0.15,
                        "min_positive_denominator": 1, "min_negative_denominator": 1},
            "emergency": {"miss_rate_max": 0.05, "override_rate_max": 0.10,
                          "min_positive_denominator": 1, "min_negative_denominator": 1},
        })
        v1, _, _ = seed_releases(app)
        seed_tenants(app, "hosp-a")
        app.submit_validation("hosp-a", fixtures.passing_validation(v1["id"]))
        activate(app, "hosp-a", v1["id"])
        batch = {
            "release_id": v1["id"],
            "batch_id": "old-batch",
            "feedback": [_feedback_row("appendicitis", "emergency", 5, 0, 5, 10)],
        }
        app.record_feedback("hosp-a", batch)
        self.assertEqual(app.get_serving("hosp-a")["mode"], "advisory")
        # 两天后的批次不再计入旧越限数据；但旧事件仍在
        clock.advance(2 * 86400)
        signals = app.drift_signals("hosp-a", v1["id"])
        emergency = next(s for s in signals if s["stratum"] == "emergency")
        self.assertEqual(emergency["window_counts"]["corrected"], 0)

    def test_feedback_only_for_online_release(self):
        with self.assertRaises(StateError):
            self.app.record_feedback("hosp-a", {
                "release_id": self.v1_threshold["id"],
                "batch_id": "x",
                "feedback": [_feedback_row("hcc", "routine", 1, 0, 0, 1)],
            })

    def test_patient_level_feedback_rejected(self):
        with self.assertRaises(ValidationError):
            self.app.record_feedback("hosp-a", {
                "release_id": self.v1["id"],
                "batch_id": "x",
                "patient_id": "P1",
                "feedback": [_feedback_row("hcc", "routine", 1, 0, 0, 1)],
            })


class CallVerificationTest(unittest.TestCase):
    def setUp(self):
        self.app, _ = build_app()
        self.v1, self.v1_threshold, self.v2 = seed_releases(self.app)
        seed_tenants(self.app, "hosp-a", "hosp-b")
        self.app.submit_validation("hosp-a", fixtures.passing_validation(self.v1["id"]))
        activate(self.app, "hosp-a", self.v1["id"])

    def _call(self, **overrides):
        payload = {
            "release_id": self.v1["id"],
            "fingerprint": self.v1["fingerprint"],
            "protocol": "noncontrast",
            "condition": "appendicitis",
        }
        payload.update(overrides)
        return self.app.verify_call("hosp-a", payload)

    def test_matching_call(self):
        result = self._call()
        self.assertTrue(result["matched"])
        self.assertFalse(result["advisory_only"])

    def test_fingerprint_mismatch_recorded(self):
        result = self._call(fingerprint="0" * 64)
        self.assertFalse(result["matched"])
        self.assertEqual(result["mismatch_reason"], "fingerprint_mismatch")
        events = self.app.call_events("hosp-a", mismatch_only=True)
        self.assertTrue(any(e["mismatch_reason"] == "fingerprint_mismatch" for e in events))

    def test_release_mismatch(self):
        result = self._call(release_id=self.v2["id"], fingerprint=self.v2["fingerprint"])
        self.assertEqual(result["mismatch_reason"], "release_mismatch")

    def test_protocol_out_of_scope(self):
        # hcc 不支持平扫
        result = self._call(condition="hcc", protocol="noncontrast")
        self.assertEqual(result["mismatch_reason"], "protocol_not_covered")

    def test_condition_not_covered(self):
        result = self._call(condition="acute_pancreatitis")
        self.assertEqual(result["mismatch_reason"], "condition_not_covered")

    def test_advisory_mode_is_flagged_but_allowed(self):
        # 触发漂移降级
        for batch in fixtures.drift_feedback_batches(self.v1["id"]):
            self.app.record_feedback("hosp-a", batch)
        result = self._call()
        self.assertTrue(result["matched"])
        self.assertTrue(result["advisory_only"])
        self.assertEqual(result["mode"], "advisory")

    def test_call_events_are_tenant_scoped(self):
        self._call()
        self.assertEqual(self.app.call_events("hosp-b"), [])

    def test_call_payload_cannot_carry_patient_data(self):
        with self.assertRaises(ValidationError):
            self.app.verify_call("hosp-a", {
                "release_id": self.v1["id"],
                "fingerprint": self.v1["fingerprint"],
                "protocol": "noncontrast",
                "patient_id": "P9",
            })


if __name__ == "__main__":
    unittest.main()
