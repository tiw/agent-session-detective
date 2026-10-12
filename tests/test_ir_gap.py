"""Skill GAP analysis unit tests — hand-built IR state, catalog, judgments.

Follows the test_ir_loads.py boundary: gap.py reads projected IR state
(SkillEntity + ContentItem), so join, set, and prefilter rules are tested
directly at that boundary, never through transcripts.
"""

import unittest
from pathlib import Path

from agent_session_detective.catalog import Skill
from agent_session_detective.ir.gap import gap_candidates, skill_gaps
from agent_session_detective.ir.schema import ContentItem, Observation, SkillEntity
from agent_session_detective.judge import Judgment


def make_skill(name, description="Use when testing."):
    return Skill(name=name, description=description,
                 trigger_excerpt=description,
                 path=Path("/tmp/%s/SKILL.md" % name))


def make_judgment(name, triggered=False, error=None):
    return Judgment(
        skill_name=name, triggered=triggered,
        turn=2 if triggered else None,
        rationale="session asked for %s" % name if triggered else "",
        evidence="please use %s" % name if triggered else "",
        confidence=0.9, error=error)


def entity(skill_id, kinds, aliases=(), tokens=100):
    """One observation per kind in `kinds`, wire order preserved."""
    observations = []
    for n, kind in enumerate(kinds, start=1):
        observations.append(Observation(
            kind=kind,
            channel="skill_listing" if kind == "listing" else "test",
            agent_id="main", call_id=None, ts=float(n),
            tokens_est=tokens,
            body_sha1="sha-%s-%d" % (skill_id, n) if kind == "body" else None,
            item_id="%s:%d" % (skill_id, n)))
    return SkillEntity(skill_id=skill_id, name=skill_id,
                       observations=observations, aliases=list(aliases))


class JoinAndSetsTest(unittest.TestCase):
    def test_join_sets_and_gap_categories(self):
        skills = [
            entity("alpha", ("listing", "body", "execution")),
            entity("beta", ("listing",)),
            entity("ghost", ("body",)),  # body without listing, no catalog entry
        ]
        catalog = [make_skill("alpha"), make_skill("beta")]
        gap = skill_gaps(skills, catalog, [])

        self.assertEqual(gap["sets"], {"listed": 2, "loaded": 2, "executed": 1})
        self.assertEqual(gap["join"]["matched"], 2)
        self.assertEqual(gap["join"]["catalog_only"], 0)
        self.assertEqual(
            [row["skill_id"] for row in gap["join"]["ir_only"]], ["ghost"])
        ir_only = gap["join"]["ir_only"][0]
        self.assertEqual(
            (ir_only["listed"], ir_only["loaded"], ir_only["executed"]),
            (0, 1, 0))
        self.assertEqual(
            [row["skill_id"] for row in gap["listed_unloaded"]], ["beta"])
        self.assertEqual(
            [row["skill_id"] for row in gap["loaded_unused"]], ["ghost"])

    def test_loaded_unused_carries_catalog_description(self):
        skills = [entity("alpha", ("body",))]
        catalog = [make_skill("alpha", description="Route here for pages.")]
        gap = skill_gaps(skills, catalog, [])
        row = gap["loaded_unused"][0]
        self.assertEqual(row["description"], "Route here for pages.")
        self.assertEqual((row["bodies"], row["stubs"]), (1, 0))

    def test_unmatched_loaded_unused_has_no_description(self):
        skills = [entity("ghost", ("body",))]
        gap = skill_gaps(skills, [], [])
        row = gap["loaded_unused"][0]
        self.assertIsNone(row["description"])
        self.assertIn("1 IR skill(s) matched no catalog entry", gap["notes"])

    def test_alias_joins_catalog(self):
        # identity merge (IR 1.2): bare id absorbed into namespaced twin
        skills = [entity("demo", ("listing", "body"), aliases=("pkg:demo",))]
        catalog = [make_skill("pkg:demo")]
        gap = skill_gaps(skills, catalog, [])
        self.assertEqual(gap["join"]["matched"], 1)
        self.assertEqual(gap["join"]["ir_only"], [])
        # a body without execution is loaded-but-unused even when the row
        # joined through an alias — the alias only fixes the join, not the gap
        self.assertEqual(
            [row["skill_id"] for row in gap["loaded_unused"]], ["demo"])
        self.assertEqual(
            gap["loaded_unused"][0]["description"], "Use when testing.")

    def test_stub_counts_as_loaded(self):
        skills = [entity("beta", ("listing", "stub"))]
        gap = skill_gaps(skills, [make_skill("beta")], [])
        self.assertEqual(gap["listed_unloaded"], [])
        self.assertEqual(gap["loaded_unused"], [])

    def test_judge_unconfigured_notes_and_unjudged(self):
        skills = [entity("beta", ("listing",))]
        gap = skill_gaps(skills, [make_skill("beta")], [],
                         stats={"listed_unloaded": 1, "candidates": 0,
                                "prefiltered": 1},
                         judge_configured=False)
        self.assertTrue(gap["unjudged"]["judge_unconfigured"])
        self.assertEqual(gap["unjudged"]["count"], 1)
        self.assertIn("judge not configured", gap["notes"][0])

    def test_triggered_judgment_becomes_missed_trigger_with_skill_id(self):
        skills = [entity("beta", ("listing",))]
        catalog = [make_skill("beta")]
        gap = skill_gaps(skills, catalog, [make_judgment("beta", triggered=True)])
        missed = gap["missed_triggers"]
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["skill_id"], "beta")
        self.assertEqual(missed[0]["evidence"], "please use beta")
        self.assertEqual(gap["unjudged"]["judge_errors"], 0)

    def test_triggered_unmatched_judgment_keeps_skill_id_none(self):
        skills = [entity("beta", ("listing",))]
        gap = skill_gaps(skills, [make_skill("beta")],
                         [make_judgment("nowhere", triggered=True)])
        missed = gap["missed_triggers"]
        self.assertEqual(len(missed), 1)
        self.assertIsNone(missed[0]["skill_id"])

    def test_error_judgment_counts_as_defect_not_missed(self):
        skills = [entity("beta", ("listing",))]
        catalog = [make_skill("beta")]
        gap = skill_gaps(skills, catalog,
                         [make_judgment("beta", triggered=True, error="boom"),
                          make_judgment("beta")],
                         stats={"listed_unloaded": 1, "candidates": 2,
                                "prefiltered": 0},
                         judge_configured=True)
        self.assertEqual(gap["missed_triggers"], [])
        self.assertEqual(gap["unjudged"]["judge_errors"], 1)
        # 2 candidates, 2 judgments (1 defect): nothing truncated — the error
        # row counts once, in judge_errors, never in over_limit
        self.assertEqual(gap["unjudged"]["over_limit"], 0)
        self.assertEqual(gap["unjudged"]["count"], 1)
