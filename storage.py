"""JSON 文件持久化。

仅保存登记信息、聚合指标、审批与事件记录；任何患者级字段在进入应用层前
就会被拒绝（见 domain.admission），因此不会落盘。写入采用临时文件原子替换。
"""

import json
import os
import tempfile


class JsonStore:
    """以单个 JSON 文件保存全部台账状态的简单存储。"""

    def __init__(self, path):
        self.path = path
        self.data = self._empty()
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                self.data = json.load(handle)

    @staticmethod
    def _empty():
        return {
            "releases": {},        # release_id -> release 记录
            "release_seq": 0,
            "tenants": {},         # hospital_id -> 租户记录（含令牌哈希）
            "validations": {},     # validation_id -> 验证集记录
            "validation_seq": 0,
            "deployments": {},     # f"{hospital_id}:{release_id}" -> 部署记录
            "serving": {},         # hospital_id -> 线上形态
            "approvals": {},       # approval_id -> 双人审批记录
            "approval_seq": 0,
            "feedback": {},        # 机构:版本:病种:亚组 -> 反馈批次
            "feedback_archive": [],
            "window_resets": [],
            "drift_events": [],
            "drift_seq": 0,
            "reviews": {},
            "call_events": [],
            "events": [],          # 预留：审计事件
            "event_seq": 0,
        }

    def save(self):
        if not self.path:
            return  # 纯内存模式（自检/测试）
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
