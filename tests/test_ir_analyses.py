"""Golden analyses over two dedicated fixtures plus cross-fixture invariants.

Every number here was hand-derived from the fixture files: item token
estimates (ASCII: ``len // 4``, min 1), the per-call live-item sets, and the
per-call anchors were cross-checked against each other (bucket sums and the
reported unattributed residual are two views of the same arithmetic)."""

import hashlib
import unittest
from pathlib import Path

import agent_session_detective.ir as ir
from agent_session_detective.ir.analyses import build_analyses, subagent_returns
from agent_session_detective.ir.builder import build_audit_document
from agent_session_detective.ir.schema import BUCKETS
from agent_session_detective.wire import load_session
from tests.ir_helpers import IREventsTestCase
from tests.test_ir_dispatch import _clock, assistant_record, user_record

FIXTURES = Path(__file__).parent / "fixtures" / "ir"
ALL_FIXTURES = ["tier1.jsonl", "tier2.jsonl", "tier3.jsonl",
                "tier3-usage.jsonl", "compaction.jsonl",
                "reinject.jsonl", "attachments.jsonl", "dispatch.jsonl",
                "skill-identity.jsonl"]

ZERO_BUCKETS = dict.fromkeys(BUCKETS, 0)

CATALOG_V1 = "- demo: A demo skill"
CATALOG_V2 = "- demo: A demo skill with a longer blurb"
CATALOG_TWO = "- demo: A demo skill\n- ghost: A ghost skill"
BODY_V1 = '<skill_content name="demo">\nBody of demo skill v1\n</skill_content>'
BODY_V2 = '<skill_content name="demo">\nBody of demo skill v2\n</skill_content>'
BODY_V3 = '<skill_content name="demo">\nBody of demo skill v3\n</skill_content>'
READ_BODY = "Demo skill body via read."


def build(name):
    return build_audit_document(load_session(FIXTURES / name), "qoder")


def sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class ReinjectGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analyses = build_analyses(build("reinject.jsonl"))

    def rows(self):
        return self.analyses["context_organization"]["rows"]

    def test_rows_count_items_anchors_and_unattributed_shares(self):
        rows = self.rows()
        self.assertEqual([row["call_id"] for row in rows],
                         ["main:0", "main:1", "main:2"])
        self.assertEqual([row["n_items"] for row in rows], [2, 4, 6])
        self.assertEqual([row["anchor_tokens"] for row in rows],
                         [1000, 1100, 1200])
        self.assertEqual([row["unattributed_tokens"] for row in rows],
                         [993, 1082, 1171])
        self.assertEqual(
            [row["unattributed_share"] for row in rows],
            [round(993 / 1000, 4), round(1082 / 1100, 4),
             round(1171 / 1200, 4)],
        )
        self.assertEqual(self.analyses["context_organization"]["compactions"], [])

    def test_rows_split_new_and_re_injected_skill_tokens(self):
        rows = self.rows()
        self.assertEqual(
            [(row["new_tokens_est"], row["re_injected_tokens_est"])
             for row in rows],
            [(5, 0), (10, 5), (0, 25)],
        )
        self.assertEqual(rows[2]["buckets"],
                         dict(ZERO_BUCKETS, user=2, skill=25, assistant=2))

    def test_skill_audit_sees_three_injections_two_versions(self):
        (demo,) = self.analyses["skill_audit"]
        self.assertEqual(demo["skill_id"], "demo")
        self.assertEqual(demo["name"], "demo")
        self.assertEqual(
            demo["listing"],
            {
                "injections": 3,
                "tokens_est": 25,
                "repeat_tokens_est": 10,
                "distinct_versions": 2,
                "versions": [
                    {"sha1": sha(CATALOG_V1), "first_item_id": "main:2:0",
                     "injections": 1, "tokens_est": 5},
                    {"sha1": sha(CATALOG_V2), "first_item_id": "main:4:0",
                     "injections": 2, "tokens_est": 20},
                ],
            },
        )
        self.assertEqual(
            demo["bodies"],
            {"observations": 0, "tokens_est": 0, "channels": [], "sha1s": [],
             "version_drift": False, "without_execution": False},
        )
        self.assertEqual((demo["stubs"], demo["executions"]), (0, 0))
        self.assertIsNone(demo["stub_body_ratio"])

    def test_redundancy_flags_the_re_sent_catalog_only(self):
        result = self.analyses["redundancy"]
        self.assertEqual(
            result["duplicate_groups"],
            [{
                "norm_sha1": sha(CATALOG_V2),
                "item_ids": ["main:4:0", "main:6:0"],
                "buckets": ["skill"],
                "cross_bucket": False,
                "tokens_est": 20,
                "repeat_tokens_est": 10,
            }],
        )
        self.assertEqual(result["exact_resend_groups"], [])
        self.assertEqual(result["useless"], [])


class AttachmentsGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build("attachments.jsonl")
        cls.analyses = build_analyses(cls.document)

    def rows(self):
        return self.analyses["context_organization"]["rows"]

    def test_rows_track_live_items_anchors_and_shares(self):
        rows = self.rows()
        self.assertEqual(
            [(row["n_items"], row["anchor_tokens"], row["unattributed_tokens"])
             for row in rows],
            [(2, 1000, 988), (3, 1100, 1087), (7, 1200, 1177),
             (9, 1300, 1260), (11, 1400, 1343), (16, 1500, 1403),
             (18, 1600, 1494)],
        )
        self.assertEqual(
            [row["unattributed_share"] for row in rows],
            [round(988 / 1000, 4), round(1087 / 1100, 4),
             round(1177 / 1200, 4), round(1260 / 1300, 4),
             round(1343 / 1400, 4), round(1403 / 1500, 4),
             round(1494 / 1600, 4)],
        )
        self.assertEqual(rows[0]["buckets"],
                         dict(ZERO_BUCKETS, user=2, skill=10))

    def test_rows_split_new_and_re_injected_inject_and_skill_tokens(self):
        rows = self.rows()
        self.assertEqual(
            [(row["new_tokens_est"], row["re_injected_tokens_est"])
             for row in rows],
            [(10, 0), (0, 10), (0, 10), (16, 10), (16, 26), (24, 42),
             (5, 66)],
        )
        self.assertEqual(
            rows[5]["buckets"],
            dict(ZERO_BUCKETS, user=2, skill=42, assistant=18, tool=11,
                 inject=24),
        )
        self.assertEqual(
            rows[6]["buckets"],
            dict(ZERO_BUCKETS, user=2, skill=47, assistant=22, tool=11,
                 inject=24),
        )

    def test_skill_audit_body_channels_stub_and_execution(self):
        demo, ghost = self.analyses["skill_audit"]
        self.assertEqual((demo["skill_id"], ghost["skill_id"]), ("demo", "ghost"))
        expected_listing = {
            "injections": 1,
            "tokens_est": 5,
            "repeat_tokens_est": 0,
            "distinct_versions": 1,
            "versions": [{"sha1": sha(CATALOG_TWO),
                          "first_item_id": "main:2:0",
                          "injections": 1, "tokens_est": 5}],
        }
        self.assertEqual(demo["listing"], expected_listing)
        self.assertEqual(ghost["listing"], expected_listing)
        self.assertEqual(
            demo["bodies"],
            {
                "observations": 4,
                "tokens_est": 54,
                "channels": ["hook_output", "invoked_skills", "tool:read",
                             "user_signature"],
                "sha1s": sorted([sha(BODY_V1), sha(BODY_V2), sha(BODY_V3),
                                 sha(READ_BODY)]),
                "version_drift": True,
                "without_execution": True,
            },
        )
        self.assertEqual((demo["stubs"], demo["executions"]), (0, 0))
        self.assertEqual(demo["stub_body_ratio"], round(0 / 4, 4))
        self.assertEqual(
            ghost["bodies"],
            {"observations": 0, "tokens_est": 0, "channels": [], "sha1s": [],
             "version_drift": False, "without_execution": False},
        )
        self.assertEqual((ghost["stubs"], ghost["executions"]), (1, 1))
        self.assertEqual(ghost["stub_body_ratio"], round(1 / 1, 4))

    def test_duplicate_groups_see_cross_bucket_reuse(self):
        self.assertEqual(
            self.analyses["redundancy"]["duplicate_groups"],
            [{
                "norm_sha1": sha("hello world"),
                "item_ids": ["main:5:0", "main:13:0"],
                "buckets": ["inject", "tool"],
                "cross_bucket": True,
                "tokens_est": 4,
                "repeat_tokens_est": 2,
            }],
        )

    def test_useless_flags_unpaired_calls_and_unexecuted_bodies(self):
        result = self.analyses["redundancy"]
        self.assertEqual(result["exact_resend_groups"], [])
        self.assertEqual(
            result["useless"],
            [
                {"item_id": "main:6:0", "reason": "tool_result_without_call",
                 "inferred": True},
                {"item_id": "main:8:0",
                 "reason": "skill_body_without_execution", "inferred": True},
                {"item_id": "main:10:0",
                 "reason": "skill_body_without_execution", "inferred": True},
                {"item_id": "main:12:0",
                 "reason": "skill_body_without_execution", "inferred": True},
                {"item_id": "main:17:0", "reason": "tool_call_without_result",
                 "inferred": True},
                {"item_id": "main:18:0",
                 "reason": "skill_body_without_execution", "inferred": True},
            ],
        )


class CrossFixtureTest(unittest.TestCase):
    def test_every_fixture_yields_the_three_analyses(self):
        self.assertIs(ir.build_analyses, build_analyses)
        for name in ALL_FIXTURES:
            with self.subTest(fixture=name):
                document = build(name)
                analyses = build_analyses(document)
                self.assertEqual(
                    sorted(analyses),
                    ["context_organization", "dispatches", "redundancy",
                     "skill_audit", "skill_loads", "skill_tree"],
                )
                items_by_id = {item.item_id: item for item in document.items}
                rows = analyses["context_organization"]["rows"]
                self.assertEqual(
                    {row["call_id"] for row in rows},
                    {call.call_id for call in document.requests},
                )
                for row in rows:
                    self.assertEqual(set(row["buckets"]), set(BUCKETS))
                    self.assertLessEqual(
                        row["new_tokens_est"]
                        + row["re_injected_tokens_est"],
                        row["buckets"].get("inject", 0)
                        + row["buckets"].get("skill", 0),
                    )
                audits = analyses["skill_audit"]
                self.assertEqual(
                    [audit["skill_id"] for audit in audits],
                    [skill.skill_id for skill in document.skills],
                )
                for audit in audits:
                    listing = audit["listing"]
                    self.assertEqual(
                        listing["injections"],
                        sum(v["injections"] for v in listing["versions"]),
                    )
                    self.assertEqual(
                        listing["tokens_est"],
                        sum(v["tokens_est"] for v in listing["versions"]),
                    )
                    for version in listing["versions"]:
                        self.assertIn(version["first_item_id"], items_by_id)
                for compaction in analyses["context_organization"]["compactions"]:
                    for item_id in (compaction["killed_item_ids"]
                                    + compaction["restored_item_ids"]):
                        self.assertIn(item_id, items_by_id)

    def test_tier1_re_sends_are_found_via_request_hash(self):
        document = build("tier1.jsonl")
        analyses = build_analyses(document)
        self.assertEqual(
            analyses["redundancy"]["exact_resend_groups"],
            [{
                "agent_id": "main",
                "request_hash": "e1e2e3e4f1f2f3f4",
                "call_ids": ["main:1", "main:2"],
            }],
        )
        self.assertEqual(analyses["redundancy"]["duplicate_groups"], [])
        self.assertEqual(analyses["redundancy"]["useless"], [])
        self.assertEqual(
            [skill["skill_id"] for skill in analyses["skill_audit"]],
            ["demo", "other"],
        )

    def test_tier2_has_no_request_level_redundancy(self):
        document = build("tier2.jsonl")
        self.assertTrue(all(call.input.request_hash is None
                            for call in document.requests))
        self.assertEqual(
            build_analyses(document)["redundancy"],
            {"duplicate_groups": [], "exact_resend_groups": [],
             "useless": []},
        )

    def test_tier3_degrades_to_item_level_flags(self):
        analyses = build_analyses(build("tier3.jsonl"))
        self.assertEqual(analyses["skill_audit"], [])
        self.assertEqual(analyses["context_organization"]["rows"], [])
        self.assertEqual(
            analyses["redundancy"]["useless"],
            [{"item_id": "main:3:0", "reason": "tool_call_without_result",
              "inferred": True}],
        )

    def test_compaction_kills_restores_and_flags_short_lived(self):
        document = build("compaction.jsonl")
        analyses = build_analyses(document)
        rows = analyses["context_organization"]["rows"]
        self.assertEqual(
            [(row["anchor_tokens"], row["unattributed_tokens"],
              row["n_items"], row["new_tokens_est"],
              row["re_injected_tokens_est"]) for row in rows],
            [(500, 499, 1, 0, 0), (300, 291, 2, 9, 0)],
        )
        self.assertEqual(
            [row["unattributed_share"] for row in rows],
            [round(499 / 500, 4), round(291 / 300, 4)],
        )
        self.assertEqual(
            analyses["context_organization"]["compactions"],
            [{
                "compaction_id": "main:compact:0",
                "agent_id": "main",
                "boundary_seq": 3,
                "trigger": "auto",
                "pre_tokens": 900,
                "post_tokens": 90,
                "killed_item_ids": ["main:1:0", "main:2:0"],
                "restored_item_ids": ["main:5:0"],
            }],
        )
        self.assertEqual(
            analyses["redundancy"],
            {"duplicate_groups": [], "exact_resend_groups": [],
             "useless": [{"item_id": "main:1:0",
                          "reason": "short_lived_before_compaction",
                          "inferred": True}]},
        )


def result_record(ts, uuid, tool_id, content, parent=None):
    rec = {"type": "user", "uuid": uuid, "parentUuid": parent,
           "isSidechain": False, "timestamp": ts,
           "message": {"content": [
               {"type": "tool_result", "tool_use_id": tool_id,
                "content": content}]}}
    return rec


class SubagentReturnsTest(IREventsTestCase, unittest.TestCase):
    def audit(self, main_lines, subagents=None):
        document = build_audit_document(
            self.load_lines(main_lines, subagents=subagents), "qoder")
        return subagent_returns(document)

    def test_full_join_return_cost_echo_and_ratio(self):
        clock = _clock()
        main = [
            user_record(clock(), "m1", "please investigate the bug"),
            assistant_record(
                clock(), "m2",
                tool_use=("call_00A", "Agent",
                          {"subagent_type": "code",
                           "description": "spawned worker"}),
                request_id="req-1", parent="m1"),
            result_record(clock(), "m3", "call_00A", "A" * 8004,
                          parent="m2"),
            assistant_record(clock(), "m4", "done", request_id="req-2"),
        ]
        worker = [
            user_record(clock(), "w1", "alpha-flow investigate the bug",
                        parent_tool="call_00A"),
            assistant_record(
                clock(), "w2",
                tool_use=("wread", "Read", {"file_path": "/tmp/bug.txt"}),
                request_id="req-w1"),
            result_record(clock(), "w3", "wread", "A" * 8004, parent="w2"),
            assistant_record(clock(), "w4", "all clear", request_id="req-w2"),
        ]
        document = build_audit_document(
            self.load_lines(main, subagents={"worker": worker}), "qoder")
        ledger = subagent_returns(document)

        self.assertEqual(len(ledger["rows"]), 1)
        row = ledger["rows"][0]
        self.assertEqual(row["dispatch_id"], "main:dispatch:1")
        self.assertEqual(row["subagent_agent_id"], "subagent:worker")
        self.assertEqual(row["description"], "spawned worker")
        self.assertEqual(row["return_tokens_est"], 2001)
        sub_total = sum(item.tokens_est for item in document.items
                        if item.agent_id == "subagent:worker")
        self.assertEqual(row["subagent_tokens_est"], sub_total)
        echo_item = next(item for item in document.items
                         if item.agent_id == "subagent:worker"
                         and item.kind == "tool_result")
        self.assertEqual(row["echo_item_id"], echo_item.item_id)
        self.assertEqual(row["ratio"], round(2001 / sub_total, 4))
        self.assertEqual(row["flagged_reasons"], ["size", "echo"])
        self.assertEqual(ledger["totals"], {
            "dispatches": 1, "linked": 1, "return_tokens_total": 2001,
            "flagged": 1, "process_unavailable": 0})

    def test_orphan_dispatches_have_process_unavailable_and_no_ratio(self):
        clock = _clock()
        main = [
            user_record(clock(), "m1", "go"),
            assistant_record(
                clock(), "m2",
                tool_use=("call_00C", "Agent",
                          {"subagent_type": "code", "description": "one"}),
                request_id="req-1", parent="m1"),
            result_record(clock(), "m3", "call_00C", "A" * 8004,
                          parent="m2"),
            assistant_record(
                clock(), "m4",
                tool_use=("call_00D", "Agent",
                          {"subagent_type": "code", "description": "two"}),
                request_id="req-2", parent="m3"),
            result_record(clock(), "m5", "call_00D", "ok", parent="m4"),
            assistant_record(clock(), "m6", "done", request_id="req-3"),
        ]
        ledger = self.audit(main)

        self.assertEqual([r["dispatch_id"] for r in ledger["rows"]],
                         ["main:dispatch:1", "main:dispatch:2"])
        first, second = ledger["rows"]
        self.assertIsNone(first["subagent_agent_id"])
        self.assertEqual(first["return_tokens_est"], 2001)
        self.assertIsNone(first["subagent_tokens_est"])
        self.assertIsNone(first["ratio"])
        self.assertEqual(first["flagged_reasons"], ["size"])
        self.assertEqual(second["return_tokens_est"], 1)
        self.assertEqual(second["flagged_reasons"], [])
        self.assertEqual(ledger["totals"], {
            "dispatches": 2, "linked": 0, "return_tokens_total": 2002,
            "flagged": 1, "process_unavailable": 2})

    def test_echo_only_matches_the_linked_subagent(self):
        clock = _clock()
        main = [
            user_record(clock(), "m1", "please investigate"),
            assistant_record(
                clock(), "m2",
                tool_use=("call_00A", "Agent",
                          {"subagent_type": "code",
                           "description": "spawned joined"}),
                request_id="req-1", parent="m1"),
            result_record(clock(), "m3", "call_00A", "A" * 8004,
                          parent="m2"),
            assistant_record(clock(), "m4", "done", request_id="req-2"),
        ]
        subagents = {
            "joined": [
                user_record(clock(), "j1", "small brief",
                            parent_tool="call_00A"),
                assistant_record(clock(), "j2", "small reply",
                                 request_id="req-j1"),
            ],
            # claims a nonexistent parent call: its items exist in the
            # document but no dispatch row points at it
            "bystander": [
                user_record(clock(), "b1", "A" * 8004,
                            parent_tool="call_missing"),
            ],
        }
        document = build_audit_document(
            self.load_lines(main, subagents=subagents), "qoder")
        ledger = subagent_returns(document)

        self.assertEqual(len(ledger["rows"]), 1)
        row = ledger["rows"][0]
        self.assertEqual(row["subagent_agent_id"], "subagent:joined")
        self.assertIsNone(row["echo_item_id"])
        self.assertEqual(row["flagged_reasons"], ["size"])
        self.assertEqual(ledger["totals"]["process_unavailable"], 0)

    def test_bloat_threshold_is_inclusive(self):
        clock = _clock()
        main = [
            user_record(clock(), "m1", "go"),
            assistant_record(
                clock(), "m2",
                tool_use=("call_00C", "Agent",
                          {"subagent_type": "code", "description": "one"}),
                request_id="req-1", parent="m1"),
            # "A" * 8000 → estimate_tokens = 2000 → at threshold → flagged
            result_record(clock(), "m3", "call_00C", "A" * 8000,
                          parent="m2"),
            assistant_record(
                clock(), "m4",
                tool_use=("call_00D", "Agent",
                          {"subagent_type": "code", "description": "two"}),
                request_id="req-2", parent="m3"),
            # "A" * 7996 → 1999 → below threshold → not flagged
            result_record(clock(), "m5", "call_00D", "A" * 7996,
                          parent="m4"),
        ]
        ledger = self.audit(main)

        self.assertEqual(ledger["rows"][0]["flagged_reasons"], ["size"])
        self.assertEqual(ledger["rows"][1]["flagged_reasons"], [])

    def test_session_without_dispatches_returns_empty_ledger(self):
        ledger = self.audit([user_record(_clock()(), "m1", "hello")])

        self.assertEqual(ledger["rows"], [])
        self.assertEqual(ledger["totals"], {
            "dispatches": 0, "linked": 0, "return_tokens_total": 0,
            "flagged": 0, "process_unavailable": 0})


if __name__ == "__main__":
    unittest.main()
