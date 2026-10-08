import unittest

from agent_session_detective.ir.schema import (
    ContentItem,
    LLMCall,
    RequestInput,
    RequestOutput,
    Span,
)
from agent_session_detective.ir.skills import build_skills

SHA_A = "a" * 64


def item(item_id, agent_id, bucket, kind, channel, seq, skill_id=None, sha1=SHA_A):
    return ContentItem(
        item_id=item_id,
        agent_id=agent_id,
        bucket=bucket,
        kind=kind,
        name=None,
        channel=channel,
        wire_seq=seq,
        gone_seq=None,
        size_chars=10,
        tokens_est=5,
        sha1=sha1,
        norm_sha1=sha1,
        record={"file": "f.jsonl", "seq": seq, "uuid": "u%d" % seq},
        preview="x" * 10,
        skill_id=skill_id,
    )


def call(call_id, agent_id, first_seq, last_seq):
    return LLMCall(
        call_id=call_id,
        agent_id=agent_id,
        ts=100.0,
        model="m",
        span=Span(first_seq=first_seq, last_seq=last_seq, n_records=1),
        identity_tier=1,
        input=RequestInput(
            item_refs=[],
            buckets={},
            anchor_tokens=10,
            unattributed_tokens=None,
            cache_read=0,
            cache_write_5m=0,
            cache_write_1h=0,
            context_usage_ratio=None,
            request_id=None,
            request_hash=None,
            response_hash=None,
            credits=None,
            prefix_hashes=None,
        ),
        output=RequestOutput(parts=[], output_tokens=0),
    )


class SkillsTest(unittest.TestCase):
    def test_listing_lines_expand_into_one_observation_per_skill(self):
        items = [
            item("main:2:0", "main", "inject", "skill_catalog",
                 "qoder:attachment:skill_catalog", 2),
        ]
        listing = {"main:2:0": [("demo", "demo", 5), ("other", "other", 7)]}
        skills = build_skills(items, [call("main:0", "main", 3, 5)],
                              {("f.jsonl", 2): 100.0}, listing)
        self.assertEqual([entity.skill_id for entity in skills], ["demo", "other"])
        self.assertEqual(skills[0].name, "demo")
        self.assertEqual(len(skills[0].observations), 1)
        obs = skills[0].observations[0]
        self.assertEqual((obs.kind, obs.channel, obs.tokens_est),
                         ("listing", "skill_listing", 5))
        self.assertEqual(obs.call_id, "main:0")
        self.assertEqual(obs.ts, 100.0)
        self.assertIsNone(obs.body_sha1)

    def test_body_stub_and_execution_observations_map_to_kinds_and_channels(self):
        items = [
            item("main:3:0", "main", "skill", "skill_body",
                 "qoder:attachment:hook_output", 3, skill_id="demo",
                 sha1="a" * 64),
            item("main:4:0", "main", "skill", "skill_body",
                 "qoder:signature:skill_body", 4, skill_id="demo",
                 sha1="b" * 64),
            item("main:5:0", "main", "tool", "tool_result",
                 "qoder:tool_result", 5, skill_id="demo", sha1="c" * 64),
            item("main:6:0", "main", "skill", "skill_stub",
                 "qoder:tool_result", 6, skill_id="ghost", sha1="d" * 64),
            item("main:7:0", "main", "assistant", "tool_call",
                 "qoder:tool_call", 7, skill_id="demo", sha1="e" * 64),
        ]
        seq_ts = {("f.jsonl", seq): 100.0 for seq in (3, 4, 5, 6, 7)}
        skills = build_skills(items, [call("main:0", "main", 3, 7)], seq_ts, {})
        self.assertEqual([entity.skill_id for entity in skills], ["demo", "ghost"])
        observed = [(obs.item_id, obs.kind, obs.channel, obs.body_sha1)
                    for obs in skills[0].observations]
        self.assertEqual(observed, [
            ("main:3:0", "body", "hook_output", "a" * 64),
            ("main:4:0", "body", "user_signature", "b" * 64),
            ("main:5:0", "body", "tool:read", "c" * 64),
            ("main:7:0", "execution", "skill_tool_call", None),
        ])
        ghost = skills[1].observations
        self.assertEqual([(obs.kind, obs.channel, obs.tokens_est) for obs in ghost],
                         [("stub", "tool_result", 5)])

    def test_call_linkage_inside_span_then_next_call_then_none(self):
        items = [
            item("main:4:0", "main", "skill", "skill_body",
                 "qoder:signature:skill_body", 4, skill_id="demo"),
            item("main:6:0", "main", "skill", "skill_body",
                 "qoder:signature:skill_body", 6, skill_id="demo"),
            item("main:9:0", "main", "skill", "skill_body",
                 "qoder:signature:skill_body", 9, skill_id="demo"),
            item("sub:5:0", "subagent:x", "skill", "skill_body",
                 "qoder:signature:skill_body", 5, skill_id="demo"),
        ]
        calls = [call("main:0", "main", 3, 4), call("main:1", "main", 7, 8),
                 call("sub:0", "subagent:x", 3, 4)]
        seq_ts = {("f.jsonl", seq): 100.0 for seq in (3, 4, 5, 6, 7, 8, 9)}
        skills = build_skills(items, calls, seq_ts, {})
        linked = {obs.item_id: obs.call_id for obs in skills[0].observations}
        self.assertEqual(linked, {
            "main:4:0": "main:0",
            "main:6:0": "main:1",
            "main:9:0": None,
            "sub:5:0": None,
        })

    def test_observations_sorted_by_timestamp_without_ts_last(self):
        items = [
            item("main:3:0", "main", "skill", "skill_body",
                 "qoder:signature:skill_body", 3, skill_id="demo"),
            item("main:4:0", "main", "skill", "skill_body",
                 "qoder:signature:skill_body", 4, skill_id="demo"),
            item("main:5:0", "main", "skill", "skill_body",
                 "qoder:signature:skill_body", 5, skill_id="demo"),
        ]
        seq_ts = {("f.jsonl", 3): 200.0, ("f.jsonl", 4): 100.0}
        skills = build_skills(items, [], seq_ts, {})
        self.assertEqual(skills[0].name, "demo")
        self.assertEqual([obs.item_id for obs in skills[0].observations],
                         ["main:4:0", "main:3:0", "main:5:0"])

    def test_item_display_name_used_when_no_listing_line(self):
        execution = item("main:3:0", "main", "assistant", "tool_call",
                         "qoder:assistant", 3, skill_id="demo")
        execution.name = "Skill"
        body = item("main:4:0", "main", "skill", "skill_body",
                    "qoder:attachment:hook_output", 4, skill_id="demo")
        body.name = "Demo"
        skills = build_skills([execution, body], [], {}, {})

        self.assertEqual([entity.skill_id for entity in skills], ["demo"])
        self.assertEqual(skills[0].name, "Demo")


if __name__ == "__main__":
    unittest.main()
