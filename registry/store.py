"""准入台账的核心状态与领域规则。

设计约束：
- 发布版本"内容即身份"：代码、权重、运行环境、阈值共同决定 release_id，
  同名模型换了权重就是另一个版本，无法混淆。
- 患者级资料不进入台账：本地验证只接受按病种与亚组聚合的指标，
  提交中夹带患者级字段会被拒绝。
- 运行状态在 观察/批准/仅提示/暂停/退役 之间流转，每次变化都引用证据。
- 替换权重、调整阈值、回滚必须双人审批，审批与证据一并留痕。

状态全部保存在内存中，可整体导出 / 导入 JSON 快照。
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .checklist import evaluate_admission, evaluate_drift_window

REQUIRED_APPROVALS = 2
DEPLOYMENT_STATUSES = ("observation", "approved", "advisory_only", "suspended", "retired")
MANUAL_STATUSES = ("observation", "suspended", "retired")
FEEDBACK_KINDS = ("adopt", "override", "miss")
CHANGE_KINDS = ("replace_weights", "adjust_threshold", "rollback")

RELEASE_FIELDS = (
    "model_name",
    "model_version",
    "code_ref",
    "weights_hash",
    "runtime_env",
    "thresholds",
    "applicable_organs",
    "scan_protocols",
    "contraindications",
)

# 患者级标识字段：出现在验证提交中即拒绝，保证患者级资料不进入台账。
PATIENT_LEVEL_KEYS = {
    "patient_id", "patient_name", "mrn", "id_card", "birth_date", "case_id",
    "study_id", "accession_number", "dicom", "image", "images", "name",
    "sex", "gender", "age", "phone", "address",
}

ALLOWED_RECORD_KEYS = {
    "disease", "subgroup", "sample_size",
    "sensitivity", "specificity", "auc",
    "sensitivity_ci", "specificity_ci", "auc_ci",
}

METRIC_KEYS = ("sensitivity", "specificity", "auc")


class DomainError(Exception):
    """带 HTTP 状态码的领域错误。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_record(record):
    """校验一条按病种与亚组聚合的验证记录，拒绝患者级字段。"""
    if not isinstance(record, dict):
        raise DomainError(400, "验证记录必须是对象")
    patient_keys = sorted(set(record) & PATIENT_LEVEL_KEYS)
    if patient_keys:
        raise DomainError(
            400, f"包含患者级字段 {patient_keys}，患者级资料不得进入台账"
        )
    unknown = sorted(set(record) - ALLOWED_RECORD_KEYS)
    if unknown:
        raise DomainError(400, f"验证记录包含未定义字段 {unknown}")
    for key in ("disease", "subgroup"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise DomainError(400, f"验证记录缺少有效的 {key}")
    sample_size = record.get("sample_size")
    if not isinstance(sample_size, int) or isinstance(sample_size, bool) or sample_size < 1:
        raise DomainError(400, "sample_size 必须是正整数")
    normalized = {
        "disease": record["disease"].strip(),
        "subgroup": record["subgroup"].strip(),
        "sample_size": sample_size,
    }
    for metric in METRIC_KEYS:
        value = record.get(metric)
        if not _is_number(value) or not 0 <= value <= 1:
            raise DomainError(400, f"{metric} 必须是 [0,1] 内的数值")
        ci = record.get(f"{metric}_ci")
        if (
            not isinstance(ci, list)
            or len(ci) != 2
            or not all(_is_number(bound) for bound in ci)
            or not 0 <= ci[0] <= ci[1] <= 1
        ):
            raise DomainError(400, f"{metric}_ci 必须是 [下限, 上限] 且落在 [0,1]")
        if not ci[0] <= value <= ci[1]:
            raise DomainError(400, f"{metric} 点估计不在置信区间内，请核对数据质量")
        normalized[metric] = float(value)
        normalized[f"{metric}_ci"] = [float(ci[0]), float(ci[1])]
    return normalized


class RegistryStore:
    """台账状态：发布版本、本地验证、部署状态、漂移与审批记录。"""

    def __init__(self):
        self.releases = {}
        self.hospitals = {}
        self.validations = {}
        self.deployments = {}
        self.current = {}
        self.feedback = {}
        self.drift_reports = []
        self.rereviews = []
        self.change_requests = {}
        self.call_audits = []
        self._seqs = {}

    # ---- 基础工具 ----

    def _seq(self, name):
        self._seqs[name] = self._seqs.get(name, 0) + 1
        return self._seqs[name]

    @staticmethod
    def _dep_key(hospital_id, release_id):
        return f"{hospital_id}|{release_id}"

    def _require_hospital(self, hospital_id):
        if hospital_id not in self.hospitals:
            raise DomainError(404, f"医院未登记: {hospital_id}")

    def _require_release(self, release_id):
        if release_id not in self.releases:
            raise DomainError(404, f"发布版本未登记: {release_id}")

    def _deployment_for(self, hospital_id, release_id, create=False):
        key = self._dep_key(hospital_id, release_id)
        deployment = self.deployments.get(key)
        if deployment is None and create:
            deployment = {
                "hospital_id": hospital_id,
                "release_id": release_id,
                "status": None,
                "history": [],
            }
            self.deployments[key] = deployment
        return deployment

    @staticmethod
    def _transition(deployment, to, evidence_ref, reason, at):
        deployment["history"].append({
            "at": at or now_iso(),
            "from": deployment["status"],
            "to": to,
            "evidence_ref": evidence_ref,
            "reason": reason,
        })
        deployment["status"] = to

    # ---- 发布版本登记：内容即身份 ----

    def register_release(self, actor="unknown", at=None, **fields):
        unknown = sorted(set(fields) - set(RELEASE_FIELDS))
        if unknown:
            raise DomainError(400, f"登记包含未定义字段 {unknown}")
        missing = [field for field in RELEASE_FIELDS if field not in fields]
        if missing:
            raise DomainError(400, f"登记字段缺失: {missing}")
        for key in ("model_name", "model_version", "code_ref", "weights_hash", "runtime_env"):
            if not isinstance(fields[key], str) or not fields[key].strip():
                raise DomainError(400, f"{key} 必须是非空字符串")
        if not isinstance(fields["thresholds"], dict) or not fields["thresholds"]:
            raise DomainError(400, "thresholds 必须是非空对象")
        for key in ("applicable_organs", "scan_protocols", "contraindications"):
            if not isinstance(fields[key], list) or not all(
                isinstance(item, str) for item in fields[key]
            ):
                raise DomainError(400, f"{key} 必须是字符串数组")
        identity = {key: fields[key] for key in RELEASE_FIELDS}
        release_id = "rel-" + _digest(identity)
        existing = self.releases.get(release_id)
        if existing:
            return existing, False
        release = {
            "release_id": release_id,
            **identity,
            "registered_by": actor,
            "registered_at": at or now_iso(),
        }
        self.releases[release_id] = release
        return release, True

    def add_hospital(self, hospital_id=None, name="", at=None):
        if hospital_id:
            if hospital_id in self.hospitals:
                raise DomainError(409, f"医院已登记: {hospital_id}")
            hid = hospital_id
        else:
            hid = f"hosp-{self._seq('hospital')}"
        hospital = {"hospital_id": hid, "name": name, "registered_at": at or now_iso()}
        self.hospitals[hid] = hospital
        return hospital

    # ---- 本地验证与准入复算 ----

    def submit_validation(self, checklist, hospital_id, release_id, records, actor="unknown", at=None):
        self._require_hospital(hospital_id)
        self._require_release(release_id)
        if not records:
            raise DomainError(400, "验证记录不能为空")
        validated = [validate_record(record) for record in records]
        outcome = evaluate_admission(validated, checklist)
        at = at or now_iso()
        batch_id = f"val-{self._seq('validation')}"
        batch = {
            "batch_id": batch_id,
            "hospital_id": hospital_id,
            "release_id": release_id,
            "records": validated,
            "decision": outcome["decision"],
            "failures": outcome["failures"],
            "submitted_by": actor,
            "submitted_at": at,
        }
        self.validations[batch_id] = batch
        decision = outcome["decision"]
        if decision != "rejected":
            deployment = self._deployment_for(hospital_id, release_id, create=True)
            # 暂停 / 退役状态不被新的验证提交自动改写，需运营方显式处置。
            if deployment["status"] not in ("suspended", "retired"):
                self._transition(
                    deployment, decision, evidence_ref=batch_id,
                    reason="local_validation", at=at,
                )
            self.current[hospital_id] = release_id
        return batch

    # ---- 反馈与漂移 ----

    def add_feedback(self, checklist, events, at=None):
        if not events:
            raise DomainError(400, "反馈事件不能为空")
        actions = []
        for event in events:
            hospital_id = event.get("hospital_id")
            release_id = event.get("release_id")
            self._require_hospital(hospital_id)
            self._require_release(release_id)
            kind = event.get("kind")
            if kind not in FEEDBACK_KINDS:
                raise DomainError(400, f"未知反馈类型: {kind}")
            record = {
                "hospital_id": hospital_id,
                "release_id": release_id,
                "kind": kind,
                "disease": event.get("disease"),
                "at": event.get("at") or at or now_iso(),
            }
            key = self._dep_key(hospital_id, release_id)
            self.feedback.setdefault(key, []).append(record)
            window = self.feedback[key][-checklist["drift"]["window_size"]:]
            breach = evaluate_drift_window(window, checklist["drift"])
            if breach:
                actions.append(
                    self._handle_drift(hospital_id, release_id, breach, record["at"])
                )
        return actions

    def _handle_drift(self, hospital_id, release_id, breach, at):
        report_id = f"drift-{self._seq('drift')}"
        deployment = self._deployment_for(hospital_id, release_id)
        action = "recorded"
        if deployment and deployment["status"] == "approved":
            # 达到阈值先降级到仅提示，再触发复审。
            self._transition(
                deployment, "advisory_only",
                evidence_ref=report_id, reason="drift_breach", at=at,
            )
            action = "downgraded_to_advisory_only"
        rereview_id = None
        if deployment and deployment["status"] in ("approved", "advisory_only"):
            rereview_id = self._open_rereview(
                hospital_id, release_id, report_id, at
            )["rereview_id"]
        report = {
            "report_id": report_id,
            "hospital_id": hospital_id,
            "release_id": release_id,
            "at": at,
            **breach,
            "action": action,
            "rereview_id": rereview_id,
        }
        self.drift_reports.append(report)
        return report

    def _open_rereview(self, hospital_id, release_id, drift_report_id, at):
        for review in self.rereviews:
            if (
                review["hospital_id"] == hospital_id
                and review["release_id"] == release_id
                and review["status"] == "open"
            ):
                return review
        review = {
            "rereview_id": f"rr-{self._seq('rereview')}",
            "hospital_id": hospital_id,
            "release_id": release_id,
            "opened_at": at,
            "reason": "drift_breach",
            "drift_report_id": drift_report_id,
            "status": "open",
        }
        self.rereviews.append(review)
        return review

    # ---- 双人审批的变更 ----

    def create_change_request(self, hospital_id, kind, payload, evidence_refs, requester, at=None):
        self._require_hospital(hospital_id)
        if kind not in CHANGE_KINDS:
            raise DomainError(400, f"未知变更类型: {kind}")
        if not requester:
            raise DomainError(400, "缺少发起人")
        if not evidence_refs:
            raise DomainError(400, "变更必须保留当时证据（evidence_refs 不能为空）")
        payload = payload or {}
        if kind == "replace_weights" and not payload.get("new_weights_hash"):
            raise DomainError(400, "replace_weights 需要 new_weights_hash")
        if kind == "adjust_threshold" and not isinstance(payload.get("new_thresholds"), dict):
            raise DomainError(400, "adjust_threshold 需要 new_thresholds 对象")
        if kind == "rollback":
            target = payload.get("target_release_id")
            if not target:
                raise DomainError(400, "rollback 需要 target_release_id")
            self._require_release(target)
            if self._dep_key(hospital_id, target) not in self.deployments:
                raise DomainError(409, "回滚目标在本院没有部署记录")
        request_id = f"cr-{self._seq('change')}"
        request = {
            "request_id": request_id,
            "hospital_id": hospital_id,
            "kind": kind,
            "payload": payload,
            "evidence_refs": list(evidence_refs),
            "requester": requester,
            "approvals": [],
            "status": "pending",
            "created_at": at or now_iso(),
        }
        self.change_requests[request_id] = request
        return request

    def approve_change_request(self, request_id, approver, at=None):
        request = self.change_requests.get(request_id)
        if not request:
            raise DomainError(404, f"变更申请不存在: {request_id}")
        if request["status"] != "pending":
            raise DomainError(409, f"变更申请已处理: {request['status']}")
        if not approver:
            raise DomainError(400, "缺少审批人")
        if approver == request["requester"]:
            raise DomainError(403, "发起人不得审批自己的申请")
        if any(item["approver"] == approver for item in request["approvals"]):
            raise DomainError(409, "同一审批人不得重复审批")
        request["approvals"].append({"approver": approver, "at": at or now_iso()})
        if len(request["approvals"]) >= REQUIRED_APPROVALS:
            applied = self._apply_change_request(request, at or now_iso())
            request["applied"] = applied
            request["status"] = "applied"
        return request

    def _apply_change_request(self, request, at):
        hospital_id = request["hospital_id"]
        kind = request["kind"]
        payload = request["payload"]
        if kind == "rollback":
            target = payload["target_release_id"]
            self.current[hospital_id] = target
            applied = {"current_release_id": target}
        else:
            current_id = self.current.get(hospital_id)
            if not current_id:
                raise DomainError(409, "本院当前没有已部署版本，无法在此基础上升版")
            base = self.releases[current_id]
            fields = {key: base[key] for key in RELEASE_FIELDS}
            if kind == "replace_weights":
                fields["weights_hash"] = payload["new_weights_hash"]
                for optional in ("code_ref", "runtime_env", "thresholds"):
                    if payload.get(optional) is not None:
                        fields[optional] = payload[optional]
            else:  # adjust_threshold
                fields["thresholds"] = payload["new_thresholds"]
            release, _ = self.register_release(
                actor=f"change:{request['request_id']}", at=at, **fields
            )
            deployment = self._deployment_for(hospital_id, release["release_id"], create=True)
            if deployment["status"] is None:
                # 换权重 / 调阈值产生新版本，必须重新本地验证，先进入观察。
                self._transition(
                    deployment, "observation",
                    evidence_ref=request["request_id"], reason="change_applied", at=at,
                )
            self.current[hospital_id] = release["release_id"]
            applied = {
                "new_release_id": release["release_id"],
                "current_release_id": release["release_id"],
            }
        applied["closed_rereviews"] = self._close_rereviews(
            hospital_id, request["evidence_refs"], request["request_id"], at
        )
        return applied

    def _close_rereviews(self, hospital_id, evidence_refs, request_id, at):
        closed = []
        for review in self.rereviews:
            if review["hospital_id"] != hospital_id or review["status"] != "open":
                continue
            if (
                review["rereview_id"] in evidence_refs
                or review["drift_report_id"] in evidence_refs
            ):
                review["status"] = "closed"
                review["closed_by"] = request_id
                review["closed_at"] = at
                closed.append(review["rereview_id"])
        return closed

    # ---- 运行状态人工处置（暂停 / 退役 / 回到观察） ----

    def set_deployment_status(self, hospital_id, release_id, status, evidence_ref, actor, at=None):
        if status not in MANUAL_STATUSES:
            raise DomainError(
                400, "手动状态只能是 观察/暂停/退役；批准须经本地验证，仅提示由漂移触发"
            )
        deployment = self._deployment_for(hospital_id, release_id)
        if deployment is None:
            raise DomainError(404, "部署记录不存在")
        if deployment["status"] == "retired":
            raise DomainError(409, "退役为终态，不得变更")
        if not evidence_ref:
            raise DomainError(400, "状态变更必须引用证据")
        self._transition(
            deployment, status, evidence_ref=evidence_ref,
            reason=f"manual:{actor}", at=at or now_iso(),
        )
        return deployment

    # ---- 线上调用核对 ----

    def verify_call(self, hospital_id, release_id, at=None):
        """核对一次线上调用是否命中该院当前获批版本，结果留痕。"""
        at = at or now_iso()
        result, detail = self._check_call(hospital_id, release_id)
        audit = {
            "at": at,
            "hospital_id": hospital_id,
            "release_id": release_id,
            "result": result,
            "detail": detail,
        }
        self.call_audits.append(audit)
        return audit

    def _check_call(self, hospital_id, release_id):
        if hospital_id not in self.hospitals:
            return "miss", {"reason": "unknown_hospital"}
        if release_id not in self.releases:
            return "miss", {"reason": "unknown_release"}
        current = self.current.get(hospital_id)
        if current is None:
            return "miss", {"reason": "no_approved_release"}
        if current != release_id:
            return "miss", {"reason": "wrong_version", "expected_release_id": current}
        status = self.deployments[self._dep_key(hospital_id, release_id)]["status"]
        if status == "approved":
            return "hit", {"mode": "full"}
        if status == "advisory_only":
            return "hit", {"mode": "advisory_only"}
        return "miss", {"reason": "status_not_authorized", "status": status}

    # ---- 快照 ----

    def to_dict(self):
        return {
            "releases": self.releases,
            "hospitals": self.hospitals,
            "validations": self.validations,
            "deployments": self.deployments,
            "current": self.current,
            "feedback": self.feedback,
            "drift_reports": self.drift_reports,
            "rereviews": self.rereviews,
            "change_requests": self.change_requests,
            "call_audits": self.call_audits,
            "seqs": self._seqs,
        }

    @classmethod
    def from_dict(cls, data):
        store = cls()
        for key, value in data.items():
            if key == "seqs":
                store._seqs = dict(value)
            elif hasattr(store, key):
                setattr(store, key, value)
        return store

    def save(self, path):
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
