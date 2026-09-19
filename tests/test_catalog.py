import unittest

from domain import catalog


class CatalogTest(unittest.TestCase):
    def test_covers_more_than_hundred_conditions(self):
        self.assertGreaterEqual(len(catalog.CONDITIONS), 100)

    def test_every_entry_well_formed(self):
        for code, entry in catalog.CONDITIONS.items():
            self.assertEqual(entry["code"], code)
            self.assertTrue(entry["name"])
            self.assertIn(entry["organ"], catalog.ORGANS)
            self.assertIsInstance(entry["emergency"], bool)
            self.assertTrue(entry["protocols"])
            for protocol in entry["protocols"]:
                self.assertIn(protocol, catalog.PROTOCOLS)

    def test_emergency_subset_nonempty(self):
        self.assertGreaterEqual(len(catalog.EMERGENCY_CODES), 30)
        self.assertIn("appendicitis", catalog.EMERGENCY_CODES)
        self.assertNotIn("hcc", catalog.EMERGENCY_CODES)

    def test_protocol_support(self):
        self.assertTrue(catalog.supports_protocol("appendicitis", "noncontrast"))
        # hcc 只在增强期可判定
        self.assertFalse(catalog.supports_protocol("hcc", "noncontrast"))
        self.assertTrue(catalog.supports_protocol("hcc", "portal_venous"))

    def test_unknown_condition(self):
        self.assertIsNone(catalog.get("does_not_exist"))
        self.assertFalse(catalog.supports_protocol("does_not_exist", "portal_venous"))


if __name__ == "__main__":
    unittest.main()
