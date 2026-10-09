"""Invariants that hold for every adapter, plus liveness re-derivation from
the spec formula rather than from the builder's own helper. A registry guard
keeps the fixture list and the fixture directory in lockstep."""

import unittest
from pathlib import Path

from agent_session_detective.ir.builder import build_audit_document
from agent_session_detective.ir.loads import build_loads
from agent_session_detective.wire import load_session
from tests.ir_helpers import assert_adapter_contract

FIXTURES = Path(__file__).parent / "fixtures" / "ir"
ALL_FIXTURES = ["tier1.jsonl", "tier2.jsonl", "tier3.jsonl",
                "tier3-usage.jsonl", "compaction.jsonl",
                "reinject.jsonl", "attachments.jsonl", "calibration.jsonl",
                "dispatch.jsonl"]


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
        # main + three tier-1 subagent transcripts, each grouped by anchor
        "dispatch.jsonl": (4, 0, 0),
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
        # three main spans + one span per subagent transcript
        "dispatch.jsonl": 6,
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


class EvidencePropertiesTest(unittest.TestCase):
    """IR 1.1 evidence invariants (spec Tests #6), re-derived from the
    document itself rather than from the builders' own counters. The
    Σbuckets + unattributed == anchor half of the item is already enforced
    for every fixture (dispatch.jsonl included) by AdapterContractTest via
    ``assert_adapter_contract`` — D3's re-bucketed briefs live inside that
    same bucket sum."""

    def test_load_rows_resolve_and_costs_come_only_from_body_items(self):
        for name in ALL_FIXTURES:
            document = build(name)
            items_by_id = {item.item_id: item for item in document.items}
            agent_ids = {agent.agent_id for agent in document.agents}
            for row in document.skill_loads:
                self.assertIn(row.agent_id, agent_ids, name)
                # biconditional: a cost exists exactly when a body was joined
                self.assertEqual(
                    row.cost_tokens_est is None,
                    row.cost_basis == "unavailable",
                    name,
                )
                if row.body_item_id is None:
                    self.assertIsNone(row.cost_tokens_est, name)
                    self.assertIsNone(row.body_sha1, name)
                else:
                    body = items_by_id[row.body_item_id]
                    self.assertEqual(body.kind, "skill_body", name)
                    self.assertEqual(row.skill_id, body.skill_id, name)
                    self.assertEqual(
                        row.cost_tokens_est, body.tokens_est, name
                    )
                    self.assertEqual(row.body_sha1, body.sha1, name)
                if row.marker_item_id is not None:
                    marker = items_by_id[row.marker_item_id]
                    self.assertEqual(marker.kind, "skill_stub", name)
                    self.assertEqual(row.skill_id, marker.skill_id, name)
                # a stub estimate is never promoted into a load cost: the
                # only cost source is the joined body item above
                self.assertIn(row.cost_basis, ("body", "unavailable"), name)

    def test_skill_load_evidence_counters_equal_an_independent_recount(self):
        for name in ALL_FIXTURES:
            document = build(name)
            rows = document.skill_loads
            channels = {}
            for row in rows:
                if row.channel is not None:
                    channels[row.channel] = channels.get(row.channel, 0) + 1
            _, redundant = build_loads(
                document.skills, document.items, document.agents
            )
            self.assertEqual(
                document.coverage.skill_load_evidence,
                {
                    "loads": sum(1 for r in rows if r.kind == "load"),
                    "reloads": sum(1 for r in rows if r.kind == "reload"),
                    "with_body": sum(
                        1 for r in rows if r.cost_basis == "body"
                    ),
                    "unavailable": sum(
                        1 for r in rows if r.cost_basis == "unavailable"
                    ),
                    "redundant_bodies": redundant,
                    "channels": {
                        channel: channels[channel]
                        for channel in sorted(channels)
                    },
                },
                name,
            )

    def test_dispatch_links_reconcile_with_the_dispatch_list(self):
        for name in ALL_FIXTURES:
            document = build(name)
            dispatches = document.dispatches
            items_by_id = {item.item_id: item for item in document.items}
            agent_ids = {agent.agent_id for agent in document.agents}
            links = document.coverage.dispatch_links

            self.assertEqual(links["dispatches"], len(dispatches), name)
            self.assertEqual(
                links["joined"] + links["joined_via_meta_only"]
                + links["orphan_dispatches"],
                len(dispatches),
                name,
            )
            self.assertEqual(
                links["briefs_found"] + links["briefs_missing"],
                len(dispatches),
                name,
            )
            self.assertEqual(
                links["briefs_found"],
                sum(1 for d in dispatches if d.brief_item_id is not None),
                name,
            )
            self.assertEqual(
                links["orphan_dispatches"],
                sum(1 for d in dispatches if d.subagent_agent_id is None),
                name,
            )
            for dispatch in dispatches:
                tool_item = items_by_id[dispatch.tool_item_id]
                self.assertEqual(tool_item.kind, "tool_call", name)
                self.assertEqual(
                    tool_item.tool_use_id, dispatch.tool_use_id, name
                )
                if dispatch.subagent_agent_id is not None:
                    self.assertIn(dispatch.subagent_agent_id, agent_ids, name)
                if dispatch.brief_item_id is not None:
                    self.assertIn(dispatch.brief_item_id, items_by_id, name)

    def test_phase_recognition_reconciles_under_the_shipped_empty_rules(self):
        for name in ALL_FIXTURES:
            document = build(name)
            recognition = document.coverage.phase_recognition
            items_by_id = {item.item_id: item for item in document.items}
            dispatch_ids = {d.dispatch_id for d in document.dispatches}

            # shipped RULES are empty: tier B never fires, tier A is exactly
            # the set of signature-brief dispatches
            tier_a = sum(
                1 for d in document.dispatches
                if d.brief_item_id is not None
                and items_by_id[d.brief_item_id].kind == "skill_body"
            )
            self.assertEqual(
                recognition["tierA"] + recognition["tierB"]
                + recognition["tierC"],
                len(document.dispatches),
                name,
            )
            self.assertEqual(recognition["tierA"], tier_a, name)
            self.assertEqual(recognition["tierB"], 0, name)
            self.assertIsNone(recognition["rule_set_version"], name)
            self.assertTrue(recognition["corpus_note"], name)

            # phases exist only on recognition evidence and reference only
            # objects that exist in the document; under the shipped empty
            # rules every dispatch observation carries the tier A brief cost
            brief_tokens = {
                d.dispatch_id: d.brief_tokens_est for d in document.dispatches
            }
            for phase in document.phases:
                self.assertGreater(len(phase.observations), 0, name)
                for observation in phase.observations:
                    if observation.dispatch_id is not None:
                        self.assertIn(observation.dispatch_id, dispatch_ids,
                                      name)
                        self.assertEqual(
                            observation.tokens_est,
                            brief_tokens[observation.dispatch_id],
                            name,
                        )
                    if observation.item_id is not None:
                        self.assertIn(observation.item_id, items_by_id, name)


class FixtureParityTest(unittest.TestCase):
    def test_registry_matches_the_fixture_directory(self):
        self.assertEqual(
            sorted(path.name for path in FIXTURES.glob("*.jsonl")),
            sorted(ALL_FIXTURES),
        )


if __name__ == "__main__":
    unittest.main()
