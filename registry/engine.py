"""情景回放：用仓库中的模拟指标复算准入、触发漂移、核对线上调用。

情景文件（data/simulated_scenario.json）包含医院、发布版本和一条按时间
排序的事件线。回放引擎按时间顺序把事件应用到一份全新的台账上，输出：
- decisions：每次本地验证的复算结论（批准 / 观察 / 驳回）；
- drift_actions：反馈流触发的漂移处置（降级仅提示、开启复审）；
- change_requests：双人审批变更的处理过程；
- call_results：每次线上调用是否命中该院获批版本。
"""

from .store import RegistryStore

ALL_EVENT_TYPES = ("validation", "feedback", "change_request", "approve", "call")


def run_scenario(store, checklist, scenario, include=ALL_EVENT_TYPES):
    """按时间线回放情景，include 决定执行哪些事件类型。"""
    include = set(include)
    release_refs = {}
    for hospital in scenario.get("hospitals", []):
        store.add_hospital(
            hospital_id=hospital.get("hospital_id"), name=hospital.get("name", "")
        )
    for release in scenario.get("releases", []):
        fields = {key: value for key, value in release.items() if key != "ref"}
        record, _ = store.register_release(actor="scenario", **fields)
        release_refs[release["ref"]] = record["release_id"]

    report = {"decisions": [], "drift_actions": [], "change_requests": [], "call_results": []}
    request_refs = {}
    pending_yields = {}
    timeline = sorted(scenario.get("timeline", []), key=lambda event: event.get("at", ""))
    for event in timeline:
        kind = event.get("type")
        if kind not in include:
            continue
        at = event.get("at")
        if kind == "validation":
            batch = store.submit_validation(
                checklist,
                event["hospital_id"],
                release_refs[event["release_ref"]],
                event["records"],
                actor=event.get("actor", "scenario"),
                at=at,
            )
            report["decisions"].append({
                "at": at,
                "hospital_id": event["hospital_id"],
                "release_ref": event["release_ref"],
                "batch_id": batch["batch_id"],
                "decision": batch["decision"],
                "failures": batch["failures"],
            })
        elif kind == "feedback":
            events = []
            for item in event["events"]:
                events.extend(
                    {
                        "hospital_id": event["hospital_id"],
                        "release_id": release_refs[event["release_ref"]],
                        "kind": item["kind"],
                        "at": at,
                    }
                    for _ in range(item.get("count", 1))
                )
            report["drift_actions"].extend(store.add_feedback(checklist, events))
        elif kind == "change_request":
            evidence = [
                _resolve_evidence(ref, store, event)
                for ref in event.get("evidence_refs", [])
            ]
            request = store.create_change_request(
                event["hospital_id"],
                event["kind"],
                event.get("payload"),
                evidence,
                event.get("requester", "scenario"),
                at=at,
            )
            request_refs[event["ref"]] = request["request_id"]
            if event.get("yields_ref"):
                pending_yields[request["request_id"]] = event["yields_ref"]
            report["change_requests"].append({
                "at": at,
                "ref": event["ref"],
                "request_id": request["request_id"],
                "kind": request["kind"],
                "status": request["status"],
            })
        elif kind == "approve":
            request = store.approve_change_request(
                request_refs[event["request_ref"]], event["approver"], at=at
            )
            applied = request.get("applied") or {}
            new_release_id = applied.get("new_release_id")
            yields_ref = pending_yields.get(request["request_id"])
            if new_release_id and yields_ref:
                release_refs[yields_ref] = new_release_id
                del pending_yields[request["request_id"]]
            report["change_requests"].append({
                "at": at,
                "ref": event["request_ref"],
                "request_id": request["request_id"],
                "status": request["status"],
                "approvals": len(request["approvals"]),
            })
        elif kind == "call":
            release_id = release_refs.get(event.get("release_ref"), event.get("release_id"))
            audit = store.verify_call(event["hospital_id"], release_id, at=at)
            report["call_results"].append({
                "at": at,
                "hospital_id": event["hospital_id"],
                "release": event.get("release_ref") or event.get("release_id"),
                "result": audit["result"],
                "detail": audit["detail"],
            })
    return report


def _resolve_evidence(ref, store, event):
    """情景中的符号证据引用：指向该院最近的漂移报告或未结复审。

    复算准入时反馈事件不参与回放，符号引用可能尚无目标，此时保留原符号。
    """
    if ref == "last_drift_report":
        reports = [
            report for report in store.drift_reports
            if report["hospital_id"] == event["hospital_id"]
        ]
        return reports[-1]["report_id"] if reports else ref
    if ref == "open_rereview":
        reviews = [
            review for review in store.rereviews
            if review["hospital_id"] == event["hospital_id"] and review["status"] == "open"
        ]
        return reviews[-1]["rereview_id"] if reviews else ref
    return ref
