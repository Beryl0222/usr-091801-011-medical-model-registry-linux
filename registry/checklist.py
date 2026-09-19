"""准入评估清单的加载、校验与按亚组复算。

清单是仓库内受版本控制的文件：任何阈值调整都留下记录。
复算规则：按病种与急诊亚组逐项判定，任何亚组不足都不能被总体平均掩盖；
指标不足只能进入观察或驳回，不能因为其他亚组优秀而放行。
"""

import json
from pathlib import Path

DEFAULT_CHECKLIST_PATH = (
    Path(__file__).resolve().parent.parent / "checklists" / "admission_checklist.json"
)

METRICS = ("sensitivity", "specificity", "auc")


def load_checklist(path=None):
    """读取并校验评估清单，默认使用仓库内置清单。"""
    source = Path(path) if path else DEFAULT_CHECKLIST_PATH
    checklist = json.loads(source.read_text(encoding="utf-8"))
    validate_checklist(checklist)
    return checklist


def validate_checklist(checklist):
    """清单结构不完整时拒绝使用，避免按残缺规则放行。"""
    for key in ("default_rule", "required_subgroups", "drift"):
        if key not in checklist:
            raise ValueError(f"评估清单缺少 {key}")
    rule = checklist["default_rule"]
    for key in ("min_sample_size", "borderline_margin"):
        if key not in rule:
            raise ValueError(f"默认规则缺少 {key}")
    for metric in METRICS:
        if f"min_{metric}" not in rule:
            raise ValueError(f"默认规则缺少 min_{metric}")
    drift = checklist["drift"]
    for key in ("window_size", "min_window_events", "max_override_rate", "max_miss_rate"):
        if key not in drift:
            raise ValueError(f"漂移规则缺少 {key}")


def rule_for(checklist, disease):
    """病种专属规则覆盖默认规则。"""
    rule = dict(checklist["default_rule"])
    rule.update(checklist.get("disease_rules", {}).get(disease, {}))
    return rule


def evaluate_admission(records, checklist):
    """按病种与亚组逐项复算本地验证指标。

    返回 {"decision", "failures"}：
    - 任一指标明显低于阈值（hard）→ rejected（驳回）；
    - 否则只要存在不足（样本量、临界指标、置信区间下限、缺亚组）→ observation（观察）；
    - 全部达标 → approved（可批准）。
    """
    failures = []
    seen = {(record["disease"], record["subgroup"]) for record in records}
    diseases = {record["disease"] for record in records}
    for disease in sorted(diseases):
        for subgroup in checklist["required_subgroups"]:
            if (disease, subgroup) not in seen:
                failures.append({
                    "disease": disease,
                    "subgroup": subgroup,
                    "kind": "missing_subgroup",
                    "severity": "borderline",
                    "detail": "缺少必需亚组的本地验证记录",
                })
    for record in records:
        rule = rule_for(checklist, record["disease"])
        tag = {"disease": record["disease"], "subgroup": record["subgroup"]}
        if record["sample_size"] < rule["min_sample_size"]:
            failures.append({
                **tag,
                "kind": "sample_size",
                "severity": "borderline",
                "expected": rule["min_sample_size"],
                "actual": record["sample_size"],
                "detail": "样本量不足",
            })
        for metric in METRICS:
            threshold = rule[f"min_{metric}"]
            value = record[metric]
            if value < threshold:
                severity = (
                    "borderline"
                    if value >= threshold - rule["borderline_margin"]
                    else "hard"
                )
                failures.append({
                    **tag,
                    "kind": metric,
                    "severity": severity,
                    "expected": threshold,
                    "actual": value,
                    "detail": f"{metric} 低于清单阈值",
                })
            min_ci_lower = rule.get(f"min_{metric}_ci_lower")
            if min_ci_lower is not None:
                ci_lower = record[f"{metric}_ci"][0]
                if ci_lower < min_ci_lower:
                    failures.append({
                        **tag,
                        "kind": f"{metric}_ci_lower",
                        "severity": "borderline",
                        "expected": min_ci_lower,
                        "actual": ci_lower,
                        "detail": "置信区间下限不足，需补充样本",
                    })
    if any(failure["severity"] == "hard" for failure in failures):
        decision = "rejected"
    elif failures:
        decision = "observation"
    else:
        decision = "approved"
    return {"decision": decision, "failures": failures}


def evaluate_drift_window(window, drift_rule):
    """在滑动窗口上汇总采纳、改判、漏报，返回是否触达漂移阈值。

    窗口内事件数不足最小样本时不判定，避免小样本误触发。
    """
    if len(window) < drift_rule["min_window_events"]:
        return None
    total = len(window)
    overrides = sum(1 for event in window if event["kind"] == "override")
    misses = sum(1 for event in window if event["kind"] == "miss")
    override_rate = overrides / total
    miss_rate = misses / total
    breaches = []
    if override_rate > drift_rule["max_override_rate"]:
        breaches.append({
            "kind": "override_rate",
            "threshold": drift_rule["max_override_rate"],
            "actual": round(override_rate, 4),
        })
    if miss_rate > drift_rule["max_miss_rate"]:
        breaches.append({
            "kind": "miss_rate",
            "threshold": drift_rule["max_miss_rate"],
            "actual": round(miss_rate, 4),
        })
    if not breaches:
        return None
    return {
        "window_size": total,
        "override_rate": round(override_rate, 4),
        "miss_rate": round(miss_rate, 4),
        "breaches": breaches,
    }
