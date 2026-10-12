# tests/test_catalog_closure.py
"""Static loading closure: what activating one skill drags in.

A skill body points at things — other skills via ``requires:``, local files
via markdown links and bare mentions of asset directories. Static closure is
the measurable version of that fan-out: which skills are reachable by name,
which files the bodies point at, and what the tree would cost together. It
makes no claim that any agent loaded any of it — that is the IR's job.
"""

import tempfile
import unittest
from pathlib import Path

from agent_session_detective.catalog import Skill, load_skill_file
from agent_session_detective.catalog_audit import (
    DEFAULT_CLOSURE_DEPTH,
    DEFAULT_CLOSURE_WARN_TOKENS,
    audit_catalog,
    detect_closures,
    render_audit,
)
from agent_session_detective.ir.estimator import estimate


def write_skill(root: Path, name: str, requires_line=None,
                body: str = None) -> Path:
    """A real SKILL.md on disk. ``requires_line`` is raw frontmatter text
    (one line, or several for the block form)."""
    body = body if body is not None else (
        "First body line of %s.\nSecond body line of %s." % (name, name))
    lines = ["---", "name: %s" % name, "description: Description for %s." % name]
    if requires_line is not None:
        lines.extend(requires_line if isinstance(requires_line, list)
                     else [requires_line])
    lines.append("---")
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text("\n".join(lines) + "\n" + body + "\n", encoding="utf-8")
    return path


def loaded(root: Path, name: str, **kwargs) -> Skill:
    """Load through the real parser, so ``requires`` is what the file says."""
    skill = load_skill_file(write_skill(root, name, **kwargs))
    assert skill is not None
    return skill


def write_asset(skill: Skill, rel: str, text: str) -> Path:
    path = Path(skill.path).parent / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def file_tokens(skill: Skill) -> int:
    return estimate(Path(skill.path).read_text(encoding="utf-8"))


class RequiresParsingTest(unittest.TestCase):
    def test_bracket_list_form(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "s", requires_line="requires: [mid, leaf]")
        self.assertEqual(skill.requires, ["mid", "leaf"])

    def test_comma_string_form(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "s", requires_line="requires: mid, leaf")
        self.assertEqual(skill.requires, ["mid", "leaf"])

    def test_block_list_form(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "s",
                           requires_line=["requires:", "  - mid", "  - leaf"])
        self.assertEqual(skill.requires, ["mid", "leaf"])

    def test_absent_key_means_no_dependencies(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "s")
        self.assertEqual(skill.requires, [])

    def test_repeated_names_collapse(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "s", requires_line="requires: mid, mid")
        self.assertEqual(skill.requires, ["mid"])


class ReferenceDiscoveryTest(unittest.TestCase):
    def test_markdown_link_to_a_local_file_is_counted(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "s",
                           body="See [the checklist](references/checklist.md).")
            asset = write_asset(skill, "references/checklist.md",
                                "point one\npoint two\n")
            entry = detect_closures([skill])[0]
            expected = file_tokens(skill) + estimate(
                asset.read_text(encoding="utf-8"))
        self.assertEqual(entry.files, ["references/checklist.md"])
        self.assertEqual(entry.tokens, expected)

    def test_scheme_urls_and_anchors_are_not_local_reads(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "s", body=(
                "See [docs](https://example.com/guide.md) and [top](#intro)."))
            entry = detect_closures([skill])[0]
            expected = file_tokens(skill)
        self.assertEqual(entry.files, [])
        self.assertEqual(entry.tokens, expected)

    def test_bare_mentions_of_asset_dirs_are_counted(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "s", body="Run `scripts/run.sh` to go.")
            write_asset(skill, "scripts/run.sh", "#!/bin/sh\necho hi\n")
            entry = detect_closures([skill])[0]
        self.assertEqual(entry.files, ["scripts/run.sh"])

    def test_missing_targets_are_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "s", body="Read references/gone.md anyway.")
            entry = detect_closures([skill])[0]
            expected = file_tokens(skill)
        self.assertEqual(entry.files, [])
        self.assertEqual(entry.tokens, expected)

    def test_repeated_references_count_once(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "s", body=(
                "Read references/dup.md. Later: [again](references/dup.md)."))
            asset = write_asset(skill, "references/dup.md", "only copy\n")
            entry = detect_closures([skill])[0]
            expected = file_tokens(skill) + estimate(
                asset.read_text(encoding="utf-8"))
        self.assertEqual(entry.files, ["references/dup.md"])
        self.assertEqual(entry.tokens, expected)


class ClosureTraversalTest(unittest.TestCase):
    def _chain(self, root: Path, names):
        """names[0] requires names[1] requires ... — returns the catalog."""
        catalog = []
        for i, name in enumerate(names):
            requires = ("requires: %s" % names[i + 1]
                        if i + 1 < len(names) else None)
            catalog.append(loaded(root, name, requires_line=requires))
        return catalog

    def test_requires_chain_is_followed_transitively(self):
        with tempfile.TemporaryDirectory() as td:
            top, mid, leaf = self._chain(Path(td), ["top", "mid", "leaf"])
            entries = {e.name: e for e in detect_closures([top, mid, leaf])}
            entry = entries["top"]
            expected = (file_tokens(top) + file_tokens(mid)
                        + file_tokens(leaf))
        self.assertEqual(entry.skills, ["leaf", "mid"])
        self.assertEqual(entry.tokens, expected)

    def test_cycles_terminate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = loaded(root, "a", requires_line="requires: b")
            b = loaded(root, "b", requires_line="requires: a")
            entries = {e.name: e for e in detect_closures([a, b])}
            entry = entries["a"]
            expected = file_tokens(a) + file_tokens(b)
        self.assertEqual(entry.skills, ["b"])
        self.assertEqual(entry.tokens, expected)

    def test_depth_cap_stops_expansion_and_flags_truncation(self):
        # A six-hop chain under a four-hop cap: the fifth dependency is the
        # frontier, and stopping there must be disclosed, not silent.
        with tempfile.TemporaryDirectory() as td:
            catalog = self._chain(Path(td), ["s0", "s1", "s2", "s3", "s4", "s5"])
            entries = {e.name: e for e in detect_closures(catalog, depth=4)}
            default_entries = {e.name: e for e in detect_closures(catalog)}
        self.assertEqual(DEFAULT_CLOSURE_DEPTH, 4)
        entry = entries["s0"]
        self.assertEqual(entry.skills, ["s1", "s2", "s3", "s4"])
        self.assertTrue(entry.truncated)
        self.assertEqual(default_entries["s0"].skills, entry.skills)

    def test_unresolved_names_are_reported_and_survive_transitively(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mid = loaded(root, "mid", requires_line="requires: ghost")
            top = loaded(root, "top", requires_line="requires: mid")
            entries = {e.name: e for e in detect_closures([top, mid])}
            entry = entries["top"]
        self.assertEqual(entry.unresolved, ["ghost"])
        self.assertEqual(entry.skills, ["mid"])

    def test_injected_entry_without_body_is_not_a_root(self):
        ghost = Skill(name="ghost", description="Injected routing text.",
                      trigger_excerpt="x", path=Path("<injected>"),
                      source="injected")
        with tempfile.TemporaryDirectory() as td:
            real = loaded(Path(td), "real")
            names = [e.name for e in detect_closures([ghost, real])]
        self.assertEqual(names, ["real"])

    def test_file_references_of_dependencies_are_namespaced(self):
        # Two skills pointing at "references/shared.md" mean two different
        # files. Counting them once by display text would understate the cost.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mid = loaded(root, "mid",
                         body="See [shared](references/shared.md).")
            top = loaded(root, "top", requires_line="requires: mid",
                         body="See [shared](references/shared.md) too.")
            mid_asset = write_asset(mid, "references/shared.md", "mid copy\n")
            top_asset = write_asset(top, "references/shared.md", "top copy\n")
            entries = {e.name: e for e in detect_closures([top, mid])}
            entry = entries["top"]
            expected = (file_tokens(top) + file_tokens(mid)
                        + estimate(mid_asset.read_text(encoding="utf-8"))
                        + estimate(top_asset.read_text(encoding="utf-8")))
        self.assertEqual(entry.files,
                         ["mid:references/shared.md", "references/shared.md"])
        self.assertEqual(entry.tokens, expected)


class ClosureAuditIntegrationTest(unittest.TestCase):
    def test_audit_carries_closures_and_their_thresholds(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "solo")
            audit = audit_catalog([skill])
        self.assertEqual([e.name for e in audit.closures], ["solo"])
        self.assertEqual(audit.thresholds["closure_depth"],
                         DEFAULT_CLOSURE_DEPTH)
        self.assertEqual(audit.thresholds["closure_warn_tokens"],
                         DEFAULT_CLOSURE_WARN_TOKENS)

    def test_closures_are_sorted_by_cost(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dep = loaded(root, "dep")
            heavy = loaded(root, "heavy", requires_line="requires: dep")
            light = loaded(root, "light")
            audit = audit_catalog([heavy, dep, light])
        self.assertEqual(audit.closures[0].name, "heavy")


class ClosureRenderTest(unittest.TestCase):
    def test_closure_section_renders_the_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            leaf = loaded(root, "leaf")
            mid = loaded(root, "mid", requires_line="requires: leaf")
            top = loaded(root, "top", requires_line="requires: mid")
            text = render_audit(audit_catalog([top, mid, leaf]))
        self.assertIn("加载闭包", text)
        for name in ("top", "mid", "leaf"):
            self.assertIn(name, text)

    def test_unresolved_requires_is_disclosed(self):
        with tempfile.TemporaryDirectory() as td:
            skill = loaded(Path(td), "s", requires_line="requires: ghost")
            text = render_audit(audit_catalog([skill]))
        self.assertIn("未解析", text)
        self.assertIn("ghost", text)

    def test_depth_truncation_is_disclosed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            names = ["s%d" % i for i in range(6)]
            catalog = [
                loaded(root, name,
                       requires_line=("requires: %s" % names[i + 1]
                                      if i + 1 < len(names) else None))
                for i, name in enumerate(names)
            ]
            text = render_audit(audit_catalog(catalog))
        self.assertIn("深度截断", text)

    def test_heavy_closure_is_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dep = loaded(root, "dep")
            heavy = loaded(root, "heavy", requires_line="requires: dep")
            # Strictly above the dependency alone: only the root's closure
            # crosses it, so exactly one entry is overweight.
            warn = file_tokens(dep) + 1
            text = render_audit(
                audit_catalog([heavy, dep], closure_warn_tokens=warn))
        self.assertIn("偏重 1 个", text)


if __name__ == "__main__":
    unittest.main()
