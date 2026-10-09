"""Renderer contract tests for the standalone skill-tree page."""

import unittest

from agent_session_detective.tree_html import render_skill_tree
from tests.test_skill_tree import (CHILD, MAIN, agent, dispatch, document,
                                   load, observation, skill)


def page(*args, **kwargs):
    return render_skill_tree(document(*args, **kwargs))


class SelfContainedTest(unittest.TestCase):
    def test_no_script_and_no_external_urls(self):
        out = page(agents=[agent(MAIN)],
                   skills=[skill("demo", [observation("body", tokens=40)])],
                   loads=[load("main:load:1", MAIN, "demo", cost=40)])
        self.assertNotIn("<script", out)
        self.assertNotIn("http://", out)
        self.assertNotIn("https://", out)
        self.assertNotIn("src=", out)
        self.assertNotIn("href=", out)
        self.assertNotIn("url(", out)

    def test_determinism(self):
        kwargs = dict(
            agents=[agent(MAIN)],
            skills=[skill("demo", [observation("body", tokens=40)])],
            loads=[load("main:load:1", MAIN, "demo", cost=40)])
        self.assertEqual(page(**kwargs), page(**kwargs))


class UnavailableTest(unittest.TestCase):
    def test_unavailable_renders_the_word_never_a_number(self):
        out = page(
            agents=[agent(MAIN)],
            skills=[skill("demo", [observation("stub", ts=1.0)])],
            loads=[load("main:load:1", MAIN, "demo", channel=None,
                        cost=None, basis="unavailable")])
        self.assertIn("unavailable", out)
        self.assertNotIn("~", out)

    def test_a_known_cost_renders_as_est(self):
        out = page(
            agents=[agent(MAIN)],
            skills=[skill("demo", [observation("body", tokens=37)])],
            loads=[load("main:load:1", MAIN, "demo", cost=37)])
        self.assertIn("~37 EST", out)


class EscapingTest(unittest.TestCase):
    def test_escaping(self):
        out = page(
            agents=[agent(MAIN)],
            skills=[skill('a<b&"c', [observation("body", tokens=10)])],
            loads=[load("main:load:1", MAIN, 'a<b&"c', cost=10)],
            dispatches=[dispatch("main:dispatch:1", MAIN, CHILD, "<i>t</i>",
                                 description="<b>bold</b>", brief=None)])
        self.assertIn("a&lt;b&amp;&quot;c", out)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", out)


class AliasChipTest(unittest.TestCase):
    def test_alias_chip_is_rendered_from_the_entity(self):
        out = page(
            agents=[agent(MAIN)],
            skills=[skill("ns:demo", [observation("body", tokens=37)],
                          name="demo", aliases=["demo"])],
            loads=[load("main:load:1", MAIN, "ns:demo", cost=37)])
        self.assertIn("(alias: demo)", out)
        self.assertIn("ns:demo", out)

    def test_alias_chip_uses_the_stylesheet_badge_class(self):
        out = page(
            agents=[agent(MAIN)],
            skills=[skill("ns:demo", [observation("body", tokens=37)],
                          name="demo", aliases=["demo"])],
            loads=[load("main:load:1", MAIN, "ns:demo", cost=37)])
        self.assertIn("class='badge missed'>(alias: demo)", out)


class StructureTest(unittest.TestCase):
    def test_cycle_guard_renders_the_note_and_terminates(self):
        out = page(
            agents=[agent(MAIN), agent("A", request_ids=("a:0",))],
            dispatches=[dispatch("a:dispatch:1", "A", MAIN, "subagent"),
                        dispatch("main:dispatch:1", MAIN, "A", "subagent")])
        self.assertIn("already on this path", out)

    def test_orphan_and_loose_are_rendered(self):
        out = page(
            agents=[agent(MAIN), agent(CHILD)],
            dispatches=[dispatch("main:dispatch:1", MAIN, None, "subagent")])
        self.assertIn("orphan dispatch", out)
        self.assertIn("no incoming dispatch edge", out)

    def test_honest_empty_node(self):
        out = page(agents=[agent(MAIN)])
        self.assertIn("no skill loads, no executions", out)
        self.assertIn("(counted without rows)", out)


class EmptyDocumentTest(unittest.TestCase):
    def test_empty_document(self):
        out = page(agents=[])
        self.assertIn("no agents in document", out)


class DepthCapTest(unittest.TestCase):
    def test_chain_longer_than_the_cap_renders_the_depth_notice(self):
        agents = [agent(MAIN)]
        dispatches = []
        previous = MAIN
        for i in range(14):
            agent_id = "subagent:agent-%02d-%s" % (i, "ab12cd34" * 2)
            agents.append(agent(agent_id))
            dispatches.append(dispatch(
                "chain:dispatch:%02d" % i, previous, agent_id, "subagent"))
            previous = agent_id
        out = page(agents=agents, dispatches=dispatches)
        self.assertIn("depth limit reached at", out)


class NoMainRootsTest(unittest.TestCase):
    def test_document_without_main_renders_every_agent_as_a_root(self):
        root_a = "subagent:root-a-11111111"
        root_b = "subagent:root-b-22222222"
        out = page(agents=[agent(root_a), agent(root_b)])
        self.assertIn("<summary>%s" % root_a, out)
        self.assertIn("<summary>%s" % root_b, out)


class LooseSubtreeTest(unittest.TestCase):
    def test_loose_agent_renders_attachments_and_its_subtree(self):
        grandchild = "subagent:agent-gc-3333444455556666"
        out = page(
            agents=[agent(MAIN), agent(CHILD), agent(grandchild)],
            skills=[skill("loose:demo", [observation(
                "body", CHILD, tokens=42, item_id="i1")])],
            loads=[load("child:load:1", CHILD, "loose:demo", cost=42)],
            dispatches=[dispatch("child:dispatch:1", CHILD, grandchild,
                                 "agent-gc"),
                        dispatch("child:dispatch:2", CHILD, None,
                                 "subagent")])
        self.assertIn("no incoming dispatch edge", out)
        self.assertIn("loose:demo", out)
        self.assertIn("agent-gc", out)
        self.assertIn("no matching subagent transcript", out)


class MissingChildTest(unittest.TestCase):
    def test_dispatch_target_without_a_transcript_renders_the_gap(self):
        ghost = "subagent:ghost-99999999"
        out = page(
            agents=[agent(MAIN)],
            dispatches=[dispatch("main:dispatch:1", MAIN, ghost, "subagent")])
        self.assertIn("has no transcript", out)
        self.assertIn(ghost, out)


class FooterCoverageEscapingTest(unittest.TestCase):
    def test_identity_fragments_in_the_footer_are_escaped(self):
        doc = document(agents=[agent(MAIN)])
        doc.coverage.skill_identity = {
            "merges": 1,
            "aliases": {"ns:demo": ["<i>x</i>"]},
            "ambiguous": [],
        }
        out = render_skill_tree(doc)
        self.assertIn("identity merges", out)
        self.assertIn("&lt;i&gt;", out)
        self.assertNotIn("<i>x</i>", out)


class TagBalanceTest(unittest.TestCase):
    def test_skill_attachments_leave_no_unclosed_tags(self):
        # both branches of the attachment conditional (table rows / no
        # load rows) must emit balanced div and details markup.
        out = page(
            agents=[agent(MAIN)],
            skills=[skill("demo", [observation("body", tokens=40)]),
                    skill("stub:demo", [observation("stub", ts=1.0)])],
            loads=[load("main:load:1", MAIN, "demo", cost=40)])
        self.assertIn("no load rows", out)
        self.assertIn("<table", out)
        self.assertGreater(out.count("<div"), 0)
        self.assertGreater(out.count("<details"), 0)
        self.assertEqual(out.count("<div"), out.count("</div>"))
        self.assertEqual(out.count("<details"), out.count("</details>"))


class SourcesTest(unittest.TestCase):
    def test_sources_are_collapsed_one_per_line(self):
        doc = document(agents=[agent(MAIN)])
        doc.source_files = ["a6f01ae6.jsonl", "subagents/agent-x.jsonl"]
        out = render_skill_tree(doc)
        self.assertIn("<details class='sources'><summary>2 source transcripts"
                      "</summary>", out)
        self.assertIn("<li>a6f01ae6.jsonl</li>", out)
        self.assertIn("<li>subagents/agent-x.jsonl</li>", out)
        self.assertNotIn("a6f01ae6.jsonl ·", out)

    def test_theme_uses_the_webapp_dark_tokens(self):
        out = page(agents=[agent(MAIN)])
        self.assertIn("#09090b", out)
        self.assertIn("ui-monospace", out)


if __name__ == "__main__":
    unittest.main()
