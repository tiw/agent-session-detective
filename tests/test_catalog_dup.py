# tests/test_catalog_dup.py
"""Near-duplicate detection across the catalog.

Bodies that are copies (or copies of a section) of one another predict a
maintenance trap: an edit to one copy never reaches the other. Descriptions
that are copies predict a routing trap: the listing cannot tell the two
apart. Both are facts measurable offline, so they are measured here.
"""

import tempfile
import unittest
from pathlib import Path

from agent_session_detective.catalog import Skill
from agent_session_detective.catalog_audit import (
    DEFAULT_DUP_CONTAINMENT,
    audit_catalog,
    detect_duplicates,
    render_audit,
)
from agent_session_detective.ir.estimator import estimate


def write_skill_text(root: Path, name: str, body: str,
                     description: str = None) -> Path:
    # Unique by default: identical descriptions across fixtures would form
    # desc pairs and drown the body-pair assertions these tests are about.
    description = description or ("Description for %s." % name)
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        "---\nname: %s\ndescription: %s\n---\n%s\n" % (name, description, body),
        encoding="utf-8")
    return path


def skill_at(path: Path, description: str = None) -> Skill:
    description = description or ("Description for %s." % path.parent.name)
    return Skill(name=path.parent.name, description=description,
                 trigger_excerpt=description, path=path,
                 description_parse_failed=len(description) < 3)


def numbered(start: int, end: int) -> str:
    return "\n".join("content line %d" % i for i in range(start, end + 1))


class BodyDupTest(unittest.TestCase):
    def test_identical_bodies_form_an_exact_pair(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills = [skill_at(write_skill_text(root, n, numbered(1, 20)))
                      for n in ("alpha", "beta")]
            pairs = detect_duplicates(skills)
        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertEqual((pair.a, pair.b), ("alpha", "beta"))
        self.assertEqual(pair.kind, "body")
        self.assertTrue(pair.exact)
        self.assertEqual(pair.containment, 1.0)
        self.assertEqual(pair.jaccard, 1.0)

    def test_subset_body_is_contained_not_identical(self):
        # beta is alpha with its tail cut off: containment must be 1.0 (every
        # beta shingle exists in alpha) while jaccard stays below 1.0 — the
        # asymmetry is what separates "copy of a section" from "same file".
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills = [
                skill_at(write_skill_text(root, "alpha", numbered(1, 20))),
                skill_at(write_skill_text(root, "beta", numbered(1, 15))),
            ]
            pairs = detect_duplicates(skills)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].containment, 1.0)
        self.assertLess(pairs[0].jaccard, 1.0)
        self.assertFalse(pairs[0].exact)

    def test_unrelated_bodies_form_no_pair(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills = [
                skill_at(write_skill_text(root, "alpha", numbered(1, 20))),
                skill_at(write_skill_text(root, "beta", "unrelated " * 40)),
            ]
            self.assertEqual(detect_duplicates(skills), [])

    def test_partial_overlap_respects_the_threshold(self):
        # alpha = lines 1-20, beta = lines 6-25: every window of beta has 13
        # counterparts in alpha, so containment is 13/18 â‰ˆ 0.72.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills = [
                skill_at(write_skill_text(root, "alpha", numbered(1, 20))),
                skill_at(write_skill_text(root, "beta", numbered(6, 25))),
            ]
            loose = detect_duplicates(skills, containment=0.6)
            strict = detect_duplicates(skills, containment=0.85)
        self.assertEqual(len(loose), 1)
        self.assertEqual(strict, [])

    def test_short_bodies_fall_back_to_line_sets(self):
        # Two identical two-line bodies have no 3-line window to shingle;
        # dropping them would silently exempt the smallest skills.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            body = "line one\nline two"
            skills = [skill_at(write_skill_text(root, n, body))
                      for n in ("alpha", "beta")]
            pairs = detect_duplicates(skills)
        self.assertEqual(len(pairs), 1)
        self.assertTrue(pairs[0].exact)

    def test_skill_without_a_readable_body_is_skipped(self):
        ghost = Skill(name="ghost", description="Injected routing text.",
                      trigger_excerpt="x", path=Path("<injected>"),
                      source="injected")
        with tempfile.TemporaryDirectory() as td:
            real = skill_at(write_skill_text(Path(td), "real", numbered(1, 20)))
            self.assertEqual(detect_duplicates([ghost, real]), [])


class DescDupTest(unittest.TestCase):
    def test_same_description_different_bodies_is_a_desc_pair(self):
        # Body content is allowed to be nothing alike — the trap is that the
        # listing shows two indistinguishable routing texts.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared = "Handles everything the same way here."
            skills = [
                skill_at(write_skill_text(root, "alpha", numbered(1, 20), shared),
                         shared),
                skill_at(write_skill_text(root, "beta", numbered(50, 70), shared),
                         shared),
            ]
            pairs = detect_duplicates(skills)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].kind, "desc")
        self.assertTrue(pairs[0].exact)

    def test_desc_pairs_respect_their_own_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            near_a = "Review pull requests against the checklist."
            near_b = "Review pull requests against the checklist carefully."
            skills = [
                skill_at(write_skill_text(root, "alpha", numbered(1, 20), near_a),
                         near_a),
                skill_at(write_skill_text(root, "beta", numbered(50, 70), near_b),
                         near_b),
            ]
            loose = detect_duplicates(skills, desc_containment=0.5)
            strict = detect_duplicates(skills, desc_containment=0.99)
        self.assertEqual(len(loose), 1)
        self.assertEqual(loose[0].kind, "desc")
        self.assertEqual(strict, [])


class ClusterTest(unittest.TestCase):
    def test_chained_pairs_merge_into_one_cluster(self):
        # alpha ~ union, union ~ gamma, but alpha is not directly a duplicate
        # of gamma. Merge candidates must still come out as one group.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills = [
                skill_at(write_skill_text(root, "alpha", numbered(1, 20))),
                skill_at(write_skill_text(root, "union", numbered(1, 35))),
                skill_at(write_skill_text(root, "gamma", numbered(16, 35))),
            ]
            audit = audit_catalog(skills)
        bodies = [p for p in audit.duplicates if p.kind == "body"]
        # alpha~union and union~gamma pair up; alpha~gamma alone would not.
        self.assertEqual(len(bodies), 2)
        self.assertEqual(audit.dup_clusters, [["alpha", "gamma", "union"]])
        self.assertAlmostEqual(audit.dup_rate, 1.0)


class AuditIntegrationTest(unittest.TestCase):
    def test_audit_exposes_duplicates_rate_and_activation_waste(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills = [
                skill_at(write_skill_text(root, "alpha", numbered(1, 20))),
                skill_at(write_skill_text(root, "beta", numbered(1, 20))),
                skill_at(write_skill_text(root, "gamma", numbered(50, 70))),
                skill_at(write_skill_text(root, "delta", numbered(80, 100))),
            ]
            audit = audit_catalog(skills)
        self.assertEqual(len(audit.duplicates), 1)
        self.assertAlmostEqual(audit.dup_rate, 2 / 4)
        by_name = {e.name: e.tokens for e in audit.size}
        self.assertEqual(audit.dup_waste_activation_tokens,
                         min(by_name["alpha"], by_name["beta"]))
        self.assertEqual(audit.dup_waste_discovery_tokens, 0)

    def test_desc_duplicates_account_discovery_waste(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared = "Identical routing text for both entries."
            skills = [
                skill_at(write_skill_text(root, "alpha", numbered(1, 20), shared),
                         shared),
                skill_at(write_skill_text(root, "beta", numbered(50, 70), shared),
                         shared),
            ]
            audit = audit_catalog(skills)
        self.assertEqual(audit.dup_waste_discovery_tokens, estimate(shared))
        self.assertEqual(audit.dup_waste_activation_tokens, 0)

    def test_thresholds_are_reported(self):
        audit = audit_catalog([])
        self.assertEqual(audit.thresholds["dup_containment"],
                         DEFAULT_DUP_CONTAINMENT)
        self.assertIsNone(audit.dup_rate)


class DupRenderTest(unittest.TestCase):
    def test_dup_section_names_the_pair(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills = [skill_at(write_skill_text(root, n, numbered(1, 20)))
                      for n in ("alpha", "beta")]
            text = render_audit(audit_catalog(skills), top=5)
        self.assertIn("重复度", text)
        self.assertIn("alpha", text)
        self.assertIn("beta", text)

    def test_clean_catalog_says_so_instead_of_printing_an_empty_section(self):
        with tempfile.TemporaryDirectory() as td:
            skill = skill_at(write_skill_text(Path(td), "solo", numbered(1, 20)))
            text = render_audit(audit_catalog([skill]))
        self.assertIn("未发现近重复", text)


if __name__ == "__main__":
    unittest.main()
