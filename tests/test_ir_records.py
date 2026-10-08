# tests/test_ir_records.py
import unittest

from agent_session_detective.ir.records import (
    WireRecord,
    attribute_agents,
    detect_tier,
    group_records,
)
from tests.ir_helpers import IREventsTestCase


class RecordsTest(IREventsTestCase):
    def test_group_records_restores_per_file_line_order(self):
        session = self.load_lines(
            [
                {"type": "user", "uuid": "u1", "timestamp": "2026-10-07T08:00:00.000Z",
                 "message": {"content": [{"type": "text", "text": "go"}]}},
                {"type": "assistant", "uuid": "a1", "timestamp": "2026-10-07T08:00:01.000Z",
                 "message": {"content": [{"type": "text", "text": "ok"}]}},
            ]
        )
        records = group_records(session.events)

        self.assertEqual([record.seq for record in records], [1, 2])
        self.assertEqual(records[0].kind, "user")
        self.assertEqual(records[1].kind, "assistant")
        self.assertTrue(all(record.ts is not None for record in records))

    def test_kind_classifies_tool_and_meta_records(self):
        session = self.load_lines(
            [
                {"type": "assistant", "uuid": "a1",
                 "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}},
                {"type": "user", "uuid": "r1",
                 "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}},
                {"type": "runtime-config", "model": "m", "contextWindow": 128000},
            ]
        )
        records = group_records(session.events)

        self.assertEqual([record.kind for record in records], ["assistant", "user", "meta"])

    def test_has_positive_usage_ignores_zero_usage(self):
        session = self.load_lines(
            [
                {"type": "assistant", "uuid": "a1",
                 "message": {"content": [{"type": "text", "text": "a"}],
                             "usage": {"input_tokens": 0, "output_tokens": 0,
                                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
                {"type": "assistant", "uuid": "a2",
                 "message": {"content": [{"type": "text", "text": "b"}],
                             "usage": {"input_tokens": 7, "output_tokens": 1}}},
            ]
        )
        records = group_records(session.events)

        self.assertFalse(records[0].has_positive_usage)
        self.assertTrue(records[1].has_positive_usage)

    def test_attribute_agents_walks_sidechain_chain_to_root(self):
        session = self.load_lines(
            [
                {"type": "user", "uuid": "u1", "timestamp": "2026-10-07T08:00:00.000Z",
                 "message": {"content": [{"type": "text", "text": "go"}]}},
                {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "isSidechain": True,
                 "timestamp": "2026-10-07T08:00:01.000Z",
                 "message": {"content": [{"type": "text", "text": "side1"}]}},
                {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "isSidechain": True,
                 "timestamp": "2026-10-07T08:00:02.000Z",
                 "message": {"content": [{"type": "text", "text": "side2"}]}},
            ]
        )
        groups = attribute_agents(group_records(session.events))

        self.assertEqual([group.agent_id for group in groups], ["main", "sidechain:a1"])
        self.assertEqual(groups[0].origin_channel, "qoder:main")
        self.assertEqual(groups[1].origin_channel, "qoder:isSidechain")
        self.assertEqual(groups[1].parent_id, "main")
        self.assertEqual([record.seq for record in groups[1].records], [2, 3])
        self.assertEqual(groups[1].records[0].uuid, "a1")

    def test_attribute_agents_assigns_subagent_origin(self):
        session = self.load_lines(
            [
                {"type": "user", "uuid": "u1", "timestamp": "2026-10-07T08:00:00.000Z",
                 "message": {"content": [{"type": "text", "text": "go"}]}},
            ],
            subagents={
                "abc123": [
                    {"type": "assistant", "uuid": "s1", "timestamp": "2026-10-07T08:00:05.000Z",
                     "message": {"content": [{"type": "text", "text": "sub work"}]}},
                ]
            },
        )
        groups = attribute_agents(group_records(session.events))

        self.assertEqual([group.agent_id for group in groups], ["main", "subagent:abc123"])
        self.assertEqual(groups[1].origin_channel, "qoder:subagent-file")
        self.assertEqual(groups[1].parent_id, "main")
        self.assertEqual(groups[1].records[0].preview_text, "sub work")

    def test_detect_tier_over_three_shapes(self):
        shapes = [
            ([{"type": "assistant", "uuid": "a1",
               "requestTokenAnchor": {"request": "r", "response": "s", "requestId": "req-1"},
               "message": {"content": [{"type": "text", "text": "x"}]}}], 1),
            ([{"type": "assistant", "uuid": "a1",
               "message": {"content": [{"type": "text", "text": "x"}],
                           "usage": {"input_tokens": 7, "request_id": "req-1"}}}], 2),
            ([{"type": "assistant", "uuid": "a1",
               "message": {"content": [{"type": "text", "text": "x"}],
                           "usage": {"input_tokens": 7}}}], 3),
        ]
        for lines, expected in shapes:
            session = self.load_lines(lines)
            self.assertEqual(detect_tier(group_records(session.events)), expected)

    def test_group_records_restores_order_when_timestamps_run_backwards(self):
        session = self.load_lines(
            [
                {"type": "user", "uuid": "u1", "timestamp": "2026-10-07T08:00:05.000Z",
                 "message": {"content": [{"type": "text", "text": "first"}]}},
                {"type": "assistant", "uuid": "a1", "timestamp": "2026-10-07T08:00:00.000Z",
                 "message": {"content": [{"type": "text", "text": "second"}]}},
            ]
        )
        records = group_records(session.events)

        self.assertEqual([record.seq for record in records], [1, 2])
        # user text lives in the TurnBegin payload, not a ContentPart, so
        # preview_text is empty for u1; check record identity order instead.
        self.assertEqual([record.uuid for record in records], ["u1", "a1"])

    def test_sidechain_parent_cycle_terminates(self):
        session = self.load_lines(
            [
                {"type": "assistant", "uuid": "a1", "parentUuid": "a2", "isSidechain": True,
                 "message": {"content": [{"type": "text", "text": "cyc1"}]}},
                {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "isSidechain": True,
                 "message": {"content": [{"type": "text", "text": "cyc2"}]}},
            ]
        )
        groups = attribute_agents(group_records(session.events))

        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0].agent_id.startswith("sidechain:"))


if __name__ == "__main__":
    unittest.main()
