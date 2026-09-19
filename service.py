"""医疗影像模型准入与持续监测后端的运行入口。

- python3 service.py --check            离线复算演示（准入/漂移/调用核对）
- python3 service.py --port 8000        HTTP 服务
- 数据文件与管理员令牌由环境变量 DATA_FILE / ADMIN_TOKEN 配置

HTTP 鉴权：
- /admin/*   使用 X-Admin-Token（平台管理员登记发布版本与机构）
- /v1/*      使用 Authorization: Bearer <机构令牌>，只能访问本院数据
"""

import argparse
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app import Application
from domain.errors import DomainError
from storage import JsonStore

SERVICE_ID = "medical-model-registry"
SERVICE_NAME = "医疗影像模型准入台账"

ADMIN_TOKEN_DEFAULT = "dev-admin-token-please-change"


def health_payload():
    """返回服务状态与身份。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_application(data_file=None, drift_policy=None):
    path = data_file or os.environ.get("DATA_FILE")
    store = JsonStore(path) if path else JsonStore(None)
    return Application(store=store, drift_policy=drift_policy)


def _admin_token():
    return os.environ.get("ADMIN_TOKEN", ADMIN_TOKEN_DEFAULT)


class Handler(BaseHTTPRequestHandler):
    app = None  # 由 main 注入到类属性

    # ── 基础工具 ───────────────────────────────────────────
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            from domain.errors import ValidationError
            raise ValidationError("请求体不是合法 JSON")
        if not isinstance(payload, dict):
            from domain.errors import ValidationError
            raise ValidationError("请求体必须是 JSON 对象")
        return payload

    def _hospital(self):
        from domain.errors import AuthError
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise AuthError("缺少 Authorization: Bearer <机构令牌>")
        return self.app.authenticate(header[len("Bearer "):].strip())

    def _require_admin(self):
        from domain.errors import AuthError
        token = self.headers.get("X-Admin-Token", "")
        if token != _admin_token():
            raise AuthError("管理员令牌无效")

    def log_message(self, *_args):
        return

    # ── 路由 ───────────────────────────────────────────────
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if method == "GET" and path == "/health":
                self._send_json(200, health_payload())
                return
            if path.startswith("/admin/"):
                self._require_admin()
                self._route_admin(method, path)
                return
            if path.startswith("/v1/"):
                hospital_id = self._hospital()
                self._route_tenant(method, path, query, hospital_id)
                return
            self._send_json(404, {"error": "not_found", "message": "路由不存在"})
        except DomainError as error:
            self._send_json(
                error.http_status,
                {"error": type(error).__name__, "message": str(error)},
            )
        except Exception:  # 防御：内部错误不外泄堆栈
            traceback.print_exc()
            self._send_json(500, {"error": "InternalError", "message": "服务内部错误"})

    def _route_admin(self, method, path):
        if method != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return
        payload = self._read_json()
        if path == "/admin/releases":
            record = self.app.register_release(payload)
            self._send_json(201, record)
        elif path == "/admin/tenants":
            record = self.app.register_tenant(
                payload["hospital_id"], payload.get("name", ""), payload["token"]
            )
            record = {k: v for k, v in record.items() if k != "token_hash"}
            self._send_json(201, record)
        else:
            self._send_json(404, {"error": "not_found"})

    def _route_tenant(self, method, path, query, hospital_id):
        app = self.app

        if method == "POST" and path == "/v1/validations":
            record = app.submit_validation(hospital_id, self._read_json())
            self._send_json(201, _validation_view(record))
            return

        if method == "GET" and path == "/v1/validations":
            records = app.list_validations(hospital_id, query.get("release_id"))
            self._send_json(200, {"validations": [_validation_view(r) for r in records]})
            return

        if method == "GET" and path.startswith("/v1/validations/"):
            record = app.get_validation(hospital_id, path.rsplit("/", 1)[1])
            self._send_json(200, _validation_view(record))
            return

        if method == "GET" and path.startswith("/v1/deployments/"):
            record = app.get_deployment(hospital_id, path.rsplit("/", 1)[1])
            self._send_json(200, record)
            return

        if method == "GET" and path.startswith("/v1/releases/"):
            self._send_json(200, app.get_release(path.rsplit("/", 1)[1]))
            return

        if method == "GET" and path == "/v1/serving":
            self._send_json(200, app.get_serving(hospital_id))
            return

        if method == "POST" and path == "/v1/changes":
            record = app.create_change(hospital_id, self._read_json())
            self._send_json(201, record)
            return

        if method == "GET" and path == "/v1/changes":
            self._send_json(200, {"changes": app.list_changes(hospital_id)})
            return

        if method == "POST" and path.startswith("/v1/changes/") and path.endswith("/approve"):
            approval_id = path.split("/")[-2]
            payload = self._read_json()
            record = app.approve_change(
                hospital_id, approval_id, payload["approver"], payload.get("comment")
            )
            self._send_json(200, record)
            return

        if method == "POST" and path.startswith("/v1/changes/") and path.endswith("/reject"):
            approval_id = path.split("/")[-2]
            payload = self._read_json()
            record = app.reject_change(
                hospital_id, approval_id, payload["approver"], payload.get("comment", "")
            )
            self._send_json(200, record)
            return

        if method == "POST" and path == "/v1/feedback":
            self._send_json(201, app.record_feedback(hospital_id, self._read_json()))
            return

        if method == "GET" and path == "/v1/drift/signals":
            release_id = query.get("release_id")
            if not release_id:
                from domain.errors import ValidationError
                raise ValidationError("缺少 release_id 查询参数")
            self._send_json(200, {"signals": app.drift_signals(hospital_id, release_id)})
            return

        if method == "GET" and path == "/v1/drift/events":
            self._send_json(
                200, {"events": app.drift_events(hospital_id, query.get("release_id"))}
            )
            return

        if method == "GET" and path == "/v1/reviews":
            self._send_json(
                200, {"reviews": app.list_reviews(hospital_id, query.get("status"))}
            )
            return

        if method == "POST" and path == "/v1/calls/verify":
            self._send_json(200, app.verify_call(hospital_id, self._read_json()))
            return

        if method == "GET" and path == "/v1/calls/events":
            mismatch_only = query.get("mismatch_only") in ("1", "true", "yes")
            self._send_json(
                200, {"events": app.call_events(hospital_id, mismatch_only=mismatch_only)}
            )
            return

        self._send_json(404, {"error": "not_found", "message": "路由不存在"})


def _validation_view(record):
    """对外视图：仅返回聚合证据，不回显原始提交中的多余内容。"""
    return {
        "id": record["id"],
        "hospital_id": record["hospital_id"],
        "submitted_at": record["submitted_at"],
        "decision": record["decision"],
        "report": record["report"],
    }


# ── 离线自检：准入复算 / 漂移回放 / 调用核对 ────────────────
def run_self_check():
    import fixtures

    app = build_application()

    # 登记三个不可混淆版本
    v1 = app.register_release(fixtures.DEMO_RELEASE_V1, registered_by="platform")
    v1_threshold = app.register_release(fixtures.DEMO_RELEASE_V1_THRESHOLD, registered_by="platform")
    v2 = app.register_release(fixtures.DEMO_RELEASE_V2_WEIGHTS, registered_by="platform")
    assert v1["fingerprint"] != v1_threshold["fingerprint"] != v2["fingerprint"]

    app.register_tenant("hosp-a", "甲医院", "token-a-0123456789abcdef")
    app.register_tenant("hosp-b", "乙医院", "token-b-0123456789abcdef")
    app.register_tenant("hosp-c", "丙医院", "token-c-0123456789abcdef")

    print("== 1. 本地验证准入复算 ==")
    ok = app.submit_validation("hosp-a", fixtures.passing_validation(v1["id"]))
    weak = app.submit_validation("hosp-b", fixtures.emergency_subgroup_weak_validation(v1["id"]))
    small = app.submit_validation("hosp-c", fixtures.insufficient_sample_validation(v1["id"]))
    print(f"  甲医院: {ok['decision']}    （全部格子达标）")
    print(f"  乙医院: {weak['decision']}  （急诊亚组敏感性不达标，必须驳回）")
    print(f"  丙医院: {small['decision']} （hcc 样本不足，只能观察）")
    assert ok["decision"] == "approve"
    assert weak["decision"] == "reject"
    assert small["decision"] == "observe"

    # 协议不符 / 患者级字段必须直接拒绝；急诊亚组缺格受理后判驳回
    from domain.errors import ValidationError
    try:
        app.submit_validation("hosp-a", fixtures.protocol_mismatch_validation(v1["id"]))
        raise AssertionError("协议不符场景应被拒绝")
    except ValidationError:
        print("  拒绝：协议不符")
    gap = app.submit_validation("hosp-a", fixtures.coverage_gap_validation(v1["id"]))
    assert gap["decision"] == "reject"
    assert any(c["reason"] == "coverage_gap" for c in gap["report"]["missing_cells"])
    print("  驳回：急诊亚组缺格（coverage_gap 逐格留痕，不用总平均掩盖）")
    try:
        app.submit_validation(
            "hosp-a",
            {**fixtures.passing_validation(v1["id"]), "patient_id": "P0001"},
        )
        raise AssertionError("患者级字段应被拒绝")
    except ValidationError:
        print("  拒绝：载荷含患者级字段")

    print("== 2. 双人审批上线 ==")
    change = app.create_change("hosp-a", {
        "kind": "activate", "release_id": v1["id"],
        "requested_by": fixtures.PEOPLE["requester"],
        "reason": "本院本地验证全部达标，申请上线",
    })
    app.approve_change("hosp-a", change["id"], fixtures.PEOPLE["reviewer_1"])
    executed = app.approve_change("hosp-a", change["id"], fixtures.PEOPLE["reviewer_2"])
    assert executed["status"] == "executed"
    print(f"  {v1['id']} 经双人批准上线（{change['id']}），证据已冻结")
    try:
        app.approve_change("hosp-a", change["id"], fixtures.PEOPLE["reviewer_2"])
        raise AssertionError("重复审批应失败")
    except DomainError:
        print("  重复审批被拒绝")

    print("== 3. 漂移反馈回放：越限 → 降级仅提示 → 复审 ==")
    drift_event = None
    for batch in fixtures.drift_feedback_batches(v1["id"]):
        result = app.record_feedback("hosp-a", batch)
        if result["drift_event"]:
            drift_event = result["drift_event"]
    assert drift_event is not None, "第 5 批后应触发漂移降级"
    serving = app.get_serving("hosp-a")
    assert serving["mode"] == "advisory"
    print(f"  触发 {drift_event['id']}：{drift_event['reason']}")
    print("  线上模式 active → advisory（仅提示），复审已开立")
    open_reviews = app.list_reviews("hosp-a", status="open")
    assert len(open_reviews) == 1

    print("== 4. 复审通过：重新验证 + 双人恢复 ==")
    reval = app.submit_validation("hosp-a", fixtures.revalidation_after_drift(v1["id"]))
    assert reval["decision"] == "approve"
    resume = app.create_change("hosp-a", {
        "kind": "resume", "release_id": v1["id"],
        "requested_by": fixtures.PEOPLE["requester"],
        "reason": "漂移原因排查完成并重新验证达标",
    })
    app.approve_change("hosp-a", resume["id"], fixtures.PEOPLE["reviewer_1"])
    app.approve_change("hosp-a", resume["id"], fixtures.PEOPLE["reviewer_2"])
    assert app.get_serving("hosp-a")["mode"] == "active"
    assert app.list_reviews("hosp-a", status="open") == []
    print("  重新验证 approve + 双人批准，恢复 active，复审关闭")

    print("== 5. 线上调用版本核对 ==")
    good = app.verify_call("hosp-a", {
        "release_id": v1["id"], "fingerprint": v1["fingerprint"],
        "protocol": "noncontrast", "condition": "appendicitis",
    })
    bad_fp = app.verify_call("hosp-a", {
        "release_id": v1["id"], "fingerprint": "0" * 64,
        "protocol": "noncontrast", "condition": "appendicitis",
    })
    bad_scope = app.verify_call("hosp-a", {
        "release_id": v1["id"], "fingerprint": v1["fingerprint"],
        "protocol": "noncontrast", "condition": "hcc",
    })
    assert good["matched"] and good["advisory_only"] is False
    assert bad_fp["mismatch_reason"] == "fingerprint_mismatch"
    assert bad_scope["mismatch_reason"] == "protocol_not_covered"
    print(f"  命中获批版本: matched={good['matched']}")
    print(f"  指纹不符: {bad_fp['mismatch_reason']}（已留证）")
    print(f"  协议越界: {bad_scope['mismatch_reason']}（已留证）")

    print("== 6. 阈值调整：仅阈值版本 + 双人审批 ==")
    reval_threshold = fixtures.passing_validation(v1_threshold["id"])
    reval_threshold["dataset_id"] = "ds-hosp-a-2026q3-threshold042"
    thr_validation = app.submit_validation("hosp-a", reval_threshold)
    assert thr_validation["decision"] == "approve"
    thr_change = app.create_change("hosp-a", {
        "kind": "threshold_change", "release_id": v1["id"],
        "target_release_id": v1_threshold["id"],
        "requested_by": fixtures.PEOPLE["requester"],
        "reason": "提高急诊灵敏度，阈值 0.50→0.42，已按新阈值重新验证",
    })
    app.approve_change("hosp-a", thr_change["id"], fixtures.PEOPLE["reviewer_1"])
    app.approve_change("hosp-a", thr_change["id"], fixtures.PEOPLE["reviewer_2"])
    assert app.get_serving("hosp-a")["snapshot"]["threshold"] == 0.42
    print("  阈值变更执行成功，线上快照 threshold=0.42")

    print("== 7. 机构隔离 ==")
    from domain.errors import ForbiddenError, AuthError
    try:
        app.get_validation("hosp-b", ok["id"])
        raise AssertionError("跨机构查询应被拒绝")
    except ForbiddenError:
        print("  乙医院查询甲医院验证记录被拒绝")
    try:
        app.authenticate("not-a-real-token")
        raise AssertionError("伪令牌应被拒绝")
    except AuthError:
        print("  无效机构令牌被拒绝")

    print("\n全部检查通过：准入复算、双人审批、漂移降级回放、调用核对均符合规则。")
    return True


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true", help="离线复算与回放自检")
    parser.add_argument("--data-file", default=None, help="JSON 台账文件路径")
    args = parser.parse_args()

    if args.check:
        assert health_payload()["name"] == SERVICE_NAME
        run_self_check()
        print("基础检查通过")
        return

    Handler.app = build_application(args.data_file)
    print(f"{SERVICE_NAME} 监听 0.0.0.0:{args.port}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
