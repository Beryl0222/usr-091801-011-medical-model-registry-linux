"""患者级数据防泄漏的共用防线。

所有跨机构进入本服务的载荷（验证指标、临床反馈、调用核对）都必须先过这道
检查：出现患者标识类字段或疑似病例明细列表直接拒绝。本服务只保存聚合数据。
注意 model_name 等模型/机构字段不能被误伤，因此标识字段用精确匹配。
"""

from .errors import ValidationError

# 键中包含这些子串即视为患者标识（patient_id / patient_mrn / study_uid 等）
_FORBIDDEN_SUBSTRINGS = ("patient", "mrn", "study_uid", "accession", "id_card", "idcard")
# 精确命中的患者标识键
_FORBIDDEN_EXACT = frozenset({
    "name", "full_name", "patient_name", "cases", "case", "records", "record",
    "birth", "birth_date", "dob", "phone", "phone_number", "email", "ssn",
})


def assert_no_patient_fields(obj, list_allowlist=frozenset(), path=""):
    """递归检查载荷；命中患者标识字段或非白名单嵌套列表即拒绝。

    list_allowlist 中的路径以 "." 结尾，例如 {"cells.", "feedback."}。
    白名单只豁免“此处可以是列表”，列表元素仍要逐个递归检查患者字段。
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            lowered = str(key).lower()
            if lowered in _FORBIDDEN_EXACT or any(
                token in lowered for token in _FORBIDDEN_SUBSTRINGS
            ):
                raise ValidationError(
                    f"检测到患者级字段 {path}{key}：患者级数据不得跨机构传输，仅接受汇总数据"
                )
            assert_no_patient_fields(value, list_allowlist, f"{path}{key}.")
    elif isinstance(obj, list):
        if path and path not in list_allowlist:
            raise ValidationError(
                f"字段 {path[:-1]} 是列表：疑似病例级明细，仅接受汇总数据"
            )
        for element in obj:
            assert_no_patient_fields(element, list_allowlist, path)
