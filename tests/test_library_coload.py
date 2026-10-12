# tests/test_library_coload.py
"""The empirical co-load graph and its diff against declared requires.

``co_load_graph`` answers "which skills actually loaded together, in which
agents" from the document alone. ``diff_edges`` compares that observation
with what the catalog declares, exposing both over-declaration and the
implicit pairs nobody wrote down. Facts only: it never claims a co-load was
right or wrong.
"""

import unittest

from agent_session_detective.ir.analyses import build_analyses
from agent_session_detective.ir.library import (
    CoLoadEdge,
    co_load_graph,
    diff_edges,
    join_declared_observed,
    library_report,
)
from agent_session_detective.ir.schema import (
    AuditDocument, ContentItem, ContextAgent, CoverageReport, Observation,
    SkillEntity,
)

MAIN = "main"
CHILD = "subagent:agent-apstack:poteto-agent-241caed765ad6b60"


def agent(agent_id):
    return ContextAgent(agent_id=agent_id, parent_id=None,
                        origin_channel="qoder", model="qoder-pro",
                        context_window=200000,
                        request_ids=["%s:0" % agent_id], active_leaf=None)


def observation(kind, agent_id=MAIN, item_id="i0"):
    return Observation(kind=kind, channel="tool:read", agent_id=agent_id,
                       call_id=None, ts=None, tokens_est=10,
                       body_sha1=None, item_id=item_id)


def skill(skill_id, observations):
    return SkillEntity(skill_id=skill_id, name=skill_id,
                       observations=list(observations))


def item(item_id, agent_id=MAIN, wire_seq=0):
    return ContentItem(item_id=item_id, agent_id=agent_id, bucket="skill",
                       kind="skill_body", name=None, channel="tool:read",
                       wire_seq=wire_seq, gone_seq=None, size_chars=0,
                       tokens_est=10, sha1="s", norm_sha1="n", record={},
                       preview="")


def document(agents, skills, items=()):
    return AuditDocument(
        adapter={"id": "test", "version": "test"},
        estimator_version="test",
        source_files=["test.jsonl"],
        agents=list(agents),
        requests=[],
        items=list(items),
        skills=list(skills),
        compactions=[],
        coverage=CoverageReport(
            per_field={}, requests_with_anchor="0/0", request_identity={},
            bucket_sources={}, unknown_channels=[], dropped_records={},
            notes=[]),
    )


def edge(a, b, weight=1, agent_ids=None):
    return CoLoadEdge(a=a, b=b, weight=weight,
                      agent_ids=list(agent_ids or [MAIN]))


class CoLoadGraphTest(unittest.TestCase):
    def test_skills_loaded_in_one_agent_form_an_edge(self):
        graph = co_load_graph(document(
            agents=[agent(MAIN)],
            skills=[skill("a", [observation("body")]),
                    skill("b", [observation("body")])],
        ))
        self.assertEqual(graph, [
            CoLoadEdge(a="a", b="b", weight=1, agent_ids=[MAIN])])

    def test_weight_counts_agents_not_observations(self):
        # Three observations in MAIN are still one agent agreeing; only the
        # CHILD appearance raises the weight.
        graph = co_load_graph(document(
            agents=[agent(MAIN), agent(CHILD)],
            skills=[
                skill("a", [observation("body"), observation("stub"),
                            observation("execution"), observation("body", CHILD)]),
                skill("b", [observation("body"), observation("body", CHILD)]),
            ],
        ))
        self.assertEqual(len(graph), 1)
        self.assertEqual(graph[0].weight, 2)
        self.assertEqual(graph[0].agent_ids, [MAIN, CHILD])

    def test_listing_alone_is_not_loading(self):
        # A listing is ambient re-injection, not an attachment; the router
        # being resident in every request must not pair it with everything.
        graph = co_load_graph(document(
            agents=[agent(MAIN)],
            skills=[skill("a", [observation("body")]),
                    skill("b", [observation("body")]),
                    skill("router", [observation("listing"),
                                     observation("listing")])],
        ))
        self.assertEqual([(e.a, e.b) for e in graph], [("a", "b")])

    def test_single_skill_agent_contributes_no_edge(self):
        graph = co_load_graph(document(
            agents=[agent(MAIN), agent(CHILD)],
            skills=[skill("a", [observation("body")]),
                    skill("b", [observation("body")]),
                    skill("solo", [observation("body", CHILD)])],
        ))
        self.assertEqual([(e.a, e.b) for e in graph], [("a", "b")])

    def test_edges_sorted_by_weight_then_names(self):
        graph = co_load_graph(document(
            agents=[agent(MAIN), agent(CHILD)],
            skills=[
                skill("a", [observation("body"), observation("body", CHILD)]),
                skill("b", [observation("body"), observation("body", CHILD)]),
                skill("c", [observation("body")]),
            ],
        ))
        self.assertEqual([(e.a, e.b, e.weight) for e in graph],
                         [("a", "b", 2), ("a", "c", 1), ("b", "c", 1)])

    def test_no_skills_means_no_graph(self):
        self.assertEqual(co_load_graph(document(agents=[], skills=[])), [])


class DiffEdgesTest(unittest.TestCase):
    def test_declared_and_observed_is_consistent(self):
        report = diff_edges({"a": ["b"]}, [edge("a", "b")])
        self.assertEqual(report["consistent"],
                         [{"a": "a", "b": "b", "weight": 1}])
        self.assertEqual(report["over_declared"], [])
        self.assertEqual(report["implicit"], [])

    def test_declared_but_never_observed_is_over_declared(self):
        report = diff_edges({"a": ["b"]}, [])
        self.assertEqual(report["over_declared"], [{"a": "a", "b": "b"}])
        self.assertEqual(report["consistent"], [])

    def test_repeatedly_observed_undeclared_pair_is_implicit(self):
        report = diff_edges({}, [edge("a", "b", weight=3,
                                      agent_ids=[MAIN, CHILD, "x"])])
        self.assertEqual(report["implicit"], [{
            "a": "a", "b": "b", "weight": 3,
            "agent_ids": [MAIN, CHILD, "x"]}])

    def test_a_single_sighting_stays_below_the_threshold(self):
        # One agent showing a pair can be coincidence; the default needs two
        # before it is called an implicit dependency, and admits the count.
        report = diff_edges({}, [edge("a", "b", weight=1)])
        self.assertEqual(report["implicit"], [])
        self.assertEqual(report["totals"]["observed"], 1)
        self.assertEqual(report["totals"]["implicit_min_weight"], 2)
        loose = diff_edges({}, [edge("a", "b", weight=1)], min_weight=1)
        self.assertEqual(len(loose["implicit"]), 1)

    def test_declared_order_does_not_matter(self):
        report = diff_edges({"b": ["a"]}, [edge("b", "a")])
        self.assertEqual(report["consistent"],
                         [{"a": "a", "b": "b", "weight": 1}])
        self.assertEqual(report["over_declared"], [])

    def test_self_declaration_is_ignored(self):
        report = diff_edges({"a": ["a"]}, [])
        self.assertEqual(report["over_declared"], [])
        self.assertEqual(report["totals"]["declared"], 0)

    def test_implicit_rows_sorted_by_weight_then_names(self):
        report = diff_edges({}, [
            edge("x", "y", weight=2, agent_ids=[MAIN, CHILD]),
            edge("a", "b", weight=5,
                 agent_ids=[MAIN, CHILD, "m2", "m3", "m4"]),
            edge("a", "c", weight=2, agent_ids=[MAIN, CHILD]),
        ])
        self.assertEqual([(r["a"], r["b"]) for r in report["implicit"]],
                         [("a", "b"), ("a", "c"), ("x", "y")])


class JoinDeclaredObservedTest(unittest.TestCase):
    """The identity join that lets the two name spaces meet.

    Declared names are bare (catalog frontmatter); observed ids are often
    namespaced (``superpowers:brainstorming``). The join mirrors the D1
    rule in ir/skills: join only when unique — an exact id first, else a
    single ``:``-suffixed twin; ambiguous ids are counted, never guessed.
    """

    def test_unique_suffix_twin_joins_the_declared_name(self):
        joined, report = join_declared_observed(
            {"brainstorming": [], "writing-plans": ["brainstorming"]},
            [edge("superpowers:brainstorming", "superpowers:writing-plans")])
        self.assertEqual([(e.a, e.b) for e in joined],
                         [("brainstorming", "writing-plans")])
        self.assertEqual(report["renamed"], {
            "superpowers:brainstorming": "brainstorming",
            "superpowers:writing-plans": "writing-plans"})
        self.assertEqual(report["ambiguous"], [])

    def test_pair_order_is_repaired_after_renaming(self):
        # Raw ids sort as ("ns:y", "x"); the joined pair must come out as
        # ("x", "y") — the a < b invariant is about the joined names.
        joined, _ = join_declared_observed(
            {"x": [], "y": []}, [edge("x", "ns:y")])
        self.assertEqual([(e.a, e.b) for e in joined], [("x", "y")])

    def test_exact_id_shields_its_suffixed_twin(self):
        # D1: a name that appears un-namespaced has a first-class identity
        # of its own; its twin stays a separate entity, unjoined.
        joined, report = join_declared_observed(
            {"brainstorming": ["writing-plans"], "writing-plans": []},
            [edge("brainstorming", "superpowers:writing-plans"),
             edge("superpowers:brainstorming", "superpowers:writing-plans")])
        self.assertEqual(report["renamed"], {
            "superpowers:writing-plans": "writing-plans"})
        self.assertEqual(report["ambiguous"], [])
        self.assertEqual([(e.a, e.b) for e in joined],
                         [("brainstorming", "writing-plans"),
                          ("superpowers:brainstorming", "writing-plans")])

    def test_two_twins_of_one_name_stay_ambiguous(self):
        # The real wiki pair: one declared name, two namespaced variants —
        # joining either would equate two distinct skills.
        joined, report = join_declared_observed(
            {"wiki-query": []},
            [edge("auxpc:wiki-query", "auxpc:helper"),
             edge("paze:wiki-query", "auxpc:helper")])
        self.assertEqual(report["renamed"], {})
        self.assertEqual(report["ambiguous"],
                         ["auxpc:wiki-query", "paze:wiki-query"])
        self.assertEqual([(e.a, e.b) for e in joined],
                         [("auxpc:helper", "auxpc:wiki-query"),
                          ("auxpc:helper", "paze:wiki-query")])

    def test_one_twin_matching_two_declared_names_stays_ambiguous(self):
        joined, report = join_declared_observed(
            {"b:c": [], "c": []}, [edge("a:b:c", "solo")])
        self.assertEqual(report["renamed"], {})
        self.assertEqual(report["ambiguous"], ["a:b:c"])
        self.assertEqual([(e.a, e.b) for e in joined], [("a:b:c", "solo")])

    def test_case_folded_join_echoes_the_declared_spelling(self):
        joined, report = join_declared_observed(
            {"Brainstorming": []}, [edge("superpowers:brainstorming", "solo")])
        self.assertEqual(report["renamed"],
                         {"superpowers:brainstorming": "Brainstorming"})
        self.assertEqual([(e.a, e.b) for e in joined],
                         [("Brainstorming", "solo")])

    def test_unjoined_ids_pass_through_untouched(self):
        joined, report = join_declared_observed(
            {"solo": []}, [edge("other:thing", "solo")])
        self.assertEqual(report["renamed"], {})
        self.assertEqual(report["ambiguous"], [])
        self.assertEqual([(e.a, e.b) for e in joined], [("other:thing", "solo")])


class LibraryReportTest(unittest.TestCase):
    """The wiring-facing block: raw edges + identity join + declared diff."""

    def test_declared_pair_confirmed_by_namespaced_loads_is_consistent(self):
        doc = document(
            agents=[agent(MAIN)],
            skills=[
                skill("superpowers:brainstorming", [observation("body")]),
                skill("superpowers:writing-plans", [observation("body")]),
            ],
        )
        report = library_report(
            doc, {"brainstorming": [], "writing-plans": ["brainstorming"]})
        self.assertEqual(report["edges"][0]["a"], "superpowers:brainstorming")
        self.assertEqual(report["diff"]["consistent"],
                         [{"a": "brainstorming", "b": "writing-plans",
                           "weight": 1}])
        self.assertEqual(report["diff"]["over_declared"], [])
        self.assertEqual(report["join"]["renamed"], {
            "superpowers:brainstorming": "brainstorming",
            "superpowers:writing-plans": "writing-plans"})

    def test_no_declarations_means_an_empty_declared_side(self):
        doc = document(agents=[agent(MAIN)],
                       skills=[skill("a", [observation("body")]),
                               skill("b", [observation("body")])])
        report = library_report(doc, {"a": [], "b": []})
        self.assertEqual(report["diff"]["totals"]["declared"], 0)
        self.assertEqual(report["diff"]["totals"]["observed"], 1)
        self.assertEqual(len(report["edges"]), 1)


class BuildAnalysesIntegrationTest(unittest.TestCase):
    def test_build_analyses_gains_the_library_key(self):
        doc = document(
            agents=[agent(MAIN)],
            skills=[skill("a", [observation("body", item_id="i-a")]),
                    skill("b", [observation("body", item_id="i-b")])],
            items=[item("i-a", wire_seq=0), item("i-b", wire_seq=1)],
        )
        analyses = build_analyses(doc)
        self.assertIn("skill_tree", analyses)
        self.assertEqual(analyses["library"]["edges"], [
            {"a": "a", "b": "b", "weight": 1, "agent_ids": [MAIN]}])


if __name__ == "__main__":
    unittest.main()
