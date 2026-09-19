"""台账的 HTTP JSON 接口路由。

身份与范围（演示级约定，生产环境应替换为正式认证）：
- 请求头 X-Hospital-Id 表示调用方所属机构；
- 请求头 X-Role: operator 表示台账运营方（医学工程部门）；
- 本地验证、反馈、漂移、调用审计等数据仅所属机构或运营方可读，
  患者级资料不进入本服务，跨机构查询一律拒绝。
"""

from .checklist import load_checklist
from .store import DomainError, RegistryStore

_default_store = None
_default_checklist = None


def default_store():
    global _default_store
    if _default_store is None:
        _default_store = RegistryStore()
    return _default_store


def default_checklist():
    global _default_checklist
    if _default_checklist is None:
        _default_checklist = load_checklist()
    return _default_checklist


def _require_scope(headers, hospital_id):
    """机构范围数据只能由本机构或运营方访问。"""
    if headers.get("x-role") == "operator":
        return
    if not hospital_id or headers.get("x-hospital-id") != hospital_id:
        raise DomainError(403, "跨机构访问被拒绝：本地验证与运行数据仅所属机构可查")


def _require_operator(headers):
    if headers.get("x-role") != "operator":
        raise DomainError(403, "仅台账运营方可执行该操作")


def dispatch(method, path, query, headers, body, store, checklist):
    """路由入口：返回 (状态码, 响应体)。"""
    try:
        return _route(method, path, query or {}, headers or {}, body, store, checklist)
    except DomainError as error:
        return error.status, {"error": error.message}


def _route(method, path, query, headers, body, store, checklist):
    segments = [segment for segment in path.split("/") if segment]

    if method == "GET" and segments == ["checklist"]:
        return 200, checklist

    if segments == ["releases"]:
        if method == "POST":
            data = dict(body or {})
            actor = data.pop("actor", "unknown")
            release, created = store.register_release(actor=actor, **data)
            return (201 if created else 200), release
        if method == "GET":
            return 200, {"releases": list(store.releases.values())}

    if len(segments) == 2 and segments[0] == "releases" and method == "GET":
        release = store.releases.get(segments[1])
        if not release:
            raise DomainError(404, "发布版本未登记")
        return 200, release

    if segments == ["hospitals"]:
        if method == "POST":
            data = body or {}
            hospital = store.add_hospital(
                hospital_id=data.get("hospital_id"), name=data.get("name", "")
            )
            return 201, hospital
        if method == "GET":
            return 200, {"hospitals": list(store.hospitals.values())}

    if segments == ["validations"]:
        if method == "POST":
            data = body or {}
            hospital_id = data.get("hospital_id")
            _require_scope(headers, hospital_id)
            batch = store.submit_validation(
                checklist,
                hospital_id,
                data.get("release_id", ""),
                data.get("records") or [],
                actor=data.get("actor", headers.get("x-hospital-id", "unknown")),
            )
            return 201, batch
        if method == "GET":
            hospital_id = query.get("hospital_id")
            if hospital_id:
                _require_scope(headers, hospital_id)
                batches = [
                    batch for batch in store.validations.values()
                    if batch["hospital_id"] == hospital_id
                ]
            else:
                _require_operator(headers)
                batches = list(store.validations.values())
            return 200, {"validations": batches}

    if segments == ["deployments"] and method == "GET":
        hospital_id = query.get("hospital_id")
        _require_scope(headers, hospital_id)
        deployments = [
            deployment for deployment in store.deployments.values()
            if deployment["hospital_id"] == hospital_id
        ]
        return 200, {
            "hospital_id": hospital_id,
            "current_release_id": store.current.get(hospital_id),
            "deployments": deployments,
        }

    if segments == ["deployments", "status"] and method == "POST":
        _require_operator(headers)
        data = body or {}
        deployment = store.set_deployment_status(
            data.get("hospital_id", ""),
            data.get("release_id", ""),
            data.get("status", ""),
            evidence_ref=data.get("evidence_ref", ""),
            actor=data.get("actor", "operator"),
        )
        return 200, deployment

    if segments == ["feedback"] and method == "POST":
        data = body or {}
        events = data.get("events") or []
        for event in events:
            _require_scope(headers, event.get("hospital_id"))
        actions = store.add_feedback(checklist, events)
        return 201, {"actions": actions}

    if segments == ["drift"] and method == "GET":
        hospital_id = query.get("hospital_id")
        _require_scope(headers, hospital_id)
        return 200, {
            "hospital_id": hospital_id,
            "reports": [
                report for report in store.drift_reports
                if report["hospital_id"] == hospital_id
            ],
            "rereviews": [
                review for review in store.rereviews
                if review["hospital_id"] == hospital_id
            ],
        }

    if segments == ["change-requests"]:
        if method == "POST":
            data = body or {}
            hospital_id = data.get("hospital_id")
            _require_scope(headers, hospital_id)
            request = store.create_change_request(
                hospital_id,
                data.get("kind", ""),
                data.get("payload"),
                data.get("evidence_refs") or [],
                data.get("requester", ""),
            )
            return 201, request
        if method == "GET":
            hospital_id = query.get("hospital_id")
            _require_scope(headers, hospital_id)
            requests = [
                request for request in store.change_requests.values()
                if request["hospital_id"] == hospital_id
            ]
            return 200, {"change_requests": requests}

    if (
        len(segments) == 3
        and segments[0] == "change-requests"
        and segments[2] == "approve"
        and method == "POST"
    ):
        data = body or {}
        request = store.approve_change_request(segments[1], data.get("approver", ""))
        return 200, request

    if segments == ["verify-call"] and method == "POST":
        data = body or {}
        _require_scope(headers, data.get("hospital_id"))
        audit = store.verify_call(data.get("hospital_id", ""), data.get("release_id", ""))
        return 200, audit

    if segments == ["call-audits"] and method == "GET":
        hospital_id = query.get("hospital_id")
        _require_scope(headers, hospital_id)
        audits = [
            audit for audit in store.call_audits
            if audit["hospital_id"] == hospital_id
        ]
        return 200, {"call_audits": audits}

    raise DomainError(404, "接口不存在")
