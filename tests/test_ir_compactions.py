import unittest

from agent_session_detective.ir.compactions import build_compactions
from agent_session_detective.ir.items import extract_items
from agent_session_detective.ir.records import attribute_agents, group_records
from tests.ir_helpers import IREventsTestCase


def user(seq, text, compact_summary=False):
    record = {
        "type": "user",
        "uuid": "u%d" % seq,
        "timestamp": "2026-10-07T08:00:%02d.000Z" % seq,
        "message": {"content": [{"type": "text", "text": text}]},
    }
    if compact_summary:
        record["isCompactSummary"] = True
    return record


def assistant(seq, text, usage=None):
    record = {
        "type": "assistant",
        "uuid": "a%d" % seq,
        "timestamp": "2026-10-07T08:00:%02d.000Z" % seq,
        "message": {"content": [{"type": "text", "text": text}]},
    }
    if usage is not None:
        record["message"]["usage"] = usage
    return record


def boundary(seq, **metadata):
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "timestamp": "2026-10-07T08:00:%02d.000Z" % seq,
        "compactMetadata": metadata,
    }


def restore(seq, files):
    return {
        "type": "attachment",
        "timestamp": "2026-10-07T08:00:%02d.000Z" % seq,
        "attachment": {"type": "post_compact_restored_files", "files": files},
    }


USAGE = {"input_tokens": 500, "output_tokens": 20}


class CompactionsTest(IREventsTestCase):
    def build(self, lines):
        records = group_records(self.load_lines(lines).events)
        agents = attribute_agents(records)
        extraction = extract_items(agents[0].records, "main")
        compactions = build_compactions(agents, extraction.items)
        return extraction.items, compactions

    def test_boundary_metadata_and_restored_items(self):
        items, compactions = self.build(
            [
                user(1, "start"),
                assistant(2, "working", usage=USAGE),
                boundary(3, trigger="auto", preTokens=900, postTokens=90,
                         messagesSummarized=12, durationMs=2500),
                user(4, "summary of prior work", compact_summary=True),
                restore(5, [{"filePath": "/tmp/file-a.md",
                             "content": "restored: file-a.md"}]),
                assistant(6, "continuing", usage={"input_tokens": 300}),
            ]
        )
        self.assertEqual(len(compactions), 1)
        compaction = compactions[0]
        self.assertEqual(compaction.compaction_id, "main:compact:0")
        self.assertEqual(compaction.agent_id, "main")
        self.assertEqual(compaction.boundary_seq, 3)
        self.assertIsNotNone(compaction.ts)
        self.assertEqual(compaction.trigger, "auto")
        self.assertEqual(compaction.pre_tokens, 900)
        self.assertEqual(compaction.post_tokens, 90)
        self.assertEqual(compaction.messages_summarized, 12)
        self.assertEqual(compaction.duration_ms, 2500)
        self.assertEqual(compaction.restored_item_ids, ["main:5:0"])

    def test_pre_boundary_items_die_at_the_boundary(self):
        items, compactions = self.build(
            [
                user(1, "start"),
                assistant(2, "working", usage=USAGE),
                boundary(3, preTokens=900, postTokens=90),
                user(4, "summary"),
                assistant(5, "continuing", usage={"input_tokens": 300}),
            ]
        )
        gone = {item.item_id: item.gone_seq for item in items}
        self.assertEqual(gone["main:1:0"], 3)
        self.assertEqual(gone["main:2:0"], 3)
        self.assertIsNone(gone["main:4:0"])
        self.assertEqual(compactions[0].restored_item_ids, [])

    def test_second_boundary_only_kills_items_still_alive(self):
        items, compactions = self.build(
            [
                user(1, "start"),
                assistant(2, "working", usage=USAGE),
                boundary(3),
                user(4, "summary"),
                assistant(5, "continuing", usage=USAGE),
                boundary(6),
                user(7, "summary two"),
                assistant(8, "continuing again", usage=USAGE),
            ]
        )
        self.assertEqual([compaction.compaction_id for compaction in compactions],
                         ["main:compact:0", "main:compact:1"])
        gone = {item.item_id: item.gone_seq for item in items}
        self.assertEqual(gone["main:1:0"], 3)
        self.assertEqual(gone["main:2:0"], 3)
        self.assertEqual(gone["main:4:0"], 6)
        self.assertEqual(gone["main:5:0"], 6)
        self.assertIsNone(gone["main:7:0"])

    def test_restored_items_outside_the_restore_window_are_not_listed(self):
        items, compactions = self.build(
            [
                user(1, "start"),
                assistant(2, "working", usage=USAGE),
                boundary(3),
                restore(4, [{"filePath": "/tmp/a.md", "content": "restored: a"}]),
                assistant(5, "continuing", usage=USAGE),
                restore(6, [{"filePath": "/tmp/b.md", "content": "restored: b"}]),
            ]
        )
        self.assertEqual(compactions[0].restored_item_ids, ["main:4:0"])

    def test_boundaries_are_scoped_per_agent(self):
        records = group_records(self.load_lines(
            [
                user(1, "start"),
                assistant(2, "working", usage=USAGE),
                boundary(3),
                restore(4, [{"filePath": "/tmp/m.md", "content": "restored: m"}]),
                assistant(5, "continuing", usage=USAGE),
            ],
            subagents={
                "worker": [
                    user(1, "worker start"),
                    boundary(2),
                    user(3, "worker after"),
                    user(4, "worker note"),
                    assistant(5, "worker working", usage=USAGE),
                ]
            },
        ).events)
        agents = attribute_agents(records)
        items = []
        for agent in agents:
            items.extend(extract_items(agent.records, agent.agent_id).items)
        compactions = build_compactions(agents, items)
        gone = {item.item_id: item.gone_seq for item in items}
        self.assertEqual(gone["main:1:0"], 3)
        self.assertEqual(gone["main:2:0"], 3)
        self.assertIsNone(gone["main:4:0"])
        self.assertEqual(gone["subagent:worker:1:0"], 2)
        self.assertIsNone(gone["subagent:worker:3:0"])
        by_agent = {compaction.agent_id: compaction for compaction in compactions}
        self.assertEqual(len(compactions), 2)
        self.assertEqual(by_agent["main"].restored_item_ids, ["main:4:0"])
        self.assertEqual(by_agent["subagent:worker"].restored_item_ids, [])


if __name__ == "__main__":
    unittest.main()
