"""评估清单与模拟指标夹具。

这些数据是**合成数据**，用于：
1. 离线复算准入（python3 service.py --check）：服务端从 TP/FP/TN/FN 计数
   重新计算敏感性、特异性与 AUC 置信区间，不采信自报点估计；
2. 触发漂移回放：按时间线灌入使用反馈批次，复现“先降级仅提示、再触发复审”；
3. 核对线上调用：构造命中/指纹不符/版本不符/协议越界的调用上报。
"""

# ── 演示发布规格 ────────────────────────────────────────────
DEMO_MODEL = "abd-ct-foundation"

DEMO_RELEASE_V1 = {
    "model_name": DEMO_MODEL,
    "code": {"repo": "registry.example/abd-ct", "commit": "a1b2c3d"},
    "weights": {
        "uri": "s3://model-weights/abd-ct/v1.0.0/weights.bin",
        "sha256": "a" * 64,
    },
    "runtime": {
        "image": "registry.example/abd-ct-runtime:1.0.0",
        "image_sha256": "b" * 64,
        "python": "3.11",
        "cuda": "12.1",
        "requirements_lock_sha256": "c" * 64,
    },
    "threshold": 0.5,
    # 演示用 4 个病种（2 个急诊），完整能力见 domain/catalog.py（123 种）
    "conditions": ["appendicitis", "bowel_obstruction", "hcc", "diverticulosis"],
    "contraindications": [
        "condition:hepatic_steatosis",
        "scenario:pediatric_under_6",
        "scenario:iodinated_contrast_anaphylaxis",
    ],
}

DEMO_RELEASE_V1_THRESHOLD = {
    **DEMO_RELEASE_V1,
    "threshold": 0.42,  # 仅阈值变化 → threshold_change
}

DEMO_RELEASE_V2_WEIGHTS = {
    **DEMO_RELEASE_V1,
    "code": {"repo": "registry.example/abd-ct", "commit": "e5f6a7b"},
    "weights": {
        "uri": "s3://model-weights/abd-ct/v2.0.0/weights.bin",
        "sha256": "d" * 64,
    },
}

# ── 指标生成 ────────────────────────────────────────────────
def _cell(condition, protocol, stratum, n_pos, n_neg, sens, spec, auc, seed=0):
    """由目标敏感性/特异性反推混淆计数（确定性，便于复算演示）。"""
    tp = round(n_pos * sens)
    fn = n_pos - tp
    tn = round(n_neg * spec)
    fp = n_neg - tn
    return {
        "condition": condition,
        "protocol": protocol,
        "stratum": stratum,
        "n_pos": n_pos, "n_neg": n_neg,
        "tp": tp, "fn": fn, "tn": tn, "fp": fp,
        "auc": round(auc - (seed * 0.001), 3),
    }


def passing_validation(release_id):
    """全部格子达标：每个病种取一个其支持的协议。"""
    plan = [
        ("appendicitis", "noncontrast", "routine", 48, 80, 0.94, 0.93, 0.91),
        ("appendicitis", "noncontrast", "emergency", 40, 70, 0.97, 0.92, 0.95),
        ("bowel_obstruction", "portal_venous", "routine", 42, 70, 0.93, 0.91, 0.90),
        ("bowel_obstruction", "portal_venous", "emergency", 40, 60, 0.98, 0.90, 0.94),
        ("hcc", "portal_venous", "routine", 45, 75, 0.91, 0.90, 0.88, 1),
        ("diverticulosis", "noncontrast", "routine", 55, 90, 0.92, 0.95, 0.89, 2),
    ]
    return {
        "release_id": release_id,
        "dataset_id": "ds-hospital-a-2026q2",
        "scope_protocols": ["noncontrast", "portal_venous"],
        "cells": [_cell(*row) for row in plan],
        "note": "去标识化回顾性病例集，仅含汇总计数",
    }


def emergency_subgroup_weak_validation(release_id):
    """常规亚组达标但急诊亚组敏感性不足 → 必须驳回，不能被总平均掩盖。"""
    submission = passing_validation(release_id)
    submission["dataset_id"] = "ds-hospital-b-2026q2"
    for cell in submission["cells"]:
        if cell["condition"] == "appendicitis" and cell["stratum"] == "emergency":
            cell.update({"n_pos": 30, "tp": 24, "fn": 6, "n_neg": 60, "tn": 54, "fp": 6, "auc": 0.78})
    return submission


def insufficient_sample_validation(release_id):
    """点估计尚可但样本量/置信区间不足 → 只能进入观察。"""
    submission = passing_validation(release_id)
    submission["dataset_id"] = "ds-hospital-c-small"
    for cell in submission["cells"]:
        if cell["condition"] == "hcc":
            cell.update({"n_pos": 12, "tp": 11, "fn": 1, "n_neg": 18, "tn": 17, "fp": 1, "auc": 0.88})
    return submission


def protocol_mismatch_validation(release_id):
    """使用病种未声明支持的协议提交 → 直接拒绝（协议外证据不得用于准入）。"""
    submission = passing_validation(release_id)
    submission["dataset_id"] = "ds-hospital-d-wrong-protocol"
    for cell in submission["cells"]:
        if cell["condition"] == "hcc":
            cell["protocol"] = "noncontrast"  # hcc 仅支持增强期
    return submission


def coverage_gap_validation(release_id):
    """缺少急诊病种的急诊亚组格子 → 覆盖缺口，驳回。"""
    submission = passing_validation(release_id)
    submission["dataset_id"] = "ds-hospital-e-gap"
    submission["cells"] = [c for c in submission["cells"] if c["stratum"] != "emergency"]
    return submission


# ── 漂移反馈时间线（批次聚合，无患者级数据）────────────────
def drift_feedback_batches(release_id):
    """5 批反馈：前 4 批平稳（仅零星漏报），第 5 批集中漏报使窗口越限。

    窗口（最近 5 批）累积到第 5 批时：
    - 阳性分母（采纳+纠正）= 40，漏报纠正 = 9，漏报率 22.5% > 急诊阈值 5%；
    - 此前各批漏报率分别为 0、0、1/24、1/32，均未越限。
    """
    plans = [
        # (采纳阳性, 改判阳性, 漏报纠正, 阴性确认)
        (8, 0, 0, 12),
        (8, 0, 0, 12),
        (7, 1, 1, 11),
        (8, 1, 0, 11),
        (0, 2, 8, 10),
    ]
    batches = []
    for index, (accepted, rejected, corrected, confirmed) in enumerate(plans, start=1):
        emergency_row = {
            "condition": "appendicitis", "stratum": "emergency",
            "model_positive_accepted": accepted,
            "model_positive_rejected": rejected,
            "model_negative_corrected": corrected,
            "model_negative_confirmed": confirmed,
        }
        batches.append({
            "release_id": release_id,
            "batch_id": f"batch-2026w{index:02d}",
            "feedback": [
                emergency_row,
                {
                    "condition": "appendicitis", "stratum": "routine",
                    "model_positive_accepted": 10,
                    "model_positive_rejected": 1,
                    "model_negative_corrected": 0,
                    "model_negative_confirmed": 18,
                },
            ],
        })
    return batches


def revalidation_after_drift(release_id):
    """复审期间重新采集的本地验证（全部达标），用于恢复上线。"""
    submission = passing_validation(release_id)
    submission["dataset_id"] = "ds-hospital-a-2026q3-revalidation"
    return submission


# ── 双人审批身份 ────────────────────────────────────────────
PEOPLE = {
    "requester": "zhang.wei",
    "reviewer_1": "li.na",
    "reviewer_2": "wang.fang",
}
