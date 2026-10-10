import unittest
from pathlib import Path

from agent_session_detective import __version__
from agent_session_detective.ir.builder import (
    alive_items_for_call,
    build_audit_document,
)
from agent_session_detective.wire import load_session

FIXTURES = Path(__file__).parent / "fixtures" / "ir"

RH1 = "a1a2a3a4b1b2b3b4"
RSP1 = "c1c2c3c4d1d2d3d4"
RH2 = "e1e2e3e4f1f2f3f4"
RSP2 = "0a0b0c0d1a1b1c1d"
RSP3 = "2a2b2c2d3a3b3c3d"

ZERO_BUCKETS = {
    "system": 0,
    "tools": 0,
    "user": 0,
    "inject": 0,
    "skill": 0,
    "assistant": 0,
    "tool": 0,
}


def build(name):
    return build_audit_document(load_session(FIXTURES / name), "qoder")


def ref_ids(call):
    return [ref.item_id for ref in call.input.item_refs]


def ref_tuples(call):
    return [(ref.bucket, ref.tokens_est) for ref in call.input.item_refs]


def parts(call):
    return [(part.type, part.item_id) for part in call.output.parts]


def item_states(document):
    return {item.item_id: item.gone_seq for item in document.items}


class Tier1GoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build("tier1.jsonl")

    def call(self, index):
        return self.document.requests[index]

    def test_three_spans_are_confirmed_by_the_anchor_runs(self):
        document = self.document
        self.assertEqual(
            [call.call_id for call in document.requests],
            ["main:0", "main:1", "main:2"],
        )
        self.assertEqual(
            [(call.span.first_seq, call.span.last_seq, call.span.n_records)
             for call in document.requests],
            [(3, 5, 3), (7, 7, 1), (8, 8, 1)],
        )
        self.assertEqual(
            [call.identity_tier for call in document.requests], [1, 1, 1]
        )
        self.assertTrue(all(call.ts is not None for call in document.requests))
        self.assertEqual(
            document.coverage.request_identity["derived_verification"],
            {
                "rule": "positional: user opens a turn; positive usage closes the span",
                "checked_calls": 3,
                "disagreements": 0,
            },
        )

    def test_first_call_buckets_its_anchor_against_live_items(self):
        call = self.call(0)

        self.assertEqual(ref_ids(call), ["main:1:0", "main:2:0"])
        self.assertEqual(ref_tuples(call), [("user", 2), ("skill", 10)])
        self.assertEqual(
            call.input.buckets, dict(ZERO_BUCKETS, user=2, skill=10)
        )
        self.assertEqual(call.input.anchor_tokens, 1000)
        self.assertEqual(call.input.unattributed_tokens, 988)
        self.assertEqual(call.input.cache_read, 200)
        self.assertEqual(call.input.cache_write_5m, 0)
        self.assertEqual(call.input.cache_write_1h, 0)

    def test_later_calls_accumulate_live_items_within_the_session(self):
        first, second = self.call(1), self.call(2)

        self.assertEqual(
            ref_ids(first),
            ["main:1:0", "main:2:0", "main:3:0",
             "main:4:0", "main:4:1", "main:6:0"],
        )
        self.assertEqual(
            first.input.buckets,
            dict(ZERO_BUCKETS, user=2, skill=10, assistant=10, tool=2),
        )
        self.assertEqual(
            (first.input.anchor_tokens, first.input.unattributed_tokens),
            (2000, 1976),
        )
        self.assertEqual(len(ref_ids(second)), 7)
        self.assertEqual(
            second.input.buckets,
            dict(ZERO_BUCKETS, user=2, skill=10, assistant=11, tool=2),
        )
        self.assertEqual(
            (second.input.anchor_tokens, second.input.unattributed_tokens),
            (2100, 2075),
        )

    def test_identity_credits_and_output_parts_ride_on_the_span(self):
        first, second, third = self.call(0), self.call(1), self.call(2)

        self.assertEqual(first.input.request_id, "req-1")
        self.assertEqual(first.input.request_hash, RH1)
        self.assertEqual(first.input.response_hash, RSP1)
        self.assertEqual(first.input.context_usage_ratio, 0.0078125)
        self.assertEqual(
            first.input.credits,
            {"credits": 10, "original_credits": 12, "billable": True},
        )
        self.assertEqual(first.model, "qoder-pro")
        self.assertEqual(first.output.output_tokens, 50)
        self.assertEqual(
            parts(first),
            [("think", "main:3:0"), ("text", "main:4:0"),
             ("tool_call", "main:4:1")],
        )
        self.assertEqual(
            (second.input.request_id, second.input.request_hash,
             second.input.response_hash),
            ("req-2", RH2, RSP2),
        )
        self.assertIsNone(second.input.context_usage_ratio)
        self.assertIsNone(second.input.credits)
        self.assertEqual(second.output.output_tokens, 40)
        self.assertEqual(parts(second), [("text", "main:7:0")])
        self.assertEqual(
            (third.input.request_id, third.input.request_hash,
             third.input.response_hash),
            ("req-3", RH2, RSP3),
        )
        self.assertEqual(third.output.output_tokens, 20)
        self.assertEqual(parts(third), [("text", "main:8:0")])

    def test_items_and_provenance_counters(self):
        document = self.document

        self.assertEqual(
            [(item.item_id, item.bucket, item.kind) for item in document.items],
            [
                ("main:1:0", "user", "user_message"),
                ("main:2:0", "skill", "skill_catalog"),
                ("main:3:0", "assistant", "thinking"),
                ("main:4:0", "assistant", "assistant_text"),
                ("main:4:1", "assistant", "tool_call"),
                ("main:6:0", "tool", "tool_result"),
                ("main:7:0", "assistant", "assistant_text"),
                ("main:8:0", "assistant", "assistant_text"),
            ],
        )
        catalog = document.items[1]
        self.assertEqual(catalog.channel, "qoder:attachment:skill_listing")
        self.assertEqual(catalog.tokens_est, 10)
        self.assertEqual(catalog.size_chars, 43)
        self.assertEqual(
            document.coverage.bucket_sources,
            {"envelope": 8, "signature": 0,
             "envelope_vs_signature_conflicts": 0},
        )
        self.assertEqual(document.coverage.unknown_channels, [])
        self.assertEqual(document.coverage.dropped_records, {})

    def test_agent_entry_and_skill_entities(self):
        document = self.document

        self.assertEqual(len(document.agents), 1)
        agent = document.agents[0]
        self.assertEqual(agent.agent_id, "main")
        self.assertIsNone(agent.parent_id)
        self.assertEqual(agent.origin_channel, "qoder:main")
        self.assertIsNone(agent.model)
        self.assertIsNone(agent.context_window)
        self.assertIsNone(agent.active_leaf)
        self.assertEqual(agent.request_ids, ["main:0", "main:1", "main:2"])

        self.assertEqual(
            [skill.skill_id for skill in document.skills], ["demo", "other"]
        )
        demo = document.skills[0]
        self.assertEqual(demo.name, "demo")
        self.assertEqual(len(demo.observations), 1)
        observation = demo.observations[0]
        self.assertEqual(
            (observation.kind, observation.channel), ("listing", "skill_listing")
        )
        self.assertEqual(observation.agent_id, "main")
        self.assertEqual(observation.call_id, "main:0")
        self.assertEqual(observation.item_id, "main:2:0")
        self.assertEqual(observation.tokens_est, 5)
        self.assertIsNotNone(observation.ts)
        self.assertIsNone(observation.body_sha1)
        self.assertEqual(document.skills[1].observations[0].tokens_est, 5)

    def test_per_field_coverage_and_document_envelope(self):
        document = self.document
        coverage = document.coverage

        self.assertEqual(coverage.per_field["anchor_tokens"],
                         {"fact": 3, "est": 0, "missing": 0})
        self.assertEqual(coverage.per_field["request_id"],
                         {"fact": 3, "est": 0, "missing": 0})
        self.assertEqual(coverage.per_field["request_hash"],
                         {"fact": 3, "est": 0, "missing": 0})
        self.assertEqual(coverage.per_field["response_hash"],
                         {"fact": 3, "est": 0, "missing": 0})
        self.assertEqual(coverage.per_field["context_usage_ratio"],
                         {"fact": 1, "est": 0, "missing": 2})
        self.assertEqual(coverage.per_field["credits"],
                         {"fact": 1, "est": 0, "missing": 2})
        self.assertEqual(coverage.per_field["tokens_est"],
                         {"fact": 0, "est": 8, "missing": 0})
        self.assertEqual(coverage.requests_with_anchor, "3/3")
        identity = coverage.request_identity
        self.assertEqual(
            (identity["tier1_sessions"], identity["tier2_sessions"],
             identity["tier3_ungroupable_sessions"]),
            (1, 0, 0),
        )
        self.assertEqual(identity["ungroupable_records"], 0)
        self.assertEqual(
            coverage.notes,
            [
                "qoder: no system-prompt channel; the system bucket stays 0",
                "qoder: prefix_hashes are not exposed by the transcript; left null",
            ],
        )
        self.assertEqual(document.ir_version, "1.6")
        self.assertEqual(document.adapter, {"id": "qoder", "version": "1.0"})
        self.assertEqual(document.estimator_version, "cjk-1.0")
        self.assertEqual(
            document.generator,
            {"name": "agent-session-detective", "version": __version__},
        )
        self.assertEqual(document.compactions, [])
        self.assertEqual(len(document.source_files), 1)
        self.assertTrue(document.source_files[0].endswith("tier1.jsonl"))


class Tier2GoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build("tier2.jsonl")

    def test_spans_are_derived_positionally_and_never_verified(self):
        document = self.document

        self.assertEqual(
            [(call.span.first_seq, call.span.last_seq, call.span.n_records)
             for call in document.requests],
            [(3, 5, 3), (7, 7, 1), (8, 8, 1)],
        )
        self.assertEqual(
            [call.identity_tier for call in document.requests], [2, 2, 2]
        )
        self.assertEqual(
            document.coverage.request_identity["derived_verification"]["checked_calls"],
            0,
        )
        self.assertEqual(document.coverage.requests_with_anchor, "3/3")

    def test_identity_falls_back_to_the_usage_record(self):
        call = self.document.requests[0]

        self.assertEqual(call.input.request_id, "req-1")
        self.assertIsNone(call.input.request_hash)
        self.assertIsNone(call.input.response_hash)
        self.assertEqual(call.input.anchor_tokens, 1000)
        self.assertEqual(call.input.unattributed_tokens, 988)
        self.assertEqual(call.input.cache_read, 200)
        self.assertEqual(call.output.output_tokens, 50)
        self.assertEqual(ref_ids(call), ["main:1:0", "main:2:0"])
        self.assertEqual(
            parts(call),
            [("think", "main:3:0"), ("text", "main:4:0"),
             ("tool_call", "main:4:1")],
        )

    def test_unavailable_identity_fields_are_counted_missing(self):
        coverage = self.document.coverage

        for name in ("request_hash", "response_hash",
                     "context_usage_ratio", "credits"):
            self.assertEqual(coverage.per_field[name],
                             {"fact": 0, "est": 0, "missing": 3})
        self.assertEqual(coverage.per_field["anchor_tokens"],
                         {"fact": 3, "est": 0, "missing": 0})
        self.assertEqual(coverage.per_field["request_id"],
                         {"fact": 3, "est": 0, "missing": 0})
        self.assertEqual(
            (coverage.request_identity["tier1_sessions"],
             coverage.request_identity["tier2_sessions"]),
            (0, 1),
        )


class Tier3GoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build("tier3.jsonl")

    def test_no_llm_call_is_ever_fabricated(self):
        document = self.document
        identity = document.coverage.request_identity

        self.assertEqual(document.requests, [])
        self.assertEqual(document.coverage.requests_with_anchor, "0/0")
        self.assertEqual(identity["tier1_sessions"], 0)
        self.assertEqual(identity["tier2_sessions"], 0)
        self.assertEqual(identity["tier3_ungroupable_sessions"], 1)
        self.assertEqual(identity["ungroupable_records"], 3)
        self.assertEqual(identity["derived_verification"]["checked_calls"], 0)

    def test_items_and_runtime_facts_survive_without_requests(self):
        document = self.document

        self.assertEqual(
            [(item.item_id, item.bucket, item.kind) for item in document.items],
            [
                ("main:1:0", "user", "user_message"),
                ("main:2:0", "assistant", "assistant_text"),
                ("main:3:0", "assistant", "tool_call"),
                ("main:4:0", "assistant", "assistant_text"),
            ],
        )
        self.assertEqual(
            document.coverage.bucket_sources,
            {"envelope": 4, "signature": 0,
             "envelope_vs_signature_conflicts": 0},
        )
        agent = document.agents[0]
        self.assertEqual(agent.model, "qoder-lite")
        self.assertEqual(agent.context_window, 200000)
        self.assertEqual(agent.active_leaf,
                         {"leaf_uuid": "leaf-1", "explicit": True})
        self.assertEqual(agent.request_ids, [])
        self.assertEqual(document.coverage.per_field["tokens_est"],
                         {"fact": 0, "est": 4, "missing": 0})
        self.assertEqual(
            document.coverage.notes,
            [
                "qoder: no system-prompt channel; the system bucket stays 0",
                "qoder: prefix_hashes are not exposed by the transcript; left null",
                "main: tier 3 (no request identity); records are ungroupable, "
                "no LLMCall built",
            ],
        )


class Tier3UsageGoldenTest(unittest.TestCase):
    """Tier 3 with a positive-usage record: spans close but never become calls.

    Guards the span-fold branch: positional_spans closes a span on usage
    alone, so tier-3 records that form spans must land in ungroupable_records
    rather than vanish or be fabricated into LLMCalls.
    """

    def test_usage_bearing_span_folds_into_ungroupable_records(self):
        document = build("tier3-usage.jsonl")
        identity = document.coverage.request_identity

        self.assertEqual(document.requests, [])
        self.assertEqual(identity["tier3_ungroupable_sessions"], 1)
        self.assertEqual(identity["ungroupable_records"], 2)
        self.assertIn(
            "main: tier 3 (no request identity); records are ungroupable, "
            "no LLMCall built",
            document.coverage.notes,
        )


class AdapterScopedNotesTest(unittest.TestCase):
    def test_qoder_claims_never_leak_into_other_adapters(self):
        session = load_session(FIXTURES / "tier1.jsonl")
        kimi = build_audit_document(session, "kimi-cli")
        qoder = build("tier1.jsonl")

        self.assertFalse(any("qoder" in note for note in kimi.coverage.notes))
        # The adapter-prefixed header hides the real leak: the Qoder-verified
        # claim text itself must not be stamped on another adapter's notes.
        self.assertFalse(
            any(
                "no system-prompt channel" in note
                for note in kimi.coverage.notes
            )
        )
        self.assertTrue(
            any(
                "no system-prompt channel" in note
                for note in qoder.coverage.notes
            )
        )


class CompactionGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build("compaction.jsonl")

    def test_boundary_metadata_and_restored_files(self):
        document = self.document

        self.assertEqual(len(document.compactions), 1)
        compaction = document.compactions[0]
        self.assertEqual(compaction.compaction_id, "main:compact:0")
        self.assertEqual(compaction.agent_id, "main")
        self.assertIsNotNone(compaction.ts)
        self.assertEqual(compaction.boundary_seq, 3)
        self.assertEqual(compaction.trigger, "auto")
        self.assertEqual((compaction.pre_tokens, compaction.post_tokens),
                         (900, 90))
        self.assertEqual(compaction.messages_summarized, 12)
        self.assertEqual(compaction.duration_ms, 2500)
        self.assertEqual(compaction.restored_item_ids, ["main:5:0"])

    def test_compacted_items_get_a_gone_sequence(self):
        document = self.document

        self.assertEqual(
            [(item.item_id, item.bucket, item.kind) for item in document.items],
            [
                ("main:1:0", "user", "user_message"),
                ("main:2:0", "assistant", "assistant_text"),
                ("main:4:0", "inject", "compact_summary"),
                ("main:5:0", "inject", "compact_restore"),
                ("main:6:0", "assistant", "assistant_text"),
            ],
        )
        self.assertEqual(
            item_states(document),
            {
                "main:1:0": 3,
                "main:2:0": 3,
                "main:4:0": None,
                "main:5:0": None,
                "main:6:0": None,
            },
        )

    def test_calls_after_the_boundary_see_only_surviving_items(self):
        document = self.document
        first, second = document.requests

        self.assertEqual(
            [(call.span.first_seq, call.span.last_seq)
             for call in document.requests],
            [(2, 2), (6, 6)],
        )
        self.assertEqual(ref_ids(first), ["main:1:0"])
        self.assertEqual(first.input.buckets, dict(ZERO_BUCKETS, user=1))
        self.assertEqual(
            (first.input.anchor_tokens, first.input.unattributed_tokens),
            (500, 499),
        )
        self.assertEqual(ref_ids(second), ["main:4:0", "main:5:0"])
        self.assertEqual(second.input.buckets, dict(ZERO_BUCKETS, inject=9))
        self.assertEqual(
            (second.input.anchor_tokens, second.input.unattributed_tokens),
            (300, 291),
        )
        self.assertEqual(document.coverage.requests_with_anchor, "2/2")
        self.assertEqual(
            document.coverage.request_identity["derived_verification"]["checked_calls"],
            2,
        )


class AliveItemsHelperTest(unittest.TestCase):
    def test_helper_matches_the_builder_item_refs(self):
        document = build("tier1.jsonl")
        lengths = []
        for call in document.requests:
            alive = alive_items_for_call(document.items, call)
            self.assertEqual([item.item_id for item in alive], ref_ids(call))
            lengths.append(len(alive))
        self.assertEqual(lengths, [2, 6, 7])

        compacted = build("compaction.jsonl")
        second = compacted.requests[1]
        self.assertEqual(
            [item.item_id for item in alive_items_for_call(compacted.items, second)],
            ["main:4:0", "main:5:0"],
        )


class InterventionWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build("interventions.jsonl")

    def test_main_user_messages_carry_interventions(self):
        self.assertEqual(
            [
                (
                    iv.item_id,
                    iv.label,
                    iv.rule,
                    iv.evidence,
                    list(iv.carriers),
                    iv.lead_sha1,
                )
                for iv in self.document.interventions
            ],
            [
                (
                    "main:1:0",
                    "confirm",
                    "acknowledgement",
                    "开始落地",
                    [],
                    "4f1bd19d16f262dee9265586c09a9bbbe87fefb1",
                ),
                (
                    "main:3:0",
                    "unclassified",
                    "unclassified",
                    "",
                    [],
                    "d2c33e2fe87f69356f03045893609a974d3e14af",
                ),
                (
                    "main:5:0",
                    "unclassified",
                    "unclassified",
                    "",
                    ["command"],
                    "e03c9ddf92a50054cf5bdbbbd6233d139f866482",
                ),
            ],
        )

    def test_injected_skill_and_sidechain_turns_get_no_intervention(self):
        document = self.document
        by_id = {item.item_id: item for item in document.items}
        self.assertEqual(by_id["main:7:0"].kind, "compact_summary")
        self.assertEqual(by_id["main:9:0"].kind, "skill_body")
        sidechain_item = by_id["sidechain:s1:11:0"]
        self.assertEqual(
            (sidechain_item.agent_id, sidechain_item.bucket, sidechain_item.kind),
            ("sidechain:s1", "user", "user_message"),
        )
        self.assertEqual(
            sorted(iv.item_id for iv in document.interventions),
            ["main:1:0", "main:3:0", "main:5:0"],
        )

    def test_every_intervention_joins_a_main_user_message_item(self):
        document = self.document
        by_id = {item.item_id: item for item in document.items}
        for iv in document.interventions:
            item = by_id[iv.item_id]
            self.assertEqual(
                (item.agent_id, item.bucket, item.kind),
                ("main", "user", "user_message"),
            )

    def test_coverage_reports_the_distinct_lead_block(self):
        evidence = self.document.coverage.intervention_evidence
        self.assertEqual(evidence["rows"], 3)
        self.assertEqual(evidence["leads"], 3)
        self.assertEqual(evidence["labels"], {"confirm": 1, "unclassified": 2})
        self.assertEqual(evidence["residue"], 2)
        self.assertEqual(evidence["residue_rate"], "2/3")
        self.assertEqual(evidence["label_conflicts"], [])


if __name__ == "__main__":
    unittest.main()
