"""应用装配层：登记、准入、生命周期、漂移监测的统一门面。

HTTP 层与离线自检（--check）都只依赖这里的方法，领域规则不落在控制器里。
所有写方法由同一把可重入锁串行化，避免多线程 HTTP 服务下台账写交错。
"""

import threading
import time
from functools import wraps

from storage import JsonStore
from domain.admission import AdmissionService
from domain.drift import DriftMonitor
from domain.lifecycle import LifecycleService
from domain.registry import Registry


def default_clock():
    return time.time()


def _locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Application:
    def __init__(self, store=None, clock=default_clock, drift_policy=None):
        self.store = store if store is not None else JsonStore(None)
        self.clock = clock
        self._lock = threading.RLock()
        self.registry = Registry(self.store, clock)
        self.admission = AdmissionService(self.store, clock, self.registry)
        self.lifecycle = LifecycleService(self.store, clock, self.registry, self.admission)
        self.drift = DriftMonitor(self.store, clock, self.registry, self.lifecycle, drift_policy)

    # ── 登记 ───────────────────────────────────────────────
    @_locked
    def register_release(self, payload, registered_by="admin"):
        return self.registry.register_release(payload, registered_by)

    def get_release(self, release_id):
        return self.registry.get_release(release_id)

    def list_releases(self, model_name=None):
        return self.registry.list_releases(model_name)

    @_locked
    def register_tenant(self, hospital_id, name, token):
        return self.registry.register_tenant(hospital_id, name, token)

    def authenticate(self, token):
        return self.registry.authenticate(token)

    # ── 准入 ───────────────────────────────────────────────
    @_locked
    def submit_validation(self, hospital_id, payload):
        return self.admission.submit(hospital_id, payload)

    def get_validation(self, hospital_id, validation_id):
        return self.admission.get_validation(validation_id, hospital_id)

    def list_validations(self, hospital_id, release_id=None):
        return self.admission.list_validations(hospital_id, release_id)

    def get_deployment(self, hospital_id, release_id):
        return self.lifecycle.get_deployment(hospital_id, release_id)

    # ── 双人审批与生命周期 ─────────────────────────────────
    @_locked
    def create_change(self, hospital_id, payload):
        return self.lifecycle.create_change(
            hospital_id,
            kind=payload["kind"],
            release_id=payload["release_id"],
            requested_by=payload["requested_by"],
            reason=payload["reason"],
            target_release_id=payload.get("target_release_id"),
            comment=payload.get("comment"),
        )

    @_locked
    def approve_change(self, hospital_id, approval_id, approver, comment=None):
        record = self.lifecycle.approve(approval_id, approver, comment)
        if record["hospital_id"] != hospital_id:
            from domain.errors import ForbiddenError
            raise ForbiddenError("不得操作其他机构的审批记录")
        if record["kind"] == "resume" and record["status"] == "executed":
            self.drift.reset_windows(
                hospital_id, record["release_id"], record["id"]
            )
            self.drift.close_reviews_for(
                hospital_id, record["release_id"], record["id"]
            )
        return record

    @_locked
    def reject_change(self, hospital_id, approval_id, approver, comment):
        record = self.lifecycle.reject(approval_id, approver, comment)
        if record["hospital_id"] != hospital_id:
            from domain.errors import ForbiddenError
            raise ForbiddenError("不得操作其他机构的审批记录")
        return record

    def list_changes(self, hospital_id):
        return self.lifecycle.list_changes(hospital_id)

    def get_serving(self, hospital_id):
        return self.lifecycle.get_serving(hospital_id)

    # ── 漂移与调用核对 ─────────────────────────────────────
    @_locked
    def record_feedback(self, hospital_id, payload):
        return self.drift.record_feedback(hospital_id, payload)

    def drift_signals(self, hospital_id, release_id):
        return self.drift.signals(hospital_id, release_id)

    def drift_events(self, hospital_id, release_id=None):
        return self.drift.drift_events(hospital_id, release_id)

    def list_reviews(self, hospital_id, status=None):
        return self.drift.list_reviews(hospital_id, status)

    @_locked
    def verify_call(self, hospital_id, payload):
        return self.drift.verify_call(hospital_id, payload)

    def call_events(self, hospital_id, mismatch_only=False):
        return self.drift.call_events(hospital_id, mismatch_only=mismatch_only)
