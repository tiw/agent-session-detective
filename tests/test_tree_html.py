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


if __name__ == "__main__":
    unittest.main()
