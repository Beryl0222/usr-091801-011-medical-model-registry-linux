"""测试辅助：可控时钟的内存应用与演示数据装配。"""

from app import Application
from storage import JsonStore

import fixtures


class FakeClock:
    def __init__(self, start=1_750_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def build_app(drift_policy=None):
    clock = FakeClock()
    app = Application(JsonStore(None), clock=clock, drift_policy=drift_policy)
    return app, clock


def seed_releases(app):
    v1 = app.register_release(fixtures.DEMO_RELEASE_V1, registered_by="platform")
    v1_threshold = app.register_release(fixtures.DEMO_RELEASE_V1_THRESHOLD, registered_by="platform")
    v2 = app.register_release(fixtures.DEMO_RELEASE_V2_WEIGHTS, registered_by="platform")
    return v1, v1_threshold, v2


def seed_tenants(app, *ids):
    tokens = {}
    for hospital_id in ids:
        token = f"token-{hospital_id}-0123456789"
        app.register_tenant(hospital_id, f"医院-{hospital_id}", token)
        tokens[hospital_id] = token
    return tokens


def approve_twice(app, hospital_id, change_id):
    app.approve_change(hospital_id, change_id, fixtures.PEOPLE["reviewer_1"])
    return app.approve_change(hospital_id, change_id, fixtures.PEOPLE["reviewer_2"])


def activate(app, hospital_id, release_id, reason="测试上线"):
    change = app.create_change(hospital_id, {
        "kind": "activate",
        "release_id": release_id,
        "requested_by": fixtures.PEOPLE["requester"],
        "reason": reason,
    })
    approve_twice(app, hospital_id, change["id"])
    return change
