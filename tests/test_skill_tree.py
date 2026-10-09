"""Golden tests for the actual skill tree analysis (D2)."""

import unittest

from agent_session_detective.ir.schema import (
    AuditDocument, ContextAgent, CoverageReport, Dispatch, LoadEvidence,
    Observation, SkillEntity)
from agent_session_detective.ir.skill_tree import skill_tree

MAIN = "main"
CHILD = "subagent:agent-apstack:poteto-agent-241caed765ad6b60"


def agent(agent_id, request_ids=("main:0",), model="qoder-pro",
          context_window=200000):
    return ContextAgent(agent_id=agent_id, parent_id=None,
                        origin_channel="qoder", model=model,
                        context_window=context_window,
                        request_ids=list(request_ids), active_leaf=None)


def observation(kind, agent_id=MAIN, *, ts=None, channel="tool:read",
                tokens=10, sha1=None, item_id="i0", raw_skill_id=None):
    return Observation(kind=kind, channel=channel, agent_id=agent_id,
                       call_id=None, ts=ts, tokens_est=tokens,
                       body_sha1=sha1, item_id=item_id,
                       raw_skill_id=raw_skill_id)


def skill(skill_id, observations, name=None, aliases=()):
    return SkillEntity(skill_id=skill_id, name=name or skill_id,
                       observations=list(observations),
                       aliases=list(aliases))


def load(load_id, agent_id, skill_id, *, kind="load", channel="tool:read",
         cost=100, basis="body", sha1=None):
    return LoadEvidence(load_id=load_id, agent_id=agent_id, skill_id=skill_id,
                        kind=kind, marker_item_id=None, body_item_id=None,
                        body_sha1=sha1, channel=channel, ts=1000.0,
                        cost_tokens_est=cost, cost_basis=basis)


def dispatch(dispatch_id, parent, child, subagent_type,
             description="do a thing", brief=50):
    return Dispatch(dispatch_id=dispatch_id, parent_agent_id=parent,
                    tool_use_id="t1", tool_item_id="i1",
                    subagent_agent_id=child, brief_item_id=None,
                    brief_tokens_est=brief, subagent_type=subagent_type,
                    description=description, meta=None, phase_refs=[],
                    phase_id=None)


def document(agents, skills=(), loads=(), dispatches=()):
    return AuditDocument(
        adapter={"id": "test", "version": "test"},
        estimator_version="test",
        source_files=["test.jsonl"],
        agents=list(agents),
        requests=[],
        items=[],
        skills=list(skills),
        compactions=[],
        coverage=CoverageReport(
            per_field={}, requests_with_anchor="0/0", request_identity={},
            bucket_sources={}, unknown_channels=[], dropped_records={},
            notes=[]),
        skill_loads=list(loads),
        dispatches=list(dispatches),
    )


def node_by_id(tree, agent_id):
    return next(node for node in tree["nodes"] if node["agent_id"] == agent_id)


class TreeShapeTest(unittest.TestCase):
    def test_nodes_edges_and_labels(self):
        tree = skill_tree(document(
            agents=[agent(MAIN, request_ids=("main:0", "main:1")),
                    agent(CHILD, request_ids=("subagent:...:0",))],
            dispatches=[dispatch("main:dispatch:1", MAIN, CHILD,
                                 "pstack:poteto-agent")],
        ))
        child = node_by_id(tree, CHILD)
        self.assertEqual(child["label"], "pstack:poteto-agent · 241caed7")
        self.assertEqual(child["parent_agent_id"], MAIN)
        self.assertEqual(child["via_dispatch_id"], "main:dispatch:1")
        self.assertEqual(child["n_requests"], 1)
        main = node_by_id(tree, MAIN)
        self.assertEqual(main["label"], "main")
        self.assertEqual(main["n_requests"], 2)
        self.assertEqual(tree["loose"], [])
        self.assertEqual(tree["edges"], [{
            "dispatch_id": "main:dispatch:1",
            "from_agent_id": MAIN,
            "to_agent_id": CHILD,
            "subagent_type": "pstack:poteto-agent",
            "description": "do a thing",
            "brief_tokens_est": 50,
            "phase_id": None,
        }])
        self.assertEqual(tree["totals"]["agents"], 2)
        self.assertEqual(tree["totals"]["edges"], 1)

    def test_label_falls_back_to_agent_id_without_dispatch_type(self):
        unknown = "subagent:agent-x-abcdef1234567890"
        tree = skill_tree(document(agents=[agent(MAIN), agent(unknown)]))
        self.assertEqual(node_by_id(tree, unknown)["label"], unknown)

    def test_orphan_dispatch_and_loose_agent(self):
        tree = skill_tree(document(
            agents=[agent(MAIN), agent(CHILD)],
            dispatches=[dispatch("main:dispatch:1", MAIN, None,
                                 "pstack:poteto-agent")],
        ))
        self.assertEqual(len(tree["edges"]), 1)
        self.assertIsNone(tree["edges"][0]["to_agent_id"])
        self.assertEqual(tree["totals"]["edges"], 0)
        self.assertEqual(tree["loose"], [{
            "agent_id": CHILD,
            "label": CHILD,
            "reason": "no incoming dispatch edge",
        }])

    def test_document_without_main_has_no_loose(self):
        other = "subagent:agent-a-1111222233334444"
        tree = skill_tree(document(agents=[agent(other), agent(CHILD)]))
        self.assertEqual(tree["loose"], [])

    def test_nested_dispatch_chain(self):
        mid = "subagent:agent-a-aaaa111122223333"
        leaf = "subagent:agent-b-bbbb444455556666"
        tree = skill_tree(document(
            agents=[agent(MAIN), agent(mid), agent(leaf)],
            dispatches=[
                dispatch("main:dispatch:1", MAIN, mid, "agent-a"),
                dispatch("mid:dispatch:1", mid, leaf, "agent-b"),
            ],
        ))
        middle = node_by_id(tree, mid)
        self.assertEqual(middle["parent_agent_id"], MAIN)
        self.assertEqual(middle["via_dispatch_id"], "main:dispatch:1")
        self.assertEqual(middle["label"], "agent-a · aaaa1111")
        child = node_by_id(tree, leaf)
        self.assertEqual(child["parent_agent_id"], mid)
        self.assertEqual(child["via_dispatch_id"], "mid:dispatch:1")
        self.assertEqual(child["label"], "agent-b · bbbb4444")
        self.assertEqual(tree["loose"], [])
        self.assertEqual(tree["totals"]["edges"], 2)

    def test_duplicate_dispatch_first_wins_and_main_is_root(self):
        tree = skill_tree(document(
            agents=[agent(MAIN), agent(CHILD)],
            dispatches=[
                dispatch("main:dispatch:1", MAIN, CHILD, "pstack:poteto-agent"),
                dispatch("main:dispatch:2", MAIN, CHILD, "pstack:other-agent"),
            ],
        ))
        self.assertEqual([edge["dispatch_id"] for edge in tree["edges"]],
                         ["main:dispatch:1", "main:dispatch:2"])
        self.assertEqual(tree["totals"]["edges"], 2)
        child = node_by_id(tree, CHILD)
        self.assertEqual(child["parent_agent_id"], MAIN)
        self.assertEqual(child["via_dispatch_id"], "main:dispatch:1")
        self.assertEqual(child["label"], "pstack:poteto-agent · 241caed7")
        main = node_by_id(tree, MAIN)
        self.assertIsNone(main["parent_agent_id"])
        self.assertIsNone(main["via_dispatch_id"])


class AttachmentTest(unittest.TestCase):
    def test_attachments_exclude_listing_only_skills(self):
        tree = skill_tree(document(
            agents=[agent(MAIN)],
            skills=[skill("demo", [
                observation("listing", channel="skill_listing", tokens=500,
                            item_id="i1")])],
        ))
        node = node_by_id(tree, MAIN)
        self.assertEqual(node["attachments"], [])
        self.assertEqual(node["ambient"],
                         {"skills": 1, "injections": 1, "tokens_est": 500})

    def test_execution_marks_the_attachment(self):
        tree = skill_tree(document(
            agents=[agent(MAIN)],
            skills=[skill("pstack:poteto-mode", [
                observation("execution", channel="skill_tool_call", ts=1.0,
                            tokens=3, item_id="i1"),
                observation("stub", channel="tool_result", ts=2.0, tokens=9,
                            item_id="i2"),
                observation("body", channel="tool:read", ts=3.0, tokens=357,
                            item_id="i3", sha1="abc123")],
                name="poteto-mode", aliases=("poteto-mode",))],
            loads=[load("main:load:1", MAIN, "pstack:poteto-mode", cost=357,
                        sha1="abc123")],
        ))
        attachment = node_by_id(tree, MAIN)["attachments"][0]
        self.assertTrue(attachment["executed"])
        self.assertEqual(attachment["loads_tokens_est"], 357)
        self.assertEqual(attachment["channels"], ["tool:read"])
        self.assertEqual(attachment["sha1s"], ["abc123"])
        self.assertEqual(attachment["name"], "poteto-mode")
        self.assertEqual(attachment["aliases"], ["poteto-mode"])

    def test_null_cost_load_is_kept_and_counted(self):
        tree = skill_tree(document(
            agents=[agent(MAIN)],
            skills=[skill("demo", [observation("stub", ts=1.0)])],
            loads=[load("main:load:1", MAIN, "demo", channel=None, cost=None,
                        basis="unavailable")],
        ))
        attachment = node_by_id(tree, MAIN)["attachments"][0]
        row = attachment["loads"][0]
        self.assertEqual(row["cost_basis"], "unavailable")
        self.assertIsNone(row["cost_tokens_est"])
        self.assertIsNone(row["channel"])
        self.assertEqual(attachment["loads_tokens_est"], 0)
        self.assertEqual(tree["totals"]["unavailable"], 1)


class TotalsTest(unittest.TestCase):
    def test_totals_are_re_derived(self):
        tree = skill_tree(document(
            agents=[agent(MAIN)],
            skills=[
                skill("a:x", [observation("listing", channel="skill_listing",
                                          tokens=11, item_id="i1")]),
                skill("a:y", [observation("body", tokens=5, item_id="i2")]),
            ],
            loads=[
                load("main:load:1", MAIN, "a:x", cost=100),
                load("main:load:2", MAIN, "a:y", channel=None, cost=None,
                     basis="unavailable"),
                load("main:load:3", MAIN, "a:x", kind="reload", cost=100),
            ],
        ))
        self.assertEqual(tree["totals"], {
            "agents": 1,
            "edges": 0,
            "loads": 2,
            "reloads": 1,
            "unavailable": 1,
            "executions": 0,
            "ambient_tokens_est": 11,
            "attached_tokens_est": 200,
        })
