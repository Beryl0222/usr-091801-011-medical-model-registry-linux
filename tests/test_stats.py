import math
import unittest

from domain.stats import auc_interval, hanley_mcneil_se, round3, wilson_interval


class WilsonTest(unittest.TestCase):
    def test_zero_n(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 0.0))

    def test_all_success_large_n(self):
        lo, hi = wilson_interval(100, 100)
        self.assertGreater(lo, 0.95)
        self.assertLessEqual(hi, 1.0)

    def test_all_success_small_n_stays_in_range(self):
        lo, hi = wilson_interval(5, 5)
        # Wilson 不会给出 1.0 的退化区间
        self.assertLess(lo, 1.0)
        self.assertLessEqual(hi, 1.0)

    def test_monotone_with_sample_size(self):
        # 同一点估计 0.9，样本越大区间下界越高
        small = wilson_interval(9, 10)[0]
        large = wilson_interval(90, 100)[0]
        self.assertLess(small, large)

    def test_known_value_half(self):
        lo, hi = wilson_interval(50, 100)
        self.assertAlmostEqual(lo, 0.4038, places=3)
        self.assertAlmostEqual(hi, 0.5962, places=3)

    def test_contains_point(self):
        for pos, n in [(3, 7), (20, 50), (95, 100)]:
            lo, hi = wilson_interval(pos, n)
            self.assertLessEqual(lo, pos / n)
            self.assertGreaterEqual(hi, pos / n)


class AucIntervalTest(unittest.TestCase):
    def test_no_samples_returns_none(self):
        self.assertIsNone(auc_interval(0.9, 0, 10))
        self.assertIsNone(auc_interval(0.9, 10, 0))

    def test_interval_contains_auc_and_narrows_with_n(self):
        small = hanley_mcneil_se(0.9, 20, 20)
        large = hanley_mcneil_se(0.9, 200, 200)
        self.assertGreater(small, large)
        lo, hi = auc_interval(0.9, 100, 100)
        self.assertLess(lo, 0.9)
        self.assertGreater(hi, 0.9)

    def test_round3(self):
        self.assertEqual(round3(0.123456), 0.123)
        self.assertEqual(round3(math.pi), 3.142)


if __name__ == "__main__":
    unittest.main()
