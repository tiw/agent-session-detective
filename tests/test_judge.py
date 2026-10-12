"""judge_session contract: candidates come in final — no internal filtering.

The counterfactual question only makes sense for skills the session never
loaded, and IR evidence decides that. judge_session must therefore trust
the caller's list instead of re-filtering through the older timeline view,
whose consumed set can disagree with IR observations.
"""

import unittest

from agent_session_detective.judge import Judgment, judge_session


class StubJudge:
    """Records what it was asked; never hits the network."""

    def __init__(self, model="stub"):
        self.model = model
        self.asked = []

    def judge_skill(self, skill, turns_block):
        self.asked.append(skill.name)
        return Judgment(skill_name=skill.name, triggered=False, turn=None,
                        rationale="", evidence="", confidence=0.9)


class FakeTurn:
    def text_preview(self, limit):
        return "turn text"


class FakeTimeline:
    """Duck-typed: has turns, deliberately has NO consumed_skill_names."""

    turns = [FakeTurn()]


def make_skill(name):
    from pathlib import Path
    from agent_session_detective.catalog import Skill
    return Skill(name=name, description="Use when %s." % name,
                 trigger_excerpt="Use when %s." % name,
                 path=Path("/tmp/%s/SKILL.md" % name))


class JudgeSessionCandidatesTest(unittest.TestCase):
    def test_asks_exactly_the_candidates_given(self):
        from agent_session_detective.judge import Judge  # noqa: F401
        judge = StubJudge()
        catalog = [make_skill("alpha"), make_skill("beta")]

        judgments = judge_session(FakeTimeline(), catalog, judge)

        self.assertEqual(judge.asked, ["alpha", "beta"])
        self.assertEqual(len(judgments), 2)

    def test_limit_truncates_candidates(self):
        judge = StubJudge()
        catalog = [make_skill("alpha"), make_skill("beta")]

        judge_session(FakeTimeline(), catalog, judge, limit=1)

        self.assertEqual(judge.asked, ["alpha"])

    def test_limit_zero_judges_nothing(self):
        judge = StubJudge()
        catalog = [make_skill("alpha"), make_skill("beta")]

        judgments = judge_session(FakeTimeline(), catalog, judge, limit=0)

        self.assertEqual(judge.asked, [])
        self.assertEqual(judgments, [])
