import dataclasses
import importlib.util
import unittest
from pathlib import Path

from agent_session_detective.ir import build_audit_document
from agent_session_detective.wire import load_session

FIXTURES = Path(__file__).parent / "fixtures" / "ir"
SCRIPT = Path(__file__).parent.parent / "scripts" / "calibrate_context.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("calibrate_context", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CalibrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_script_module()
        cls.document = build_audit_document(
            load_session(FIXTURES / "calibration.jsonl"), "qoder"
        )
        cls.call = cls.document.requests[-1]
        cls.items_by_id = {item.item_id: item for item in cls.document.items}

    def test_shares_match_the_live_categories(self):
        shares = self.module.ir_window_shares(
            self.call, 128000, self.items_by_id
        )
        self.assertEqual(sorted(shares), sorted(self.module.LIVE_CATEGORIES))
        self.assertAlmostEqual(shares["system_prompt+tools"], 970 / 128000)
        self.assertAlmostEqual(shares["messages"], 14 / 128000)
        self.assertAlmostEqual(shares["skills"], 5 / 128000)
        self.assertAlmostEqual(shares["memory"], 3 / 128000)
        self.assertAlmostEqual(shares["free_space"], 127000 / 128000)

    def test_shares_are_not_normalized(self):
        reminder = next(
            item
            for item in self.document.items
            if item.bucket == "inject" and item.kind == "reminder"
        )
        shares = self.module.ir_window_shares(
            self.call, 128000, self.items_by_id
        )
        self.assertAlmostEqual(
            sum(shares.values()), 1 - reminder.tokens_est / 128000
        )
        self.assertLess(sum(shares.values()), 1.0)

    def test_window_resolves_from_the_document(self):
        window = self.module._window_for(self.document, self.call, None)
        self.assertEqual(window, 128000)
        override = self.module._window_for(self.document, self.call, 64000)
        self.assertEqual(override, 64000)

    def test_call_without_anchor_is_rejected(self):
        stripped = dataclasses.replace(
            self.call,
            input=dataclasses.replace(self.call.input, anchor_tokens=None),
        )
        with self.assertRaises(ValueError):
            self.module.ir_window_shares(stripped, 128000, self.items_by_id)

    def test_bad_window_is_rejected(self):
        for window in (None, 0, -5):
            with self.assertRaises(ValueError):
                self.module.ir_window_shares(
                    self.call, window, self.items_by_id
                )


if __name__ == "__main__":
    unittest.main()
