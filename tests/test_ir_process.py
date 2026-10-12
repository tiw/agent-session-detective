"""Process signature tests — hand-built IR state, no transcripts.

process_signature reads projected IR state (items / skills / skill_loads /
interventions), so the style rule, listing re-injection accounting and
workflow-file matching are tested directly at that boundary (the
FakeTimeline precedent in test_judge.py).
"""

import unittest
from types import SimpleNamespace

from agent_session_detective.ir.process import process_signature
from agent_session_detective.ir.schema import (
    ContentItem,
    Intervention,
    LoadEvidence,
    Observation,
    SkillEntity,
)


def item(seq, bucket="user", kind="user_message", file_path=None, name=None,
         tokens=100):
    return ContentItem(
        item_id="main:%d:0" % seq, agent_id="main", bucket=bucket, kind=kind,
        name=name, channel="qoder:%s" % bucket, wire_seq=seq, gone_seq=None,
        size_chars=tokens * 4, tokens_est=tokens,
        sha1="sha-%d" % seq, norm_sha1="nsha-%d" % seq,
        record={"file": "t.jsonl", "seq": seq}, preview="p%d" % seq,
        file_path=file_path)


def call(seq, name, file_path=None):
    return item(seq, bucket="assistant", kind="tool_call", name=name,
                file_path=file_path)


def entity(skill_id, listing_tokens):
    """One listing observation per entry of ``listing_tokens``."""
    observations = [
        Observation(kind="listing", channel="skill_listing", agent_id="main",
                    call_id=None, ts=float(n), tokens_est=tokens,
                    body_sha1=None, item_id="%s:%d" % (skill_id, n))
        for n, tokens in enumerate(listing_tokens, start=1)]
    return SkillEntity(skill_id=skill_id, name=skill_id,
                       observations=observations)


def load(skill_id, kind="load"):
    return LoadEvidence(load_id="%s-1" % skill_id, agent_id="main",
                        skill_id=skill_id, kind=kind, marker_item_id=None,
                        body_item_id=None, body_sha1=None, channel=None,
                        ts=None, cost_tokens_est=None,
                        cost_basis="unavailable")


def intervention(label):
    return Intervention(item_id="main:1:0", label=label, rule="r",
                        evidence="", carriers=[], lead_sha1="x")


def document(items=(), skills=(), skill_loads=(), interventions=()):
    return SimpleNamespace(items=list(items), skills=list(skills),
                           skill_loads=list(skill_loads),
                           interventions=list(interventions))


class StyleTest(unittest.TestCase):
    def test_direct_has_no_machinery(self):
        sig = process_signature(document(
            items=[item(1, bucket="user", kind="user_message")]))
        self.assertEqual(sig["style"], "direct")
        self.assertEqual(sig["workflow"], {"reads": 0, "writes": 0})
        self.assertEqual(sig["skill_loads"], {"loads": 0, "reloads": 0})

    def test_pipeline_needs_workflow_files_and_loads(self):
        sig = process_signature(document(
            items=[call(1, "Read", "/ws/.changes/demo/progress.md"),
                   call(2, "Write", "/ws/.changes/demo/progress.md")],
            skill_loads=[load("alpha"), load("beta", "reload")]))
        self.assertEqual(sig["style"], "pipeline")
        self.assertEqual(sig["workflow"], {"reads": 1, "writes": 1})
        self.assertEqual(sig["skill_loads"], {"loads": 2, "reloads": 1})

    def test_workflow_without_loads_is_mixed(self):
        sig = process_signature(document(
            items=[call(1, "Read", "/ws/progress.md")]))
        self.assertEqual(sig["style"], "mixed")

    def test_loads_without_workflow_files_is_mixed(self):
        sig = process_signature(document(skill_loads=[load("alpha")]))
        self.assertEqual(sig["style"], "mixed")


class WorkflowMatchingTest(unittest.TestCase):
    def test_progress_md_basename_matches_regardless_of_dir(self):
        sig = process_signature(document(items=[
            call(1, "Read", "/a/b/progress.md"),
            call(2, "Read", "progress.md")]))
        self.assertEqual(sig["workflow"]["reads"], 2)

    def test_changes_directory_content_counts(self):
        sig = process_signature(document(items=[
            call(1, "Edit", "/ws/.changes/demo/tmp/notes.md")]))
        self.assertEqual(sig["workflow"], {"reads": 0, "writes": 1})

    def test_lookalike_paths_do_not_match(self):
        sig = process_signature(document(items=[
            call(1, "Read", "/tmp/my_progress.md"),
            call(2, "Read", "/tmp/progress.md.bak"),
            call(3, "Read", "/tmp/.changes-backup/x.md")]))
        self.assertEqual(sig["workflow"]["reads"], 0)

    def test_other_tools_with_paths_do_not_count(self):
        sig = process_signature(document(items=[
            call(1, "Grep", "/ws/.changes/demo/progress.md"),
            call(2, "Read", None)]))
        self.assertEqual(sig["workflow"]["reads"], 0)


class ListingReinjectTest(unittest.TestCase):
    def test_extra_occurrences_are_reinjection(self):
        sig = process_signature(document(skills=[
            entity("alpha", [10, 10, 10]),
            entity("beta", [7])]))
        self.assertEqual(sig["listing"], {
            "skills": 2, "occurrences": 4,
            "reinject_occurrences": 2, "reinject_tokens_est": 20})

    def test_single_listing_never_reinjected(self):
        sig = process_signature(document(skills=[entity("beta", [7])]))
        self.assertEqual(sig["listing"]["reinject_tokens_est"], 0)

    def test_no_listings_note(self):
        sig = process_signature(document(items=[item(1)]))
        self.assertIn("unmeasurable", " ".join(sig["notes"]))


class InjectAndInterventionsTest(unittest.TestCase):
    def test_inject_bucket_volume(self):
        sig = process_signature(document(items=[
            item(1, bucket="inject", kind="reminder", tokens=100),
            item(2, bucket="inject", kind="memory", tokens=50),
            item(3, bucket="user", kind="user_message", tokens=500)]))
        self.assertEqual(sig["inject"], {"items": 2, "tokens_est": 150})

    def test_signal_excludes_noise_only(self):
        sig = process_signature(document(interventions=[
            intervention("noise"), intervention("noise"),
            intervention("correction"), intervention("unclassified")]))
        self.assertEqual(sig["interventions"], {"total": 4, "signal": 2})
