import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, SERVICE_NAME, health_payload

import fixtures
from tests.helpers import seed_releases, seed_tenants

ADMIN_TOKEN = "test-admin-token-0001"


class HttpServer:
    def __init__(self, app):
        Handler.app = app
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def request_json(base_url, method, path, token=None, admin=False, payload=None):
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["X-Admin-Token"] = ADMIN_TOKEN
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        body = json.load(error)
        error.body = body
        raise


class HttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["ADMIN_TOKEN"] = ADMIN_TOKEN
        from app import Application
        from storage import JsonStore
        cls.app = Application(JsonStore(None))
        cls.v1, cls.v1_threshold, cls.v2 = seed_releases(cls.app)
        cls.tokens = seed_tenants(cls.app, "hosp-a", "hosp-b")
        cls.http = HttpServer(cls.app)
        cls.base_url = cls.http.base_url

    @classmethod
    def tearDownClass(cls):
        cls.http.stop()

    # ── 既有契约 ───────────────────────────────────────────
    def test_health_payload_unchanged(self):
        self.assertEqual(
            health_payload(),
            {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME},
        )

    def test_health_endpoint(self):
        status, body = request_json(self.base_url, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], SERVICE_ID)

    def test_unknown_route_404(self):
        with self.assertRaises(HTTPError) as error:
            request_json(self.base_url, "GET", "/unknown")
        self.assertEqual(error.exception.code, 404)

    # ── 鉴权 ───────────────────────────────────────────────
    def test_admin_routes_require_admin_token(self):
        with self.assertRaises(HTTPError) as error:
            request_json(self.base_url, "POST", "/admin/tenants",
                         payload={"hospital_id": "x", "token": "0123456789abcdef"})
        self.assertEqual(error.exception.code, 401)

    def test_tenant_routes_require_bearer(self):
        with self.assertRaises(HTTPError) as error:
            request_json(self.base_url, "GET", "/v1/serving")
        self.assertEqual(error.exception.code, 401)
        with self.assertRaises(HTTPError) as error:
            request_json(self.base_url, "GET", "/v1/serving", token="bogus-token")
        self.assertEqual(error.exception.code, 401)

    # ── 完整准入→上线→漂移→核对 API 流程 ──────────────────
    def test_end_to_end_api_flow(self):
        token_a = self.tokens["hosp-a"]
        token_b = self.tokens["hosp-b"]

        status, validation = request_json(
            self.base_url, "POST", "/v1/validations", token=token_a,
            payload=fixtures.passing_validation(self.v1["id"]),
        )
        self.assertEqual(status, 201)
        self.assertEqual(validation["decision"], "approve")

        # 乙医院急诊亚组不达标 → reject
        _, weak = request_json(
            self.base_url, "POST", "/v1/validations", token=token_b,
            payload=fixtures.emergency_subgroup_weak_validation(self.v1["id"]),
        )
        self.assertEqual(weak["decision"], "reject")

        # 双人审批上线
        _, change = request_json(
            self.base_url, "POST", "/v1/changes", token=token_a,
            payload={"kind": "activate", "release_id": self.v1["id"],
                     "requested_by": fixtures.PEOPLE["requester"], "reason": "验证达标"},
        )
        change_id = change["id"]
        request_json(self.base_url, "POST", f"/v1/changes/{change_id}/approve",
                     token=token_a, payload={"approver": fixtures.PEOPLE["reviewer_1"]})
        _, executed = request_json(
            self.base_url, "POST", f"/v1/changes/{change_id}/approve", token=token_a,
            payload={"approver": fixtures.PEOPLE["reviewer_2"]},
        )
        self.assertEqual(executed["status"], "executed")

        _, serving = request_json(self.base_url, "GET", "/v1/serving", token=token_a)
        self.assertEqual(serving["mode"], "active")

        # 漂移回放
        for batch in fixtures.drift_feedback_batches(self.v1["id"]):
            request_json(self.base_url, "POST", "/v1/feedback", token=token_a, payload=batch)
        _, serving = request_json(self.base_url, "GET", "/v1/serving", token=token_a)
        self.assertEqual(serving["mode"], "advisory")

        _, events = request_json(
            self.base_url, "GET", f"/v1/drift/events?release_id={self.v1['id']}", token=token_a,
        )
        self.assertEqual(len(events["events"]), 1)

        # 调用核对
        _, matched = request_json(
            self.base_url, "POST", "/v1/calls/verify", token=token_a,
            payload={"release_id": self.v1["id"], "fingerprint": self.v1["fingerprint"],
                     "protocol": "noncontrast", "condition": "appendicitis"},
        )
        self.assertTrue(matched["matched"])
        self.assertTrue(matched["advisory_only"])

    def test_patient_level_payload_rejected_over_http(self):
        payload = {**fixtures.passing_validation(self.v1["id"]), "patient_id": "P1"}
        with self.assertRaises(HTTPError) as error:
            request_json(self.base_url, "POST", "/v1/validations",
                         token=self.tokens["hosp-a"], payload=payload)
        self.assertEqual(error.exception.code, 422)

    def test_tenant_isolation_over_http(self):
        # hosp-a 已有 approve 验证（由前一个用例提交）；hosp-b 列表不应看到
        _, body = request_json(
            self.base_url, "GET", f"/v1/validations?release_id={self.v1['id']}",
            token=self.tokens["hosp-b"],
        )
        self.assertTrue(all(v["hospital_id"] == "hosp-b" for v in body["validations"]))


if __name__ == "__main__":
    unittest.main()
