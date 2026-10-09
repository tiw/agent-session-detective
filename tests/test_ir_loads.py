"""Spec item 1 — the load ledger (``ir/loads.py``) unit tests.

These run against hand-built IR state (``ContentItem`` + ``SkillEntity``),
not transcripts: the ledger reads only projected IR state, so its pairing,
reload, redundant-body and wire-order rules are tested directly at that
boundary.
"""

import unittest

from agent_session_detective.ir.loads import build_loads
from agent_session_detective.ir.schema import (
    ContentItem,
    ContextAgent,
    Observation,
    SkillEntity,
)


def item(agent_id, seq, kind, skill_id, tokens=100, channel="hook_output",
         gone_seq=None):
    """A minimal ContentItem; only ledger-relevant fields are varied."""
    return ContentItem(
        item_id="%s:%d" % (agent_id, seq),
        agent_id=agent_id,
        bucket="skill",
        kind=kind,
        name=None,
        channel=channel,
        wire_seq=seq,
        gone_seq=gone_seq,
        size_chars=tokens * 4,
        tokens_est=tokens,
        sha1="sha-%s-%d" % (agent_id, seq),
        norm_sha1="norm-%s-%d" % (agent_id, seq),
        record={"file": "transcript.jsonl", "seq": seq},
        preview="item %s" % seq,
        skill_id=skill_id,
    )


def project(items, agents=("main",)):
    """Derive SkillEntity observations from items exactly as build_skills
    does for the kinds the ledger consumes (body, stub, tool:read body),
    then run build_loads."""
    skills = {}
    for it in items:
        if it.kind == "skill_body":
            obs = Observation(kind="body", channel=it.channel,
                              agent_id=it.agent_id, call_id=None,
                              ts=float(it.wire_seq), tokens_est=it.tokens_est,
                              body_sha1=it.sha1, item_id=it.item_id)
        elif it.kind == "tool_result" and it.skill_id is not None:
            obs = Observation(kind="body", channel="tool:read",
                              agent_id=it.agent_id, call_id=None,
                              ts=float(it.wire_seq), tokens_est=it.tokens_est,
                              body_sha1=it.sha1, item_id=it.item_id)
        elif it.kind == "skill_stub":
            obs = Observation(kind="stub", channel="tool_result",
                              agent_id=it.agent_id, call_id=None,
                              ts=float(it.wire_seq), tokens_est=it.tokens_est,
                              body_sha1=None, item_id=it.item_id)
        else:
            continue
        entity = skills.setdefault(
            it.skill_id,
            SkillEntity(skill_id=it.skill_id, name=it.skill_id, observations=[]),
        )
        entity.observations.append(obs)
    agent_out = [
        ContextAgent(agent_id=a, parent_id=None, origin_channel="main",
                     model=None, context_window=None, request_ids=[],
                     active_leaf=None)
        for a in agents
    ]
    return build_loads(list(skills.values()), items, agent_out)


class StubBodyPairingTest(unittest.TestCase):
    def test_stub_joins_to_the_first_body_of_its_skill(self):
        stub = item("main", 1, "skill_stub", "demo")
        body = item("main", 2, "skill_body", "demo", tokens=700)
        loads, redundant = project([stub, body])

        self.assertEqual(redundant, 0)
        self.assertEqual(len(loads), 1)
        load = loads[0]
        self.assertEqual(load.load_id, "main:load:1")
        self.assertEqual((load.kind, load.skill_id), ("load", "demo"))
        self.assertEqual(load.marker_item_id, "main:1")
        self.assertEqual(load.body_item_id, "main:2")
        self.assertEqual(load.body_sha1, body.sha1)
        # cost is the BODY item's tokens_est — never the stub's
        self.assertEqual((load.cost_tokens_est, load.cost_basis), (700, "body"))
        self.assertEqual(load.ts, 1.0)  # the stub's opening timestamp

    def test_first_body_wins_and_duplicate_bodies_stay_counted(self):
        stub = item("main", 1, "skill_stub", "demo")
        first = item("main", 2, "skill_body", "demo", tokens=700)
        duplicate = item("main", 3, "skill_body", "demo", tokens=700)
        loads, redundant = project([stub, first, duplicate])

        # one row for the stub+first-body pair; the duplicate flows through
        # rule 3 and is counted redundant (previous body still alive), no row
        self.assertEqual(len(loads), 1)
        self.assertEqual(loads[0].body_item_id, "main:2")
        self.assertEqual(redundant, 1)
        self.assertEqual([load.cost_tokens_est for load in loads], [700])

    def test_stub_without_body_is_unavailable_with_null_cost(self):
        stub = item("main", 1, "skill_stub", "ghost")
        loads, redundant = project([stub])

        self.assertEqual((redundant, len(loads)), (0, 1))
        load = loads[0]
        self.assertEqual(load.kind, "load")
        self.assertEqual(load.marker_item_id, "main:1")
        self.assertIsNone(load.body_item_id)
        self.assertIsNone(load.body_sha1)
        self.assertIsNone(load.channel)
        self.assertIsNone(load.cost_tokens_est)
        self.assertEqual(load.cost_basis, "unavailable")

    def test_body_only_first_load_has_no_marker(self):
        body = item("main", 1, "skill_body", "demo", tokens=500)
        loads, redundant = project([body])

        self.assertEqual((redundant, len(loads)), (0, 1))
        load = loads[0]
        self.assertEqual(load.kind, "load")
        self.assertIsNone(load.marker_item_id)
        self.assertEqual(load.body_item_id, "main:1")
        self.assertEqual((load.cost_tokens_est, load.cost_basis), (500, "body"))

    def test_reload_when_the_previous_body_was_compacted_away(self):
        first = item("main", 1, "skill_body", "demo", tokens=500)
        second = item("main", 4, "skill_body", "demo", tokens=500, gone_seq=None)
        gone = item("main", 1, "skill_body", "demo", tokens=500, gone_seq=3)
        loads, redundant = project([gone, second])

        self.assertEqual((redundant, len(loads)), (0, 2))
        self.assertEqual([load.kind for load in loads], ["load", "reload"])
        self.assertEqual([load.load_id for load in loads],
                         ["main:load:1", "main:load:2"])
        for load in loads:
            self.assertEqual((load.cost_tokens_est, load.cost_basis), (500, "body"))

    def test_redundant_body_with_alive_previous_gets_no_row(self):
        alive = item("main", 1, "skill_body", "demo", tokens=500)
        duplicate = item("main", 3, "skill_body", "demo", tokens=500)
        loads, redundant = project([alive, duplicate])

        self.assertEqual((redundant, len(loads)), (1, 1))
        self.assertEqual(loads[0].body_item_id, "main:1")

    def test_successor_stub_closes_an_unattached_stub_as_unavailable(self):
        first_stub = item("main", 1, "skill_stub", "demo")
        second_stub = item("main", 2, "skill_stub", "demo")
        body = item("main", 3, "skill_body", "demo", tokens=300)
        loads, redundant = project([first_stub, second_stub, body])

        self.assertEqual((redundant, len(loads)), (0, 2))
        unavailable = loads[0]
        self.assertEqual(
            (unavailable.load_id, unavailable.marker_item_id,
             unavailable.cost_tokens_est, unavailable.cost_basis),
            ("main:load:1", "main:1", None, "unavailable"),
        )
        paired = loads[1]
        self.assertEqual(
            (paired.load_id, paired.marker_item_id, paired.body_item_id),
            ("main:load:2", "main:2", "main:3"),
        )


class ChannelTest(unittest.TestCase):
    def test_channel_is_recorded_per_body_channel_including_tool_read(self):
        # Each body carries its own opening stub so each pair becomes a row;
        # the row's channel is the attached body's channel, whatever it is.
        stubs_and_bodies = [
            (item("main", 1, "skill_stub", "demo"),
             item("main", 2, "skill_body", "demo", channel="hook_output")),
            (item("main", 3, "skill_stub", "demo"),
             item("main", 4, "skill_body", "demo", channel="invoked_skills")),
            (item("main", 5, "skill_stub", "demo"),
             item("main", 6, "skill_body", "demo",
                  channel="qoder:signature:skill_body")),
            (item("main", 7, "skill_stub", "demo"),
             item("main", 8, "tool_result", "demo")),
        ]
        items = [entry for pair in stubs_and_bodies for entry in pair]
        loads, redundant = project(items)

        self.assertEqual((redundant, len(loads)), (0, 4))
        self.assertEqual(
            [load.channel for load in loads],
            ["hook_output", "invoked_skills", "qoder:signature:skill_body",
             "tool:read"],
        )
        self.assertEqual(
            [(load.marker_item_id, load.body_item_id) for load in loads],
            [("main:1", "main:2"), ("main:3", "main:4"),
             ("main:5", "main:6"), ("main:7", "main:8")],
        )
        for load in loads:
            self.assertEqual(load.cost_basis, "body")


class WireOrderTest(unittest.TestCase):
    def test_rows_stay_in_wire_order_when_an_unattached_stub_interleaves(self):
        # The orphan stub for "ghost" opens at seq 1 and only closes at walk
        # end; "demo"'s body at seq 2 must not jump ahead of it.
        ghost_stub = item("main", 1, "skill_stub", "ghost")
        demo_body = item("main", 2, "skill_body", "demo", tokens=400)
        loads, _ = project([ghost_stub, demo_body])

        self.assertEqual([load.skill_id for load in loads], ["ghost", "demo"])
        self.assertEqual([load.load_id for load in loads],
                         ["main:load:1", "main:load:2"])

    def test_rows_are_numbered_per_agent_in_wire_order(self):
        a_body = item("a", 1, "skill_body", "demo", tokens=100)
        b_body = item("b", 2, "skill_body", "demo", tokens=200)
        loads, _ = project([a_body, b_body], agents=("a", "b"))

        self.assertEqual(
            [(load.load_id, load.agent_id, load.cost_tokens_est) for load in loads],
            [("a:load:1", "a", 100), ("b:load:1", "b", 200)],
        )


if __name__ == "__main__":
    unittest.main()
