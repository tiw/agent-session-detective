"""Invariants that hold for every adapter, plus liveness re-derivation from
the spec formula rather than from the builder's own helper. A registry guard
keeps the fixture list and the fixture directory in lockstep."""

import unittest
from pathlib import Path

from agent_session_detective.ir.builder import build_audit_document
from agent_session_detective.wire import load_session
from tests.ir_helpers import assert_adapter_contract

FIXTURES = Path(__file__).parent / "fixtures" / "ir"
ALL_FIXTURES = ["tier1.jsonl", "tier2.jsonl", "tier3.jsonl",
                "tier3-usage.jsonl", "compaction.jsonl",
                "reinject.jsonl", "attachments.jsonl", "calibration.jsonl"]


def build(name):
    return build_audit_document(load_session(FIXTURES / name), "qoder")


def live_by_formula(item, call):
    """Spec liveness rule, applied without any builder helper."""
    if item.agent_id != call.agent_id:
        return False
    if item.wire_seq >= call.span.first_seq:
        return False
    if item.gone_seq is not None and call.span.first_seq >= item.gone_seq:
        return False
    return True


class AdapterContractTest(unittest.TestCase):
    def test_contract_holds_for_every_fixture(self):
        for name in ALL_FIXTURES:
            with self.subTest(fixture=name):
                assert_adapter_contract(self, build(name))


class LivenessFormulaTest(unittest.TestCase):
    def test_item_refs_match_the_spec_liveness_formula(self):
        for name in ALL_FIXTURES:
            document = build(name)
            for call in document.requests:
                expected = [
                    item.item_id
                    for item in document.items
                    if live_by_formula(item, call)
                ]
                self.assertEqual(
                    [ref.item_id for ref in call.input.item_refs], expected, name
                )

    def test_gone_items_stay_dead_after_the_boundary(self):
        document = build("compaction.jsonl")
        second = document.requests[1]
        refs = [ref.item_id for ref in second.input.item_refs]

        self.assertNotIn("main:1:0", refs)
        self.assertNotIn("main:2:0", refs)
        gone = next(item for item in document.items if item.item_id == "main:1:0")
        self.assertEqual(gone.gone_seq, 3)
        self.assertFalse(live_by_formula(gone, second))
        alive = next(item for item in document.items if item.item_id == "main:4:0")
        self.assertTrue(live_by_formula(alive, second))

    def test_inter_span_tool_result_is_alive_at_the_next_request(self):
        document = build("tier1.jsonl")
        first, second = document.requests[0], document.requests[1]
        result = next(
            item for item in document.items if item.item_id == "main:6:0"
        )

        self.assertEqual((result.bucket, result.kind), ("tool", "tool_result"))
        self.assertGreater(result.wire_seq, first.span.last_seq)
        self.assertLess(result.wire_seq, second.span.first_seq)
        self.assertFalse(live_by_formula(result, first))
        self.assertTrue(live_by_formula(result, second))
        self.assertIn(
            "main:6:0", [ref.item_id for ref in second.input.item_refs]
        )


class IdentityContractTest(unittest.TestCase):
    def test_no_call_is_built_without_recorded_identity(self):
        for name in ALL_FIXTURES:
            for call in build(name).requests:
                self.assertIn(call.identity_tier, (1, 2), name)
        self.assertEqual(build("tier3.jsonl").requests, [])

    def test_tier_one_calls_carry_the_full_anchor(self):
        for name in ("tier1.jsonl", "compaction.jsonl"):
            document = build(name)
            self.assertGreater(len(document.requests), 0, name)
            for call in document.requests:
                self.assertEqual(call.identity_tier, 1, name)
                self.assertIsNotNone(call.input.request_id, name)
                self.assertIsNotNone(call.input.request_hash, name)
                self.assertIsNotNone(call.input.response_hash, name)

    def test_tier_two_states_missing_hashes_as_missing(self):
        document = build("tier2.jsonl")
        self.assertGreater(len(document.requests), 0)
        for call in document.requests:
            self.assertEqual(call.identity_tier, 2)
            self.assertIsNotNone(call.input.request_id)
            self.assertIsNone(call.input.request_hash)
            self.assertIsNone(call.input.response_hash)
        coverage = document.coverage
        for name in ("request_hash", "response_hash",
                     "context_usage_ratio", "credits"):
            self.assertEqual(
                coverage.per_field[name], {"fact": 0, "est": 0, "missing": 3}
            )


class CoverageLedgerTest(unittest.TestCase):
    TIER_COUNTS = {
        "tier1.jsonl": (1, 0, 0),
        "tier2.jsonl": (0, 1, 0),
        "tier3.jsonl": (0, 0, 1),
        "tier3-usage.jsonl": (0, 0, 1),
        "compaction.jsonl": (1, 0, 0),
        "reinject.jsonl": (1, 0, 0),
        "attachments.jsonl": (1, 0, 0),
        "calibration.jsonl": (1, 0, 0),
    }
    CHECKED_CALLS = {
        "tier1.jsonl": 3,
        "tier2.jsonl": 0,
        "tier3.jsonl": 0,
        "tier3-usage.jsonl": 0,
        "compaction.jsonl": 2,
        "reinject.jsonl": 3,
        "attachments.jsonl": 7,
        "calibration.jsonl": 2,
    }

    def test_coverage_reconciles_with_the_document_ledger(self):
        for name in ALL_FIXTURES:
            document = build(name)
            calls = document.requests
            coverage = document.coverage
            anchored = sum(
                1 for call in calls if call.input.anchor_tokens is not None
            )
            self.assertEqual(
                coverage.requests_with_anchor,
                "%d/%d" % (anchored, len(calls)),
                name,
            )
            self.assertEqual(
                coverage.per_field["anchor_tokens"],
                {"fact": anchored, "est": 0, "missing": len(calls) - anchored},
                name,
            )
            self.assertEqual(
                coverage.per_field["tokens_est"],
                {"fact": 0, "est": len(document.items), "missing": 0},
                name,
            )
            identity = coverage.request_identity
            self.assertEqual(
                (identity["tier1_sessions"], identity["tier2_sessions"],
                 identity["tier3_ungroupable_sessions"]),
                self.TIER_COUNTS[name],
                name,
            )
            checked = identity["derived_verification"]["checked_calls"]
            self.assertEqual(checked, self.CHECKED_CALLS[name], name)
            self.assertEqual(
                checked,
                sum(1 for call in calls if call.identity_tier == 1),
                name,
            )


class FixtureParityTest(unittest.TestCase):
    def test_registry_matches_the_fixture_directory(self):
        self.assertEqual(
            sorted(path.name for path in FIXTURES.glob("*.jsonl")),
            sorted(ALL_FIXTURES),
        )


if __name__ == "__main__":
    unittest.main()
