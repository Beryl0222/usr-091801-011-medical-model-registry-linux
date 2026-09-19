"""部署生命周期与双人审批。

两层状态：
- 档案状态（deployments[hospital:release]）：observation / approved / rejected /
  retired，由验证提交与退役操作驱动；
- 线上状态（serving[hospital]）：inactive / active / advisory / suspended /
  retired，指向当前线上 release 并固化其运行形态快照。

任何上线、替换权重、阈值变更、回滚、暂停、恢复、退役都必须走双人审批：
申请人与两名审批人须为三个不同的自然人；第二名审批落定的瞬间原子执行，
并把当时的证据（验证证据哈希、发布指纹、漂移事件）冻结进审批记录。
漂移触发的自动降级（active→advisory）是唯一的例外路径，由系统执行并留全证。
"""

from .errors import ConflictError, ForbiddenError, NotFoundError, StateError, ValidationError
from .admission import APPROVE

# 线上模式
INACTIVE = "inactive"
ACTIVE = "active"        # 批准上线：结果可直接进入临床流程
ADVISORY = "advisory"    # 仅提示：结果不得作为独立诊断依据
SUSPENDED = "suspended"  # 暂停调用
RETIRED = "retired"      # 退役（终态）

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXECUTED = "executed"

CHANGE_KINDS = (
    "activate", "weights_swap", "threshold_change", "rollback",
    "degrade", "suspend", "resume", "retire",
)
# 每种变更执行后的线上模式
_KIND_TARGET_MODE = {
    "activate": ACTIVE,
    "weights_swap": ACTIVE,
    "threshold_change": ACTIVE,
    "rollback": ACTIVE,
    "degrade": ADVISORY,
    "suspend": SUSPENDED,
    "resume": ACTIVE,
    "retire": RETIRED,
}
# 不需要新的批准验证即可执行的运维类变更
_OPERATIONAL_KINDS = {"degrade", "suspend", "retire"}


class LifecycleService:
    def __init__(self, store, clock, registry, admission):
        self.store = store
        self.clock = clock
        self.registry = registry
        self.admission = admission

    # ── 读取 ───────────────────────────────────────────────
    def _deployment(self, hospital_id, release_id):
        return self.store.data["deployments"].get(f"{hospital_id}:{release_id}")

    def require_deployment(self, hospital_id, release_id):
        deployment = self._deployment(hospital_id, release_id)
        if deployment is None:
            raise NotFoundError(f"该院尚未提交过 {release_id} 的本地验证")
        return deployment

    def get_serving(self, hospital_id):
        return self.store.data.get("serving", {}).get(hospital_id) or {
            "hospital_id": hospital_id,
            "mode": INACTIVE,
            "release_id": None,
        }

    def list_changes(self, hospital_id):
        return sorted(
            (a for a in self.store.data["approvals"].values()
             if a["hospital_id"] == hospital_id),
            key=lambda a: a["requested_at"],
        )

    def get_change(self, approval_id, hospital_id=None):
        record = self.store.data["approvals"].get(approval_id)
        if record is None:
            raise NotFoundError(f"审批单不存在: {approval_id}")
        if hospital_id is not None and record["hospital_id"] != hospital_id:
            raise ForbiddenError("不得查询其他机构的审批记录")
        return record

    def get_deployment(self, hospital_id, release_id):
        deployment = self.require_deployment(hospital_id, release_id)
        serving = self.get_serving(hospital_id)
        result = dict(deployment)
        result["is_online"] = serving.get("release_id") == release_id and serving["mode"] != INACTIVE
        result["online_mode"] = serving["mode"] if result["is_online"] else None
        return result

    # ── 审批单的创建与双人批准 ─────────────────────────────
    def create_change(self, hospital_id, kind, release_id, requested_by, reason,
                      target_release_id=None, comment=None):
        if kind not in CHANGE_KINDS:
            raise ValidationError(f"不支持的变更类型: {kind}")
        if not isinstance(requested_by, str) or not requested_by.strip():
            raise ValidationError("requested_by 不能为空")
        if not reason or not str(reason).strip():
            raise ValidationError("变更必须说明理由")

        release = self.registry.get_release(release_id)
        deployment = self.require_deployment(hospital_id, release_id)
        serving = self.get_serving(hospital_id)

        target = None
        if kind in ("weights_swap", "threshold_change", "rollback"):
            if not target_release_id:
                raise ValidationError(f"{kind} 必须指定 target_release_id")
            target = self.registry.get_release(target_release_id)
            if self._deployment(hospital_id, target_release_id) is None:
                raise StateError(
                    f"目标版本 {target_release_id} 尚未在本院完成本地验证，"
                    "不得替换权重/阈值或回滚"
                )

        self._guard_request(kind, deployment, serving, release, target, hospital_id)

        evidence = self._collect_evidence(kind, hospital_id, release, target, serving)

        self.store.data["approval_seq"] += 1
        approval_id = f"apr-{self.store.data['approval_seq']:04d}"
        record = {
            "id": approval_id,
            "hospital_id": hospital_id,
            "kind": kind,
            "release_id": release_id,
            "target_release_id": target_release_id,
            "reason": reason,
            "comment": comment,
            "requested_by": requested_by,
            "requested_at": self.clock(),
            "first_approval": None,
            "second_approval": None,
            "status": PENDING,
            "evidence_refs": evidence,
            "execution": None,
        }
        self.store.data["approvals"][approval_id] = record
        self.store.save()
        return record

    def _guard_request(self, kind, deployment, serving, release, target, hospital_id):
        """创建时的状态守卫；执行时会再次复核，防止等待审批期间状态漂移。"""
        mode = serving["mode"]
        online_release = serving.get("release_id")

        if deployment["state"] == "rejected" and kind not in ("retire",):
            raise StateError(f"档案 {release['id']} 已驳回，不能发起 {kind}")

        if kind == "activate":
            if mode != INACTIVE:
                raise StateError("已有上线版本，替换权重请走 weights_swap")
            if online_release is not None:
                raise StateError("线上存在其他版本，请使用 weights_swap")
        elif kind in ("weights_swap", "threshold_change"):
            if mode != ACTIVE and mode != ADVISORY and mode != SUSPENDED:
                raise StateError(f"当前模式 {mode} 不允许 {kind}")
            if online_release == target["id"]:
                raise StateError("目标版本与线上版本相同")
            if kind == "threshold_change":
                self._assert_threshold_only(release, target)
        elif kind == "rollback":
            if mode not in (ACTIVE, ADVISORY, SUSPENDED):
                raise StateError("仅线上版本可回滚")
            target_dep = self.require_deployment(hospital_id, target["id"])
            if target_dep["state"] == "retired":
                raise StateError(f"目标版本 {target['id']} 已退役，不得回滚")
            if not any(
                h.get("event") == "change_executed" and h.get("release_id") == target["id"]
                and h.get("mode") == ACTIVE
                for h in target_dep["history"]
            ):
                raise StateError(f"版本 {target['id']} 从未在本院正式上线，无证据可回滚")
        elif kind == "degrade":
            if mode != ACTIVE or online_release != release["id"]:
                raise StateError("仅 active 版本可降级到仅提示")
        elif kind == "suspend":
            if mode not in (ACTIVE, ADVISORY) or online_release != release["id"]:
                raise StateError("仅线上 active/advisory 版本可暂停")
        elif kind == "resume":
            if mode not in (SUSPENDED, ADVISORY) or online_release != release["id"]:
                raise StateError("仅当前 suspended/advisory 版本可申请恢复")
        elif kind == "retire":
            if mode == RETIRED:
                raise StateError("版本已退役")
            if mode != INACTIVE and online_release != release["id"]:
                # 允许退役非线上档案；执行时不会触碰当前线上版本
                pass

    @staticmethod
    def _assert_threshold_only(source, target):
        for field in ("code", "weights", "runtime", "conditions", "contraindications"):
            if source[field] != target[field]:
                raise StateError(
                    "threshold_change 只能改变阈值；代码/权重/环境/适用范围变化须按权重替换重新准入"
                )
        if source["threshold"] == target["threshold"]:
            raise StateError("新旧阈值相同")

    def _collect_evidence(self, kind, hospital_id, release, target, serving):
        """把执行所依据的证据在**申请当时**固定下来。"""
        refs = []

        def add_validation(rel, label):
            approved = self.admission.latest_approved(hospital_id, rel["id"])
            if approved is None:
                raise StateError(f"{rel['id']} 缺少本院 decision=approve 的本地验证证据")
            refs.append({
                "label": label,
                "release_id": rel["id"],
                "release_fingerprint": rel["fingerprint"],
                "validation_id": approved["id"],
                "evidence_sha256": approved["report"]["evidence_sha256"],
                "decided_at": approved["submitted_at"],
            })

        if kind == "activate":
            add_validation(release, "approval_validation")
        elif kind in ("weights_swap", "threshold_change"):
            add_validation(target, "target_validation")
        elif kind == "rollback":
            target_dep = self.require_deployment(hospital_id, target["id"])
            frozen = next(
                (h for h in reversed(target_dep["history"])
                 if h.get("event") == "change_executed" and h.get("mode") == ACTIVE),
                None,
            )
            refs.append({
                "label": "rollback_evidence",
                "release_id": target["id"],
                "release_fingerprint": target["fingerprint"],
                "approval_id": frozen.get("approval_id"),
                "evidence_sha256": frozen.get("evidence_sha256"),
                "activated_at": frozen.get("at"),
            })
        elif kind == "resume":
            # 漂移降级后恢复必须有晚于降级时间的新批准验证；人工暂停恢复可引用既有验证
            degraded_at = serving.get("degraded_at")
            approved = self.admission.latest_approved(hospital_id, release["id"])
            if approved is None:
                raise StateError("恢复上线需要 approve 的本地验证证据")
            if degraded_at and approved["submitted_at"] <= degraded_at:
                raise StateError("漂移降级后的恢复必须提交重新验证并获得 approve")
            refs.append({
                "label": "resume_validation",
                "release_id": release["id"],
                "release_fingerprint": release["fingerprint"],
                "validation_id": approved["id"],
                "evidence_sha256": approved["report"]["evidence_sha256"],
                "decided_at": approved["submitted_at"],
            })
        return refs

    def approve(self, approval_id, approver, comment=None):
        record = self.get_change(approval_id)
        if record["status"] != PENDING:
            raise ConflictError(f"审批单状态为 {record['status']}，不能再批准")
        approver = approver.strip()
        if approver == record["requested_by"]:
            raise ForbiddenError("申请人不能作为审批人")

        if record["first_approval"] is None:
            record["first_approval"] = {"by": approver, "at": self.clock(), "comment": comment}
            self.store.save()
            return record
        if approver == record["first_approval"]["by"]:
            raise ForbiddenError("两名审批人必须是不同的人")
        record["second_approval"] = {"by": approver, "at": self.clock(), "comment": comment}
        record["status"] = APPROVED
        self._execute(record)
        self.store.save()
        return record

    def reject(self, approval_id, approver, comment):
        record = self.get_change(approval_id)
        if record["status"] != PENDING:
            raise ConflictError(f"审批单状态为 {record['status']}，不能驳回")
        if approver.strip() == record["requested_by"]:
            raise ForbiddenError("申请人不能自行驳回")
        if record["first_approval"] and approver.strip() == record["first_approval"]["by"]:
            raise ForbiddenError("已参与批准的审批人不能驳回")
        record["status"] = REJECTED
        record["rejected_by"] = approver.strip()
        record["rejected_at"] = self.clock()
        record["reject_comment"] = comment
        self.store.save()
        return record

    # ── 执行 ───────────────────────────────────────────────
    def _execute(self, record):
        hospital_id = record["hospital_id"]
        kind = record["kind"]
        source = self.registry.get_release(record["release_id"])
        target = (
            self.registry.get_release(record["target_release_id"])
            if record["target_release_id"] else None
        )
        effective = target or source
        serving = self.get_serving(hospital_id)

        # 执行时复核：等待期间状态可能已被其他审批或漂移改变
        deployment = self.require_deployment(hospital_id, source["id"])
        self._guard_request(kind, deployment, serving, source, target, hospital_id)

        new_mode = _KIND_TARGET_MODE[kind]
        now = self.clock()
        evidence_hash = self._freeze_evidence(record, effective, new_mode, now)

        # 退役一个非线上档案：只更新该档案状态，保持当前线上形态不变
        retiring_offline = (
            kind == "retire"
            and serving["mode"] != INACTIVE
            and serving.get("release_id") != effective["id"]
        )
        if retiring_offline:
            dep = self.require_deployment(hospital_id, effective["id"])
            dep["state"] = "retired"
            dep["history"].append({
                "at": now,
                "event": "change_executed",
                "kind": kind,
                "approval_id": record["id"],
                "release_id": effective["id"],
                "mode": RETIRED,
                "evidence_sha256": evidence_hash,
            })
            record["status"] = EXECUTED
            record["executed_at"] = now
            record["execution"] = {
                "mode": RETIRED,
                "release_id": effective["id"],
                "release_fingerprint": effective["fingerprint"],
                "evidence_sha256": evidence_hash,
                "serving_unchanged": serving["release_id"],
            }
            self.store.save()
            return

        serving.setdefault("hospital_id", hospital_id)
        serving["mode"] = new_mode
        serving["release_id"] = effective["id"]
        serving["snapshot"] = {
            "release_id": effective["id"],
            "fingerprint": effective["fingerprint"],
            "threshold": effective["threshold"],
            "weights_sha256": effective["weights"]["sha256"],
            "code_commit": effective["code"]["commit"],
            "conditions": effective["conditions"],
            "protocols_signature": self._protocols_signature(effective),
        }
        serving["mode_since"] = now
        serving["effective_approval_id"] = record["id"]
        for flag in ("degraded_at", "drift_event_id", "review_status"):
            serving.pop(flag, None)

        self.store.data.setdefault("serving", {})[hospital_id] = serving

        dep = self.require_deployment(hospital_id, effective["id"])
        if new_mode == ACTIVE:
            dep["state"] = "approved"
        elif new_mode == RETIRED:
            dep["state"] = "retired"
        dep["history"].append({
            "at": now,
            "event": "change_executed",
            "kind": kind,
            "approval_id": record["id"],
            "release_id": effective["id"],
            "mode": new_mode,
            "evidence_sha256": evidence_hash,
        })

        record["status"] = EXECUTED
        record["executed_at"] = now
        record["execution"] = {
            "mode": new_mode,
            "release_id": effective["id"],
            "release_fingerprint": effective["fingerprint"],
            "evidence_sha256": evidence_hash,
        }

    @staticmethod
    def _protocols_signature(release):
        from . import catalog
        return {
            c: sorted(catalog.get(c)["protocols"])
            for c in release["conditions"]
        }

    @staticmethod
    def _freeze_evidence(record, effective_release, mode, at):
        """执行证据：审批链 + 申请时固定的证据引用 + 实际生效指纹。"""
        import hashlib
        import json
        frozen = {
            "approval_id": record["id"],
            "kind": record["kind"],
            "requested_by": record["requested_by"],
            "first_approval": record["first_approval"],
            "second_approval": record["second_approval"],
            "evidence_refs": record["evidence_refs"],
            "effective_release_id": effective_release["id"],
            "effective_fingerprint": effective_release["fingerprint"],
            "mode": mode,
            "at": at,
        }
        digest = hashlib.sha256(
            json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        record["frozen_evidence"] = frozen
        return digest

    # ── 漂移自动降级（系统路径，非双人）────────────────────
    def apply_drift_degrade(self, hospital_id, release_id, drift_event_id, signals, reason):
        serving = self.get_serving(hospital_id)
        if serving["mode"] != ACTIVE or serving.get("release_id") != release_id:
            raise StateError("仅 active 版本可被漂移监控自动降级")
        now = self.clock()
        record = {
            "at": now,
            "event": "drift_degrade",
            "release_id": release_id,
            "drift_event_id": drift_event_id,
            "signals": signals,
            "reason": reason,
            "operator": "system:drift-monitor",
        }
        serving["mode"] = ADVISORY
        serving["mode_since"] = now
        serving["degraded_at"] = now
        serving["drift_event_id"] = drift_event_id
        serving["review_status"] = "review_triggered"
        self.store.data.setdefault("serving", {})[hospital_id] = serving
        dep = self.require_deployment(hospital_id, release_id)
        dep["history"].append(record)
        self.store.save()
        return record
