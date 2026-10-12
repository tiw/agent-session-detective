"""Supplied skill text must never read as something the log recorded.

The rule these tests defend, from the injection design spec: a supplied
description is real text with a real origin, so it may stand beside the facts —
but the log never saw it, so it can never be presented as evidence about the
session. A supplied ``intent`` is a person's statement and is weaker still.
"""

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_session_detective import cli
from agent_session_detective.catalog import Skill
from agent_session_detective.report import render_report
from agent_session_detective.timeline import build_timeline
from agent_session_detective.wire import load_session

FIXTURES = Path(__file__).parent / "fixtures"


def injected_skill(name="library", description="Route here for pages.",
                   intent=None, source="<injected>"):
    return Skill(name=name, description=description, trigger_excerpt=description,
                 path=Path(source), source="injected", intent=intent)


def render(tmp: Path, injected=None, expected=None):
    """Render the fixture transcript with the given supplied skills."""
    transcript = tmp / "t.jsonl"
    shutil.copy(FIXTURES / "qoder-transcript.jsonl", transcript)
    session = load_session(transcript)
    return render_report(
        session, build_timeline(session), [],
        judge_enabled=False, catalog_size=10,
        expected=expected, injected_skills=injected,
    )


class SuppliedSectionTests(unittest.TestCase):
    def test_section_lists_the_supplied_skill(self):
        with tempfile.TemporaryDirectory() as td:
            html = render(Path(td), {"library": injected_skill()})
        self.assertIn("Supplied Skill Text", html)
        self.assertIn("library", html)

    def test_section_states_the_log_never_recorded_it(self):
        # The sentence that keeps the section honest. Without it the table
        # below reads like ordinary catalog data.
        with tempfile.TemporaryDirectory() as td:
            html = render(Path(td), {"library": injected_skill()})
        self.assertIn("never recorded it", html)

    def test_no_section_when_nothing_was_supplied(self):
        with tempfile.TemporaryDirectory() as td:
            html = render(Path(td), None)
        self.assertNotIn("Supplied Skill Text", html)

    def test_intent_is_labelled_as_stated_not_observed(self):
        with tempfile.TemporaryDirectory() as td:
            html = render(Path(td), {
                "library": injected_skill(intent="资料库统一入口")})
        self.assertIn("stated, not observed", html)
        self.assertIn("资料库统一入口", html)

    def test_absent_intent_says_so_rather_than_rendering_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            html = render(Path(td), {"library": injected_skill()})
        self.assertIn("none supplied", html)

    def test_description_is_shown_as_supplied_text(self):
        with tempfile.TemporaryDirectory() as td:
            html = render(Path(td), {
                "library": injected_skill(description="Route here for pages.")})
        self.assertIn("Route here for pages.", html)

    def test_supplied_text_is_escaped(self):
        with tempfile.TemporaryDirectory() as td:
            html = render(Path(td), {
                "library": injected_skill(description="<script>alert(1)</script>")})
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class BadgeTests(unittest.TestCase):
    def test_badge_appears_on_the_skill_name(self):
        with tempfile.TemporaryDirectory() as td:
            html = render(Path(td), {"library": injected_skill()},
                          expected=["library"])
        self.assertIn("badge injected", html)

    def test_badge_is_not_applied_to_catalog_skills(self):
        # A scanned skill must not pick up a supplied-text mark; that would
        # libel the parser. Two badges are expected here — the supplied-text
        # section's own row and the Expectations row — and neither may sit
        # next to the scanned name.
        with tempfile.TemporaryDirectory() as td:
            html = render(Path(td), {"library": injected_skill()},
                          expected=["library", "some-scanned-skill"])
        self.assertEqual(html.count("badge injected"), 2)
        expectations_row = re.search(
            r"<h2>Expectations</h2>.*?</table>", html, re.S).group(0)
        marked = re.findall(r"<td>([^<]*)<span class='badge injected'", expectations_row)
        self.assertEqual(marked, ["library"])

    def test_badge_title_explains_the_origin(self):
        with tempfile.TemporaryDirectory() as td:
            html = render(Path(td), {"library": injected_skill()})
        match = re.search(r"class='badge injected' title='([^']*)'", html)
        self.assertIsNotNone(match)
        self.assertIn("not parsed from a scanned SKILL.md", match.group(1))


class CliWiringTests(unittest.TestCase):
    def test_cli_passes_injected_skills_to_the_report(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            transcript = base / "t.jsonl"
            shutil.copy(FIXTURES / "qoder-transcript.jsonl", transcript)
            truth = base / "truth.json"
            truth.write_text(json.dumps({
                "library": {"description": "Supplied routing text."}}), "utf-8")
            with patch("agent_session_detective.cli.render_report",
                       return_value="<html>report</html>") as render:
                cli.main([str(transcript), "--no-judge",
                          "--injected", str(truth)])
        # keyed by name so the report can match case-insensitively
        passed = render.call_args.kwargs["injected_skills"]
        self.assertEqual(sorted(passed), ["library"])
        self.assertTrue(passed["library"].is_injected)

    def test_no_injection_passes_an_empty_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            transcript = base / "t.jsonl"
            shutil.copy(FIXTURES / "qoder-transcript.jsonl", transcript)
            with patch("agent_session_detective.cli.render_report",
                       return_value="<html>report</html>") as render:
                cli.main([str(transcript), "--no-judge"])
        self.assertEqual(render.call_args.kwargs["injected_skills"], {})


def gap_block():
    return {
        "join": {"matched": 1,
                 "ir_only": [{"skill_id": "ghost", "name": "ghost",
                              "listed": 3, "loaded": 0, "executed": 0}],
                 "catalog_only": 5},
        "sets": {"listed": 2, "loaded": 1, "executed": 1},
        "listed_unloaded": [
            {"skill_id": "ghost", "name": "ghost", "description": None,
             "bodies": 0, "stubs": 0}],
        "missed_triggers": [
            {"skill_id": "beta", "name": "beta", "turn": 2,
             "rationale": "session asked for beta",
             "evidence": "please use beta", "confidence": 0.9}],
        "loaded_unused": [
            {"skill_id": "alpha", "name": "alpha",
             "description": "Use when testing.", "bodies": 1, "stubs": 1}],
        "unjudged": {"count": 0, "prefiltered": 0, "over_limit": 0,
                     "judge_errors": 0, "judge_unconfigured": False},
        "notes": [],
    }


class SkillGapSectionTests(unittest.TestCase):
    def render_with_gap(self, gap):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "t.jsonl"
            shutil.copy(FIXTURES / "qoder-transcript.jsonl", transcript)
            session = load_session(transcript)
            return render_report(
                session, build_timeline(session), [],
                judge_enabled=True, catalog_size=6, gap=gap)

    def test_section_renders_missed_triggers_with_evidence(self):
        html = self.render_with_gap(gap_block())
        self.assertIn("Skill GAP", html)
        self.assertIn("missed triggers", html)
        self.assertIn("please use beta", html)
        self.assertIn("beta", html)

    def test_section_renders_loaded_unused_with_declared_use(self):
        html = self.render_with_gap(gap_block())
        self.assertIn("loaded but never executed", html)
        self.assertIn("Use when testing.", html)

    def test_section_renders_ir_only_table(self):
        html = self.render_with_gap(gap_block())
        self.assertIn("IR-only (no catalog entry)", html)
        self.assertIn("ghost", html)

    def test_judge_off_notice_when_unconfigured(self):
        gap = gap_block()
        gap["unjudged"]["judge_unconfigured"] = True
        gap["missed_triggers"] = []
        html = self.render_with_gap(gap)
        self.assertIn("trigger GAPs not evaluated", html)


if __name__ == "__main__":
    unittest.main()
