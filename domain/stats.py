"""准入判定使用的置信区间计算。

敏感性/特异性是二项比例，采用 Wilson 区间（小样本下也保持在 [0,1] 内）；
AUC 区间采用 Hanley-McNeil 近似。所有区间均为双侧 95%（z=1.96），
与验证清单保持同一算法，便于离线复算。
"""

import math

Z_95 = 1.959963984540054


def wilson_interval(positives, n, z=Z_95):
    """返回 Wilson 95% 置信区间 (lower, upper)，n 为 0 时区间为 (0.0, 0.0)。"""
    if n <= 0:
        return 0.0, 0.0
    phat = positives / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def hanley_mcneil_se(auc, n_pos, n_neg):
    """Hanley-McNeil AUC 标准误（n_pos/n_neg 为阳/阴性样本数）。"""
    if n_pos <= 0 or n_neg <= 0:
        return None
    q1 = auc / (2 - auc)
    q2 = 2 * auc * auc / (1 + auc)
    numerator = (
        auc * (1 - auc)
        + (n_pos - 1) * (q1 - auc * auc)
        + (n_neg - 1) * (q2 - auc * auc)
    )
    if numerator < 0:
        numerator = 0.0
    return math.sqrt(numerator / (n_pos * n_neg))


def auc_interval(auc, n_pos, n_neg, z=Z_95):
    """返回 AUC 的近似 95% 置信区间；样本不足无法估计时返回 None。"""
    se = hanley_mcneil_se(auc, n_pos, n_neg)
    if se is None:
        return None
    return max(0.0, auc - z * se), min(1.0, auc + z * se)


def round3(value):
    """指标统一保留 3 位小数。"""
    return round(value + 0.0, 3)
