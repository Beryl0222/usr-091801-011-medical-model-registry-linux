import os
import tempfile
import unittest

from app import Application
from storage import JsonStore
from tests.helpers import activate, seed_releases, seed_tenants

import fixtures


class PersistenceRoundTripTest(unittest.TestCase):
    def test_full_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ledger.json")

            store = JsonStore(path)
            app = Application(store)
            v1, _, _ = seed_releases(app)
            seed_tenants(app, "hosp-a")
            app.submit_validation("hosp-a", fixtures.passing_validation(v1["id"]))
            activate(app, "hosp-a", v1["id"])
            for batch in fixtures.drift_feedback_batches(v1["id"])[:3]:
                app.record_feedback("hosp-a", batch)
            self.assertTrue(os.path.exists(path))

            reopened = Application(JsonStore(path))
            self.assertEqual(reopened.authenticate("token-hosp-a-0123456789"), "hosp-a")
            serving = reopened.get_serving("hosp-a")
            self.assertEqual(serving["release_id"], v1["id"])
            self.assertEqual(serving["snapshot"]["fingerprint"], v1["fingerprint"])
            validations = reopened.list_validations("hosp-a")
            self.assertEqual(len(validations), 1)
            self.assertEqual(validations[0]["decision"], "approve")
            # 反馈计数窗口也持久化
            signals = reopened.drift_signals("hosp-a", v1["id"])
            self.assertTrue(signals)

    def test_memory_store_does_not_write_file(self):
        store = JsonStore(None)
        store.save()  # 不应抛错，也不应产生文件
        self.assertIsNone(store.path)


if __name__ == "__main__":
    unittest.main()
