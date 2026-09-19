"""上线后的漂移监测与线上调用版本核对。

两部分能力：

1. 漂移监测：临床使用反馈以**汇总批次**进入（禁止患者级字段）。每个
   病种×亚组维护最近若干批次的滑动窗口，由计数复算漏报率与改判率
   （Wilson 区间）。窗口指标越过阈值且分母足够时，自动把线上版本
   active → advisory（仅提示），同时开立复审。复审关闭前，恢复上线
   必须提交晚于降级时间的新验证并经双人批准（见 lifecycle.resume）。

2. 调用核对：线上推理服务每次调用前/后上报命中的 release_id、指纹、
   协议与病种，本服务与该院当前 serving 快照逐项比对；指纹不符、
   版本不符、协议超范围等全部留证。advisory 模式允许调用但结果只能提示。
"""

from . import catalog
from .errors import StateError, ValidationError
from .privacy import assert_no_patient_fields
from .stats import round3, wilson_interval

ROUTINE = "routine"
EMERGENCY = "emergency"

# 默认漂移策略：急诊亚组漏报容忍更低、所需分母更小（病例少但代价高）
DEFAULT_DRIFT_POLICY = {
    "window_batches": 5,
    "window_max_age_days": 90,
    "routine": {
        "miss_rate_max": 0.10,
        "override_rate_max": 0.15,
        "min_positive_denominator": 40,   # 采纳+漏报纠正
        "min_negative_denominator": 40,   # 阴性确认+改判
    },
    "emergency": {
        "miss_rate_max": 0.05,
        "override_rate_max": 0.10,
        "min_positive_denominator": 20,
        "min_negative_denominator": 20,
    },
}

_FEEDBACK_KEYS = frozenset({
    "condition", "stratum",
    "model_positive_accepted", "model_positive_rejected",
    "model_negative_corrected", "model_negative_confirmed",
})
_ALLOWED_FEEDBACK_TOP = frozenset({"release_id", "batch_id", "feedback"})
_CALL_KEYS = frozenset({
    "release_id", "fingerprint", "protocol", "condition", "mode_seen", "call_count",
})

SIGNAL_NORMAL = "normal"
SIGNAL_WARNING = "warning"
SIGNAL_BREACH = "breach"


def _as_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{field} 必须是非负整数")
    return value


class DriftMonitor:
    def __init__(self, store, clock, registry, lifecycle, policy=None):
        self.store = store
        self.clock = clock
        self.registry = registry
        self.lifecycle = lifecycle
        self.policy = policy or DEFAULT_DRIFT_POLICY
        self.store.data.setdefault("feedback", {})
        self.store.data.setdefault("drift_events", [])
        self.store.data.setdefault("drift_seq", self.store.data.get("drift_seq", 0))
        self.store.data.setdefault("reviews", {})
        self.store.data.setdefault("call_events", [])

    # ── 反馈录入 ───────────────────────────────────────────
    def record_feedback(self, hospital_id, payload):
        if not isinstance(payload, dict):
            raise ValidationError("反馈必须是对象")
        extra = set(payload) - _ALLOWED_FEEDBACK_TOP
        if extra:
            raise ValidationError(f"反馈包含不允许的字段（疑似患者级数据）: {sorted(extra)}")
        assert_no_patient_fields(payload, list_allowlist=frozenset({"feedback."}))

        release_id = payload.get("release_id")
        release = self.registry.get_release(release_id)
        serving = self.lifecycle.get_serving(hospital_id)
        if serving["release_id"] != release_id:
            raise StateError("只接收本院当前线上版本的反馈（历史版本请走重新验证流程）")
        if serving["mode"] not in ("active", "advisory"):
            raise StateError(f"当前线上模式 {serving['mode']} 不接收使用反馈")

        batch_id = payload.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValidationError("batch_id 不能为空")

        rows = payload.get("feedback")
        if not isinstance(rows, list) or not rows:
            raise ValidationError("feedback 必须是非空列表")

        normalized = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) - _FEEDBACK_KEYS:
                raise ValidationError("反馈行字段非法，仅接受病种×亚组的汇总计数")
            condition = row.get("condition")
            if condition not in release["conditions"]:
                raise ValidationError(f"病种 {condition} 不在该版本适用范围")
            stratum = row.get("stratum", ROUTINE)
            if stratum not in (ROUTINE, EMERGENCY):
                raise ValidationError("stratum 必须是 routine/emergency")
            if (condition, stratum) in seen:
                raise ValidationError(f"反馈行重复: {condition}/{stratum}")
            seen.add((condition, stratum))
            counts = {
                "accepted": _as_int(row.get("model_positive_accepted", 0), "model_positive_accepted"),
                "rejected": _as_int(row.get("model_positive_rejected", 0), "model_positive_rejected"),
                "corrected": _as_int(row.get("model_negative_corrected", 0), "model_negative_corrected"),
                "confirmed": _as_int(row.get("model_negative_confirmed", 0), "model_negative_confirmed"),
            }
            if sum(counts.values()) == 0:
                raise ValidationError(f"{condition}/{stratum} 反馈计数全为 0")
            normalized.append({"condition": condition, "stratum": stratum, **counts})

        now = self.clock()
        for row in normalized:
            key = f"{hospital_id}:{release_id}:{row['condition']}:{row['stratum']}"
            bucket = self.store.data["feedback"].setdefault(key, [])
            if any(b["batch_id"] == batch_id for b in bucket):
                raise ValidationError(f"批次 {batch_id} 已录入，禁止重复计入窗口")
            bucket.append({"batch_id": batch_id.strip(), "at": now, **row})

        self.store.save()

        # 录入后立即复算所有相关窗口，必要时触发降级与复审
        signals = [
            self._window_signal(hospital_id, release_id, row["condition"], row["stratum"])
            for row in normalized
        ]
        breach = [s for s in signals if s["status"] == SIGNAL_BREACH]
        drift_event = None
        if breach and serving["mode"] == "active":
            drift_event = self._trigger_degrade(hospital_id, release_id, batch_id, breach)
        return {
            "batch_id": batch_id,
            "received_rows": len(normalized),
            "signals": signals,
            "drift_event": drift_event,
        }

    # ── 窗口复算 ───────────────────────────────────────────
    def _window(self, hospital_id, release_id, condition, stratum):
        key = f"{hospital_id}:{release_id}:{condition}:{stratum}"
        bucket = self.store.data["feedback"].get(key, [])
        max_age = self.policy["window_max_age_days"] * 86400
        fresh = [b for b in bucket if self.clock() - b["at"] <= max_age]
        return fresh[-self.policy["window_batches"]:]

    def _window_signal(self, hospital_id, release_id, condition, stratum):
        batches = self._window(hospital_id, release_id, condition, stratum)
        totals = {"accepted": 0, "rejected": 0, "corrected": 0, "confirmed": 0}
        for batch in batches:
            for k in totals:
                totals[k] += batch[k]

        policy = self.policy[stratum]
        pos_den = totals["accepted"] + totals["corrected"]   # 真阳性代理分母
        neg_den = totals["confirmed"] + totals["rejected"]   # 真阴性代理分母
        misses = totals["corrected"]
        overrides = totals["rejected"]

        miss_rate = misses / pos_den if pos_den else None
        override_rate = overrides / neg_den if neg_den else None
        miss_ci = wilson_interval(misses, pos_den) if pos_den else (None, None)
        override_ci = wilson_interval(overrides, neg_den) if neg_den else (None, None)

        checks = []

        def classify(rate, ci, maximum, denominator, min_den):
            if denominator < min_den:
                return SIGNAL_NORMAL, "insufficient_denominator"
            if rate is not None and rate > maximum:
                return SIGNAL_BREACH, "threshold_exceeded"
            if ci[1] is not None and ci[1] > maximum:
                # 点估计未越线但区间上界已越过：预警，不降级
                return SIGNAL_WARNING, "ci_overlaps_threshold"
            return SIGNAL_NORMAL, None

        checks.append(
            ("miss_rate", miss_rate, miss_ci, policy["miss_rate_max"], pos_den,
             policy["min_positive_denominator"])
        )
        checks.append(
            ("override_rate", override_rate, override_ci, policy["override_rate_max"], neg_den,
             policy["min_negative_denominator"])
        )

        results = []
        worst = SIGNAL_NORMAL
        for name, rate, ci, maximum, denominator, min_den in checks:
            status, reason = classify(rate, ci, maximum, denominator, min_den)
            results.append({
                "metric": name,
                "rate": round3(rate) if rate is not None else None,
                "ci_low": round3(ci[0]) if ci[0] is not None else None,
                "ci_high": round3(ci[1]) if ci[1] is not None else None,
                "threshold": maximum,
                "denominator": denominator,
                "status": status,
                "reason": reason,
            })
            if status == SIGNAL_BREACH:
                worst = SIGNAL_BREACH
            elif status == SIGNAL_WARNING and worst != SIGNAL_BREACH:
                worst = SIGNAL_WARNING

        return {
            "hospital_id": hospital_id,
            "release_id": release_id,
            "condition": condition,
            "stratum": stratum,
            "emergency": catalog.is_emergency(condition),
            "window_batches": len(batches),
            "window_counts": totals,
            "status": worst,
            "metrics": results,
        }

    def _trigger_degrade(self, hospital_id, release_id, batch_id, breach_signals):
        self.store.data["drift_seq"] += 1
        event_id = f"drf-{self.store.data['drift_seq']:04d}"
        reason = (
            "滑动窗口内 "
            + "、".join(
                f"{s['condition']}/{s['stratum']} 的 "
                + ",".join(m["metric"] for m in s["metrics"] if m["status"] == SIGNAL_BREACH)
                + " 越限"
                for s in breach_signals
            )
        )
        event = {
            "id": event_id,
            "hospital_id": hospital_id,
            "release_id": release_id,
            "at": self.clock(),
            "trigger_batch_id": batch_id,
            "breaches": [
                {
                    "condition": s["condition"],
                    "stratum": s["stratum"],
                    "metrics": [m for m in s["metrics"] if m["status"] == SIGNAL_BREACH],
                }
                for s in breach_signals
            ],
            "action": "degrade_to_advisory_and_review",
            "reason": reason,
        }
        self.lifecycle.apply_drift_degrade(
            hospital_id, release_id, event_id,
            [self._compact_signal(s) for s in breach_signals], reason,
        )
        self.store.data["drift_events"].append(event)

        review_id = f"rev-{self.store.data['drift_seq']:04d}"
        self.store.data["reviews"][review_id] = {
            "id": review_id,
            "hospital_id": hospital_id,
            "release_id": release_id,
            "opened_at": self.clock(),
            "drift_event_id": event_id,
            "status": "open",
            "close_reason": None,
            "resume_approval_id": None,
        }
        event["review_id"] = review_id
        self.store.save()
        return event

    @staticmethod
    def _compact_signal(signal):
        return {
            "condition": signal["condition"],
            "stratum": signal["stratum"],
            "window_counts": signal["window_counts"],
            "breached_metrics": [
                {"metric": m["metric"], "rate": m["rate"], "threshold": m["threshold"],
                 "denominator": m["denominator"]}
                for m in signal["metrics"] if m["status"] == SIGNAL_BREACH
            ],
        }

    # ── 查询与复审关闭 ─────────────────────────────────────
    def signals(self, hospital_id, release_id):
        signals = []
        prefix = f"{hospital_id}:{release_id}:"
        for key in self.store.data["feedback"]:
            if not key.startswith(prefix):
                continue
            _, _, condition, stratum = key.split(":", 3)
            signals.append(self._window_signal(hospital_id, release_id, condition, stratum))
        return sorted(signals, key=lambda s: (s["status"] != "breach", s["condition"], s["stratum"]))

    def drift_events(self, hospital_id, release_id=None):
        return [
            e for e in self.store.data["drift_events"]
            if e["hospital_id"] == hospital_id
            and (release_id is None or e["release_id"] == release_id)
        ]

    def list_reviews(self, hospital_id, status=None):
        return [
            r for r in self.store.data["reviews"].values()
            if r["hospital_id"] == hospital_id and (status is None or r["status"] == status)
        ]

    def close_reviews_for(self, hospital_id, release_id, resume_approval_id):
        """双人批准的 resume 执行后关闭对应复审。"""
        closed = []
        for review in self.store.data["reviews"].values():
            if (review["hospital_id"] == hospital_id
                    and review["release_id"] == release_id
                    and review["status"] == "open"):
                review["status"] = "closed"
                review["closed_at"] = self.clock()
                review["close_reason"] = "revalidated_and_resumed"
                review["resume_approval_id"] = resume_approval_id
                closed.append(review["id"])
        if closed:
            self.store.save()
        return closed

    def reset_windows(self, hospital_id, release_id, approval_id):
        """复审后恢复上线时重置窗口基准。

        旧批次已经在漂移事件与复审中定案，不应继续计入新窗口；重置前的
        批次归档保留（archive），之后录入的反馈从空窗口重新累积。
        """
        prefix = f"{hospital_id}:{release_id}:"
        archive = self.store.data.setdefault("feedback_archive", [])
        archived = 0
        for key, bucket in list(self.store.data["feedback"].items()):
            if key.startswith(prefix):
                archive.append({
                    "at": self.clock(),
                    "key": key,
                    "approval_id": approval_id,
                    "batches": bucket,
                })
                del self.store.data["feedback"][key]
                archived += len(bucket)
        self.store.data.setdefault("window_resets", []).append({
            "at": self.clock(),
            "hospital_id": hospital_id,
            "release_id": release_id,
            "approval_id": approval_id,
            "archived_batches": archived,
        })
        self.store.save()
        return archived

    # ── 线上调用核对 ───────────────────────────────────────
    def verify_call(self, hospital_id, payload):
        if not isinstance(payload, dict) or set(payload) - _CALL_KEYS:
            raise ValidationError("调用核对字段非法；不得携带患者信息")
        assert_no_patient_fields(payload)
        release_id = payload.get("release_id")
        fingerprint = payload.get("fingerprint")
        protocol = payload.get("protocol")
        condition = payload.get("condition")
        if not all(isinstance(v, str) and v for v in (release_id, fingerprint, protocol)):
            raise ValidationError("release_id/fingerprint/protocol 必须是非空字符串")
        call_count = _as_int(payload.get("call_count", 1), "call_count")
        if call_count <= 0:
            raise ValidationError("call_count 必须为正整数")

        # 不暴露其他机构信息：所有失败结果统一带本院视角的原因码
        serving = self.lifecycle.get_serving(hospital_id)
        mismatch = None
        snapshot = serving.get("snapshot")
        if serving["mode"] == "inactive" or not snapshot:
            mismatch = "no_approved_version_online"
        elif serving["mode"] in ("suspended", "retired"):
            mismatch = f"online_version_{serving['mode']}"
        elif release_id != serving["release_id"]:
            mismatch = "release_mismatch"
        elif fingerprint != snapshot["fingerprint"]:
            mismatch = "fingerprint_mismatch"
        elif condition is not None:
            if condition not in snapshot["conditions"]:
                mismatch = "condition_not_covered"
            elif protocol not in snapshot["protocols_signature"].get(condition, []):
                mismatch = "protocol_not_covered"
        elif protocol not in catalog.PROTOCOLS:
            mismatch = "unknown_protocol"

        matched = mismatch is None
        result = {
            "at": self.clock(),
            "hospital_id": hospital_id,
            "reported_release_id": release_id,
            "reported_fingerprint": fingerprint,
            "serving_release_id": serving.get("release_id"),
            "protocol": protocol,
            "condition": condition,
            "mode": serving["mode"],
            "matched": matched,
            "advisory_only": serving["mode"] == "advisory",
            "mismatch_reason": mismatch,
            "call_count": call_count,
        }
        events = self.store.data["call_events"]
        events.append(result)
        if len(events) > 1000:
            del events[:-1000]
        self.store.save()
        return result

    def call_events(self, hospital_id, mismatch_only=False, limit=100):
        events = [
            e for e in self.store.data["call_events"]
            if e["hospital_id"] == hospital_id and (not mismatch_only or not e["matched"])
        ]
        return events[-limit:]
