"""Static catalog audit: the four measurements and their boundaries.

The tests that matter most here are the negative ones — an audit that
crashes, or that invents a number, is worse than no audit, because its output
reads as a finding.
"""

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_detective.catalog import Skill, load_catalog, parse_injected
from agent_session_detective.catalog_audit import (
    DEFAULT_MAX_LINES,
    audit_catalog,
    descriptions_of_interest,
    read_body,
    render_audit,
)
from agent_session_detective.ir.estimator import ESTIMATOR_VERSION


def make_skill(name, description="A real description.", lines=10, path=None):
    """A skill backed by a real file, so size measurement has something to read."""
    return Skill(
        name=name,
        description=description,
        trigger_excerpt=description,
        path=path or Path("/nonexistent/%s/SKILL.md" % name),
        description_parse_failed=len(description) < 3,
    )


def write_skill(root: Path, name: str, description: str = "A real one.",
                body_lines: int = 10) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    body = "\n".join("body line %d" % i for i in range(body_lines))
    (directory / "SKILL.md").write_text(
        "---\nname: %s\ndescription: %s\n---\n%s\n" % (name, description, body),
        encoding="utf-8")
    return directory / "SKILL.md"


def skill_of_lines(root: Path, name: str, total_lines: int):
    """A skill whose whole file is exactly ``total_lines`` lines.

    Frontmatter counts toward the size, so asking a helper for "500 body lines"
    yields a 504-line file and quietly moves the tier. Grow the body until the
    file measures what the test is actually about.
    """
    path = write_skill(root, name, body_lines=10)
    overhead = len(path.read_text(encoding="utf-8").splitlines()) - 10
    path = write_skill(root, name, body_lines=max(total_lines - overhead, 1))
    assert len(path.read_text(encoding="utf-8").splitlines()) == total_lines, (
        "helper could not hit %d lines exactly" % total_lines)
    return make_skill(name, path=path)


class ReadBodyTest(unittest.TestCase):
    def test_reads_a_real_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_skill(Path(td), "s")
            self.assertIn("body line", read_body(make_skill("s", path=path)))

    def test_injected_sentinel_path_does_not_raise(self):
        # An injected entry with no body_path carries the sentinel "<injected>".
        # read_text on it raises, and an audit that trusted the path would die
        # or report a zero — a zero would read as "this skill is tiny".
        skill = Skill(name="ghost", description="Still routable.",
                      trigger_excerpt="x", path=Path("<injected>"),
                      source="injected")
        self.assertIsNone(read_body(skill))

    def test_missing_body_path_does_not_raise(self):
        skill = Skill(name="gone", description="Still routable.",
                      trigger_excerpt="x", path=Path("/no/such/SKILL.md"),
                      source="injected")
        self.assertIsNone(read_body(skill))


class SizeTierTest(unittest.TestCase):
    def test_thresholds_are_inclusive_of_their_documented_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills = [
                skill_of_lines(root, "over", DEFAULT_MAX_LINES + 1),
                skill_of_lines(root, "at", DEFAULT_MAX_LINES),
                skill_of_lines(root, "large", 300),
                skill_of_lines(root, "ok", 20),
            ]
            tiers = {e.name: e.tier for e in audit_catalog(skills).size}
        # "over" means strictly greater, so a file sitting exactly on the line
        # is not yet oversize.
        self.assertEqual(tiers["over"], "oversize")
        self.assertEqual(tiers["at"], "large")
        self.assertEqual(tiers["large"], "large")
        self.assertEqual(tiers["ok"], "ok")

    def test_thresholds_are_overridable(self):
        with tempfile.TemporaryDirectory() as td:
            skill = skill_of_lines(Path(td), "s", 30)
            strict = audit_catalog([skill], max_lines=10)
        self.assertEqual(strict.size[0].tier, "oversize")
        self.assertEqual(strict.thresholds["max_lines"], 10)

    def test_entries_are_sorted_by_token_cost(self):
        # Sorting by lines would put a prose-heavy skill below a code-heavy
        # one of the same length; tokens is what actually reaches the context.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills = [
                make_skill("prose", path=write_skill(
                    root, "prose", body_lines=20)),
                make_skill("cjk", path=write_skill(
                    root, "cjk", body_lines=20,
                    description="描述")),
            ]
            # give the CJK one a heavier body by rewriting it
            heavy = root / "cjk" / "SKILL.md"
            heavy.write_text(heavy.read_text(encoding="utf-8")
                             + "中文字符" * 50, encoding="utf-8")
            skills[1] = make_skill("cjk", path=heavy)
            audit = audit_catalog(skills)
        self.assertEqual(audit.size[0].name, "cjk")


class DescriptionTierTest(unittest.TestCase):
    def test_verbose_and_terse_are_both_flagged(self):
        skills = [
            make_skill("long", "x" * 400),
            make_skill("short", "ok."),
            make_skill("justright", "y" * 120),
        ]
        tiers = {e.name: e.tier for e in audit_catalog(skills).descriptions}
        self.assertEqual(tiers["long"], "verbose")
        self.assertEqual(tiers["short"], "terse")
        self.assertEqual(tiers["justright"], "ok")

    def test_parse_failure_outranks_length(self):
        # A 400-character description that did not parse is worse than a long
        # one that did: there is no routing text at all.
        broken = Skill(name="broken", description="", trigger_excerpt="",
                       path=Path("/nope/SKILL.md"), description_parse_failed=True)
        audit = audit_catalog([broken, make_skill("long", "x" * 400)])
        tiers = {e.name: e.tier for e in audit.descriptions}
        self.assertEqual(tiers["broken"], "unusable")
        self.assertEqual(tiers["long"], "verbose")

    def test_thresholds_are_overridable(self):
        audit = audit_catalog([make_skill("m", "z" * 100)], max_desc_chars=50)
        self.assertEqual(audit.descriptions[0].tier, "verbose")

    def test_interest_list_leads_with_the_worst(self):
        audit = audit_catalog(
            [make_skill("long", "x" * 400), make_skill("short", "ok."),
             make_skill("fine", "y" * 120)])
        names = [e.name for e in descriptions_of_interest(audit, top=2)]
        self.assertNotIn("fine", names)
        self.assertIn("long", names)


class LayeringTest(unittest.TestCase):
    def test_ratio_is_activation_over_discovery(self):
        with tempfile.TemporaryDirectory() as td:
            skill = make_skill("s", "A description.",
                               path=write_skill(Path(td), "s", body_lines=400))
            audit = audit_catalog([skill])
        self.assertGreater(audit.ratio, 1.0)
        self.assertEqual(
            audit.ratio,
            audit.activation_tokens / audit.discovery_tokens)

    def test_zero_descriptions_gives_no_ratio_rather_than_a_crash(self):
        # An all-failed catalog is exactly when a division would blow up, and
        # it must not manufacture an infinity either.
        broken = Skill(name="b", description="", trigger_excerpt="",
                       path=Path("/nope/SKILL.md"), description_parse_failed=True)
        audit = audit_catalog([broken])
        self.assertIsNone(audit.ratio)

    def test_skill_without_body_is_excluded_from_activation(self):
        with tempfile.TemporaryDirectory() as td:
            real = make_skill("real", path=write_skill(Path(td), "real",
                                                       body_lines=50))
            injected = Skill(name="inj", description="Injected routing text.",
                             trigger_excerpt="x", path=Path("<injected>"),
                             source="injected")
            audit = audit_catalog([real, injected])
        self.assertEqual(audit.body_count, 1)
        self.assertEqual(audit.no_body, ["inj"])
        self.assertEqual(len(audit.size), 1)


class EstimatorTest(unittest.TestCase):
    def test_version_is_reported(self):
        # The audit must say which token definition produced its numbers.
        audit = audit_catalog([make_skill("s")])
        self.assertEqual(audit.estimator, ESTIMATOR_VERSION)


class RenderTest(unittest.TestCase):
    def test_reports_all_four_sections(self):
        with tempfile.TemporaryDirectory() as td:
            skill = make_skill("big", "x" * 400,
                               path=write_skill(Path(td), "big", body_lines=900))
            text = render_audit(audit_catalog([skill]), top=5)
        for heading in ("分层有效性", "解析健康度", "体量分级", "description 质量"):
            self.assertIn(heading, text)

    def test_thresholds_are_labelled_as_conventions(self):
        # The numbers came from eyeballing a report, not from a spec. Saying so
        # is the difference between a convention and an invented requirement.
        text = render_audit(audit_catalog([make_skill("s")]))
        self.assertIn("约定阈值", text)
        self.assertIn("不是规范要求", text)

    def test_top_zero_lists_every_entry(self):
        with tempfile.TemporaryDirectory() as td:
            skills = [make_skill("s%d" % i, "x" * 400,
                                 path=write_skill(Path(td), "s%d" % i))
                      for i in range(12)]
            limited = render_audit(audit_catalog(skills), top=3)
            full = render_audit(audit_catalog(skills))
        self.assertIn("另有", limited)
        self.assertNotIn("另有 9 个", full)

    def test_missing_bodies_are_disclosed_not_silently_dropped(self):
        injected = Skill(name="inj", description="Injected routing text.",
                         trigger_excerpt="x", path=Path("<injected>"),
                         source="injected")
        text = render_audit(audit_catalog([injected]))
        self.assertIn("无正文", text)


class RealCatalogTest(unittest.TestCase):
    """Against the developer's own catalog — the case that actually matters."""

    def test_audit_runs_on_the_real_catalog(self):
        audit = audit_catalog(load_catalog())
        self.assertGreater(audit.total, 0)
        self.assertIsNotNone(audit.ratio)
        # Every skill is either measurable or explicitly declared unmeasurable.
        self.assertEqual(audit.body_count + len(audit.no_body), audit.total)


if __name__ == "__main__":
    unittest.main()
