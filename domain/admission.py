"""本地验证提交与准入判定。

关键约束：
1. 仅接受去标识化的**汇总计数**（TP/FP/TN/FN、阴阳性样本量、聚合 AUC），
   出现患者级字段（patient_id/cases/records 等）直接拒绝——患者级数据不得离开机构。
2. 敏感性/特异性由计数**复算**，不采信提交方自报点估计；AUC 由提交方基于
   去标识化评分聚合给出，置信区间由本服务按 Hanley-McNeil 复算。
3. 判定逐格（病种 × 协议 × 亚组）进行：急诊病种必须同时有 routine 与
   emergency 两格。任何一格 fail / 缺格 → 驳回；仅样本量或置信区间不足 →
   观察；全部通过 → 建议批准。**不产生任何总平均指标。**
"""

import hashlib
import json

from . import catalog
from .errors import NotFoundError, ValidationError
from .privacy import assert_no_patient_fields
from .stats import auc_interval, round3, wilson_interval

ROUTINE = "routine"
EMERGENCY = "emergency"
STRATA = (ROUTINE, EMERGENCY)

APPROVE = "approve"
OBSERVE = "observe"
REJECT = "reject"

CELL_PASS = "pass"
CELL_INSUFFICIENT = "insufficient"
CELL_FAIL = "fail"

# 默认准入门槛：急诊亚组的敏感性/AUC 要求更高
DEFAULT_POLICY = {
    ROUTINE: {
        "sens_point": 0.85, "sens_lcb": 0.78,
        "spec_point": 0.85, "spec_lcb": 0.78,
        "auc_point": 0.80, "auc_lcb": 0.75,
        "n_pos": 30, "n_neg": 50,
    },
    EMERGENCY: {
        "sens_point": 0.90, "sens_lcb": 0.85,
        "spec_point": 0.85, "spec_lcb": 0.78,
        "auc_point": 0.85, "auc_lcb": 0.80,
        "n_pos": 20, "n_neg": 40,
    },
}

# 单元格允许出现的全部字段；不在此列的键按疑似患者级数据拒绝
_ALLOWED_CELL_KEYS = frozenset(
    {"condition", "protocol", "stratum", "n_pos", "n_neg", "tp", "fp", "tn", "fn", "auc", "note"}
)
_ALLOWED_TOP_KEYS = frozenset(
    {"release_id", "dataset_id", "scope_protocols", "cells", "note"}
)


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evidence_hash(report):
    """证据快照哈希，审批与复审引用此值，防止事后篡改指标。"""
    return hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest()


def _as_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是非负整数")
    if value < 0:
        raise ValidationError(f"{field} 不能为负数")
    return value


def evaluate_cell(cell, policy):
    """复算单个格子的指标并逐指标判定，返回可审计的格子报告。"""
    n_pos = _as_int(cell["n_pos"], "n_pos")
    n_neg = _as_int(cell["n_neg"], "n_neg")
    tp = _as_int(cell["tp"], "tp")
    fp = _as_int(cell["fp"], "fp")
    tn = _as_int(cell["tn"], "tn")
    fn = _as_int(cell["fn"], "fn")

    if tp + fn != n_pos:
        raise ValidationError(f"{cell['condition']}/{cell['stratum']}: tp+fn 必须等于 n_pos")
    if tn + fp != n_neg:
        raise ValidationError(f"{cell['condition']}/{cell['stratum']}: tn+fp 必须等于 n_neg")

    sens_lo, sens_hi = wilson_interval(tp, n_pos) if n_pos else (0.0, 0.0)
    spec_lo, spec_hi = wilson_interval(tn, n_neg) if n_neg else (0.0, 0.0)
    sens = round3(tp / n_pos) if n_pos else 0.0
    spec = round3(tn / n_neg) if n_neg else 0.0

    auc_value = cell.get("auc")
    auc_ci = None
    if auc_value is not None:
        try:
            auc_value = float(auc_value)
        except (TypeError, ValueError):
            raise ValidationError("auc 必须是数值")
        if not 0.0 <= auc_value <= 1.0:
            raise ValidationError("auc 必须位于 [0, 1]")
        auc_ci = auc_interval(auc_value, n_pos, n_neg)
        auc_value = round3(auc_value)

    checks = []
    status = CELL_PASS

    def apply_check(metric, point, lcb, point_min, lcb_floor, sample_ok):
        nonlocal status
        if not sample_ok:
            result = CELL_INSUFFICIENT
            reason = "sample_size"
        elif point < point_min:
            result = CELL_FAIL
            reason = "point_below_minimum"
        elif lcb is None or lcb < lcb_floor:
            result = CELL_INSUFFICIENT
            reason = "ci_too_wide"
        else:
            result = CELL_PASS
            reason = None
        checks.append(
            {
                "metric": metric,
                "point": round3(point),
                "ci_low": round3(lcb) if lcb is not None else None,
                "minimum": point_min,
                "lcb_floor": lcb_floor,
                "status": result,
                "reason": reason,
            }
        )
        if result == CELL_FAIL:
            status = CELL_FAIL
        elif result == CELL_INSUFFICIENT and status != CELL_FAIL:
            status = CELL_INSUFFICIENT

    sample_ok = n_pos >= policy["n_pos"] and n_neg >= policy["n_neg"]
    apply_check("sensitivity", sens, sens_lo, policy["sens_point"], policy["sens_lcb"], sample_ok)
    apply_check("specificity", spec, spec_lo, policy["spec_point"], policy["spec_lcb"], sample_ok)

    if auc_value is None:
        status = CELL_FAIL if status != CELL_FAIL else status
        checks.append(
            {
                "metric": "auc",
                "point": None, "ci_low": None,
                "minimum": policy["auc_point"], "lcb_floor": policy["auc_lcb"],
                "status": CELL_FAIL, "reason": "auc_missing",
            }
        )
    else:
        auc_lo = auc_ci[0] if auc_ci else None
        apply_check(
            "auc", auc_value, auc_lo,
            policy["auc_point"], policy["auc_lcb"], sample_ok,
        )

    return {
        "condition": cell["condition"],
        "protocol": cell["protocol"],
        "stratum": cell["stratum"],
        "n_pos": n_pos,
        "n_neg": n_neg,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "sensitivity": {"point": sens, "ci_low": round3(sens_lo), "ci_high": round3(sens_hi)},
        "specificity": {"point": spec, "ci_low": round3(spec_lo), "ci_high": round3(spec_hi)},
        "auc": (
            {"point": auc_value, "ci_low": round3(auc_ci[0]), "ci_high": round3(auc_ci[1])}
            if auc_ci else {"point": auc_value, "ci_low": None, "ci_high": None}
        ),
        "status": status,
        "checks": checks,
    }


def _normalize_submission(release, payload):
    if not isinstance(payload, dict):
        raise ValidationError("验证提交必须是对象")
    extra = set(payload) - _ALLOWED_TOP_KEYS
    if extra:
        raise ValidationError(f"包含不允许的顶层字段（疑似患者级数据）: {sorted(extra)}")
    assert_no_patient_fields(payload, list_allowlist=frozenset({"cells.", "scope_protocols."}))

    dataset_id = payload.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise ValidationError("dataset_id 不能为空（去标识化病例集标识）")

    raw_cells = payload.get("cells")
    if not isinstance(raw_cells, list) or not raw_cells:
        raise ValidationError("cells 必须是非空列表")

    scope_in = payload.get("scope_protocols")
    if scope_in is None:
        scope_protocols = None
    elif not isinstance(scope_in, list) or not all(isinstance(p, str) for p in scope_in):
        raise ValidationError("scope_protocols 必须是协议字符串列表")
    else:
        unknown = [p for p in scope_in if p not in catalog.PROTOCOLS]
        if unknown:
            raise ValidationError(f"scope_protocols 含未知协议: {unknown}")
        scope_protocols = scope_in

    conditions = set(release["conditions"])
    seen = set()
    cells = []
    for cell in raw_cells:
        if not isinstance(cell, dict):
            raise ValidationError("每个 cell 必须是对象")
        extra_keys = set(cell) - _ALLOWED_CELL_KEYS
        if extra_keys:
            raise ValidationError(f"cell 包含不允许的字段（疑似患者级数据）: {sorted(extra_keys)}")
        condition = cell.get("condition")
        protocol = cell.get("protocol")
        stratum = cell.get("stratum")
        if condition not in conditions:
            raise ValidationError(f"病种 {condition} 不在发布版本 {release['id']} 的适用范围内")
        if not catalog.supports_protocol(condition, protocol):
            raise ValidationError(
                f"病种 {condition} 未声明支持协议 {protocol}，不得用该协议下的结果准入"
            )
        if scope_protocols is not None and protocol not in scope_protocols:
            raise ValidationError(
                f"格子协议 {protocol} 不在声明的 scope_protocols 内，不得夹带未声明协议"
            )
        if stratum not in STRATA:
            raise ValidationError(f"stratum 必须是 {STRATA} 之一")
        key = (condition, protocol, stratum)
        if key in seen:
            raise ValidationError(f"格子重复: {key}")
        seen.add(key)
        cells.append(cell)

    # 网格由实际提交的（病种, 协议）对确定，逐协议准入：
    # 每个对必须有 routine；急诊病种的每个对还必须有 emergency。
    pairs = sorted({(c["condition"], c["protocol"]) for c in cells})
    required = set()
    for condition, protocol in pairs:
        required.add((condition, protocol, ROUTINE))
        if catalog.is_emergency(condition):
            required.add((condition, protocol, EMERGENCY))

    missing = sorted(required - seen)
    if seen - required:
        raise ValidationError(f"存在非急诊病种的异常亚组格子: {sorted(seen - required)}")

    return dataset_id.strip(), cells, required, missing


def decide_validation(release, payload, policy=None):
    """复算整份验证提交，返回 (decision, report)。不写存储。"""
    policy = policy or DEFAULT_POLICY
    dataset_id, cells, required, missing = _normalize_submission(release, payload)

    cell_reports = [evaluate_cell(cell, policy[cell["stratum"]]) for cell in cells]

    gaps = [
        {
            "condition": c, "protocol": p, "stratum": s,
            "status": CELL_FAIL, "reason": "coverage_gap",
        }
        for c, p, s in missing
    ]

    if any(r["status"] == CELL_FAIL for r in cell_reports) or gaps:
        decision = REJECT
    elif any(r["status"] == CELL_INSUFFICIENT for r in cell_reports):
        decision = OBSERVE
    else:
        decision = APPROVE

    conditions_in_scope = sorted({c for c, _, _ in required})
    report = {
        "release_id": release["id"],
        "release_fingerprint": release["fingerprint"],
        "dataset_id": dataset_id,
        "conditions_in_scope": conditions_in_scope,
        "n_cells": len(required),
        "cells": cell_reports,
        "missing_cells": gaps,
        "decision": decision,
        "note": payload.get("note"),
    }
    report["evidence_sha256"] = evidence_hash(
        {k: v for k, v in report.items() if k != "evidence_sha256"}
    )
    return decision, report


class AdmissionService:
    """验证提交的持久化与查询。"""

    def __init__(self, store, clock, registry):
        self.store = store
        self.clock = clock
        self.registry = registry

    def submit(self, hospital_id, payload):
        release = self.registry.get_release(payload.get("release_id"))
        decision, report = decide_validation(release, payload)

        self.store.data["validation_seq"] += 1
        validation_id = f"val-{self.store.data['validation_seq']:04d}"
        record = {
            "id": validation_id,
            "hospital_id": hospital_id,
            "submitted_at": self.clock(),
            "raw_submission": {
                "release_id": report["release_id"],
                "dataset_id": report["dataset_id"],
                "conditions_in_scope": report["conditions_in_scope"],
                "cells": [
                    {
                        key: cell[key]
                        for key in (
                            "condition", "protocol", "stratum", "n_pos", "n_neg",
                            "tp", "fp", "tn", "fn", "auc",
                        )
                        if key in cell
                    }
                    for cell in payload["cells"]
                ],
            },
            "report": report,
            "decision": decision,
        }
        self.store.data["validations"][validation_id] = record
        self._touch_deployment(hospital_id, release, record)
        self.store.save()
        return record

    def _touch_deployment(self, hospital_id, release, validation):
        key = f"{hospital_id}:{release['id']}"
        deployments = self.store.data["deployments"]
        if key not in deployments:
            deployments[key] = {
                "hospital_id": hospital_id,
                "release_id": release["id"],
                "state": "observation",
                "admitted_conditions": [],
                "history": [],
                "created_at": self.clock(),
            }
        deployment = deployments[key]
        deployment["latest_validation_id"] = validation["id"]
        if validation["decision"] == APPROVE:
            deployment["admitted_conditions"] = validation["report"]["conditions_in_scope"]
            deployment["state"] = "approved"
        elif deployment.get("state") != "approved":
            # 已批准档案不因后续一份观察/驳回验证自动改状态：
            # 最新判定仍完整留痕，是否暂停/退役走双人变更与漂移流程。
            deployment["state"] = (
                "observation" if validation["decision"] == OBSERVE else "rejected"
            )
        deployment["history"].append(
            {
                "at": self.clock(),
                "event": "validation_submitted",
                "validation_id": validation["id"],
                "decision": validation["decision"],
                "evidence_sha256": validation["report"]["evidence_sha256"],
            }
        )

    def get_validation(self, validation_id, hospital_id=None):
        record = self.store.data["validations"].get(validation_id)
        if record is None:
            raise NotFoundError(f"验证记录不存在: {validation_id}")
        if hospital_id is not None and record["hospital_id"] != hospital_id:
            from .errors import ForbiddenError
            raise ForbiddenError("不得查询其他机构的验证数据")
        return record

    def list_validations(self, hospital_id, release_id=None):
        records = [
            v for v in self.store.data["validations"].values()
            if v["hospital_id"] == hospital_id
            and (release_id is None or v["report"]["release_id"] == release_id)
        ]
        return sorted(records, key=lambda r: r["submitted_at"])

    def latest_approved(self, hospital_id, release_id):
        approved = [
            v for v in self.list_validations(hospital_id, release_id)
            if v["decision"] == APPROVE
        ]
        return approved[-1] if approved else None
