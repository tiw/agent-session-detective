# tests/test_ir_estimator.py
import unittest

from agent_session_detective.ir.estimator import ESTIMATOR_VERSION, estimate


class EstimatorTest(unittest.TestCase):
    def test_version_constant(self):
        self.assertEqual(ESTIMATOR_VERSION, "cjk-1.0")

    def test_cjk_text_counts_each_character(self):
        self.assertEqual(estimate("审计报告"), 4)

    def test_ascii_text_is_four_chars_per_token(self):
        self.assertEqual(estimate("hello world"), 2)

    def test_mixed_text(self):
        self.assertEqual(estimate("audit 审计"), 3)

    def test_empty_and_non_string_are_zero(self):
        for value in ("", None, 5, ["x"]):
            self.assertEqual(estimate(value), 0)

    def test_nonempty_text_is_never_zero(self):
        self.assertEqual(estimate("a"), 1)
